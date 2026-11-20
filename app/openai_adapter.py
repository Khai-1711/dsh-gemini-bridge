"""Lớp biên dịch chuẩn OpenAI (/v1/models, /v1/chat/completions, /v1/responses)."""
import asyncio
import contextlib
import json
import sys
import time
from typing import Any, List, Optional, Tuple
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.activity import record_failed_tool_trace, summarize_tool_calls
from app.config import FORBIDDEN_TOOLS, LOCK_ACQUIRE_TIMEOUT, logger
from app.errors import BridgeBusyError, BridgeError
from app.gemini_session import compute_session_key, is_subagent_messages
from app.prompt_builder import compile_tools_prompt, is_plain_greeting_request
from app.streaming import ToolCallAwareStreamer
from app.title_generation import extract_local_title_request
from app.tool_parser import (
    ToolParseResult,
    has_tool_call_intent,
    parse_tool_calls_detailed,
)

openai_router = APIRouter()


def _m():
    return sys.modules.get("main") or sys.modules["__main__"]


def _active_gateway_router(m: Any):
    router = getattr(m, "gateway_router", None)
    if getattr(m, "is_frontend_gateway", False) and router is not None and getattr(router, "active", False):
        return router
    return None


def _turn_activity_model_fields(mgr: Any, requested_model_id: str) -> Tuple[str, dict]:
    raw_model = getattr(mgr, "last_turn_model", None)
    actual_model = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else requested_model_id
    raw_fb = getattr(mgr, "last_turn_fallback_from", None)
    fb_from = raw_fb.strip() if isinstance(raw_fb, str) and raw_fb.strip() else None
    extra = {"fallback_from": fb_from} if fb_from else {}
    return actual_model, extra


def _reset_turn_activity_fields(mgr: Any) -> None:
    if isinstance(getattr(mgr, "last_turn_model", None), str):
        mgr.last_turn_model = ""
    if isinstance(getattr(mgr, "last_turn_fallback_from", None), str):
        mgr.last_turn_fallback_from = None
    if isinstance(getattr(mgr, "last_turn_prompt_chars", None), int):
        mgr.last_turn_prompt_chars = 0


def _actual_prompt_chars(mgr: Any, default_chars: int) -> int:
    raw_chars = getattr(mgr, "last_turn_prompt_chars", None)
    return raw_chars if isinstance(raw_chars, int) and raw_chars > 0 else default_chars


def _invalid_tool_batch_error(parse_res: ToolParseResult, reply_text: str) -> Optional[dict]:
    has_invalid = bool(
        parse_res.malformed_tags
        or parse_res.has_unclosed_tag
        or (has_tool_call_intent(reply_text) and not parse_res.tool_calls)
    )
    if not has_invalid:
        return None
    if parse_res.malformed_tags:
        details = "; ".join(
            f"thẻ #{item['index']}: {item['reason']}"
            for item in parse_res.malformed_tags[:3]
        )
        msg = f"Gemini trả về tool call không hợp lệ ({details}); chưa có tool nào được thực thi."
    elif parse_res.has_unclosed_tag:
        msg = "Gemini trả về thẻ <tool_call> bị cắt cụt (thiếu thẻ đóng </tool_call>); chưa có tool nào được thực thi."
    else:
        msg = "Gemini trả về tool call không hợp lệ hoặc không khớp schema; chưa có tool nào được thực thi."
    return {"message": msg, "type": "upstream_error", "code": "invalid_tool_call"}


def _handle_local_title_response(
    messages: list, chat_id: str, created_ts: int, model_id: str, is_stream: bool, t0: float
):
    m = _m()
    is_title_req, local_title, title_chars = extract_local_title_request(messages)
    if not is_title_req:
        return None
    if local_title is None:
        m.activity.write(
            request_id=chat_id,
            model=model_id,
            stream=is_stream,
            stage="session_title_local",
            latency_ms=int((time.time() - t0) * 1000),
            outcome="error:invalid_title_request",
        )
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "DSH session title request is malformed.", "type": "invalid_request_error", "code": "invalid_title_request"}},
        )

    prompt_tokens = max(1, title_chars // 4)
    completion_tokens = max(1, len(local_title) // 4)
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens}
    m.activity.write(
        request_id=chat_id,
        model=model_id,
        stream=is_stream,
        stage="session_title_local",
        prompt_chars=title_chars,
        reply_chars=len(local_title),
        latency_ms=int((time.time() - t0) * 1000),
        outcome="ok",
    )

    if is_stream:
        async def _title_sse():
            for delta, finish, usg in (
                ({"role": "assistant", "content": ""}, None, None),
                ({"content": local_title}, None, None),
                ({}, "stop", usage),
            ):
                chunk = {"id": chat_id, "object": "chat.completion.chunk", "created": created_ts, "model": model_id, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if usg is not None:
                    chunk["usage"] = usg
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _title_sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created_ts,
        "model": model_id,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": local_title}, "finish_reason": "stop"}],
        "usage": usage,
    }


@openai_router.get("/v1/models")
async def list_models():
    """Trả về danh sách model thực tế được phát hiện trên tài khoản (hoặc tổng hợp qua Gateway)."""
    m = _m()
    if (gw := _active_gateway_router(m)) is not None:
        return await gw.proxy_models()

    mgr = m.session_manager
    try:
        mgr.require_on()
    except BridgeError as e:
        return JSONResponse(status_code=e.status_code, content={"error": {"message": e.message, "type": e.err_type, "code": e.code}})
    details = {d["name"]: d for d in (mgr.model_details or [])}
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google",
                "display_name": details.get(m, {}).get("display_name", m),
                "description": details.get(m, {}).get("description", ""),
            }
            for m in mgr.available_models
        ],
    }


@openai_router.post("/v1/chat/completions")
@openai_router.post("/v1/responses")
async def chat_completions(request: Request):
    m = _m()
    gw = _active_gateway_router(m)
    if gw is not None:
        t0 = time.time()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON payload"})

        messages = body.get("messages") or body.get("input") or []
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]

        requested_model = str(body.get("model") or "gemini-4-neon").strip()
        model_id = requested_model or "gemini-4-neon"
        if model_id.lower() in ("default", "auto", "none", "unspecified", "gemini"):
            model_id = "gemini-4-neon"
        is_stream = bool(body.get("stream", False))
        chat_id = f"chatcmpl-{uuid.uuid4().hex}"
        created_ts = int(t0)

        if title_resp := _handle_local_title_response(messages, chat_id, created_ts, model_id, is_stream, t0):
            return title_resp

        return await gw.proxy_chat_completions(request, body, messages, chat_id, model_id, is_stream, t0)

    mgr = m.session_manager
    try:
        mgr.require_on()
    except BridgeError as e:
        return JSONResponse(status_code=e.status_code, content={"error": {"message": e.message, "type": e.err_type, "code": e.code}})

    t0 = time.time()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON payload"})

    messages = body.get("messages") or body.get("input") or []
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]

    tools = body.get("tools") or []
    tools_map = {
        t.get("function", t).get("name"): t.get("function", t)
        for t in tools
        if t.get("function", t).get("name") and t.get("function", t).get("name").lower() not in FORBIDDEN_TOOLS
    }

    requested_model = str(body.get("model") or mgr.default_model).strip()
    model_id = requested_model or mgr.default_model
    if model_id.lower() in ("default", "auto", "none", "unspecified", "gemini"):
        model_id = mgr.default_model
    is_stream = body.get("stream", False)
    chat_id = f"chatcmpl-{uuid.uuid4().hex}"
    created_ts = int(t0)
    del body

    if title_resp := _handle_local_title_response(messages, chat_id, created_ts, model_id, is_stream, t0):
        return title_resp

    if not await mgr.ensure_authenticated():
        m.activity.write(request_id=chat_id, model=model_id, stream=is_stream, chat_key=mgr.current_chat_key(), auth_ok=False, stage="authentication", latency_ms=int((time.time() - t0) * 1000), outcome="error:gemini_not_authenticated")
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "Gemini chưa đăng nhập (guest mode hoặc cookie hết hạn). Hãy cập nhật cookie ở /dashboard hoặc POST /v1/update-cookies.", "type": "auth_error", "code": "gemini_not_authenticated"}},
        )

    if model_id not in ("unspecified", mgr.default_model):
        model_status, model_data = await m._local_switch_model(model_id, keep_chat=True)
        if model_status != 200:
            m.activity.write(request_id=chat_id, model=model_id, stream=is_stream, chat_key=mgr.current_chat_key(), auth_ok=mgr.auth_ok, stage="model_selection", latency_ms=int((time.time() - t0) * 1000), outcome=f"error:{model_data.get('error', {}).get('code', 'model_not_available')}")
            return JSONResponse(status_code=model_status, content=model_data)
        model_id = model_data["active_model"]
    else:
        model_id = mgr.default_model

    explicit_sid = (request.headers.get("X-Session-Id") or request.headers.get("X-DSH-Session-Id") or "").strip()
    conv_key, is_new_conv = compute_session_key(messages, explicit_sid)
    is_subagent = is_subagent_messages(messages)

    async def _prepare_locked_turn() -> Tuple[str, List[str], bool, Any]:
        if hasattr(mgr, "activate_session_slot"):
            await mgr.activate_session_slot(conv_key, is_new_conversation=is_new_conv, is_subagent=is_subagent)
        else:
            has_dirty = bool(getattr(mgr, "chat_session", None) and getattr(mgr.chat_session, "cid", None))
            if is_new_conv and (mgr.turn_count > 0 or has_dirty):
                await mgr.reset_session()

        mgr.remember_transcript(messages)
        active_ledger = mgr.ledger
        if messages and messages[-1].get("role") == "user":
            active_ledger.note_user_goal(messages[-1].get("content"))
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                break
            if msg.get("role") == "tool":
                active_ledger.note_tool_result(msg.get("tool_call_id"), msg.get("content"))

        tool_header = compile_tools_prompt(tools)
        mgr.remember_tool_header(tool_header)
        needs_header = mgr.needs_tool_header
        defer_header = needs_header and is_plain_greeting_request(messages)
        prompt_text, img_files = m.build_incremental_prompt_and_files(messages, tools, is_first_turn=needs_header and not defer_header)
        return prompt_text, img_files, defer_header, active_ledger

    def _content_chunk(piece: str) -> dict:
        return {"id": chat_id, "object": "chat.completion.chunk", "created": created_ts, "model": model_id, "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}

    if is_stream:
        async def sse_generator():
            first_chunk = {"id": chat_id, "object": "chat.completion.chunk", "created": created_ts, "model": model_id, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
            yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

            queue: asyncio.Queue = asyncio.Queue()
            producer_done = asyncio.Event()
            turn_state: dict = {"prompt_chars": 0, "prompt_tokens": 1, "ledger": mgr.ledger}

            async def producer():
                acquired_lock = False
                try:
                    try:
                        acquired_lock = await mgr.acquire_turn_lock(LOCK_ACQUIRE_TIMEOUT)
                    except asyncio.TimeoutError:
                        mgr.total_errors += 1
                        await queue.put(("error", {"message": "Bridge đang bận, thử lại sau vài giây.", "type": BridgeBusyError.err_type, "code": BridgeBusyError.code, "stage": "lock"}))
                        return

                    prompt_to_send, image_files, defer_tool_header, active_ledger = await _prepare_locked_turn()
                    _reset_turn_activity_fields(mgr)
                    turn_state["prompt_chars"] = len(prompt_to_send)
                    turn_state["prompt_tokens"] = max(1, len(prompt_to_send) // 4)
                    turn_state["ledger"] = active_ledger

                    turn_stream = (
                        mgr.send_prepared_turn_stream(prompt_to_send, image_files, include_tool_header=False)
                        if defer_tool_header
                        else mgr.send_prepared_turn_stream(prompt_to_send, image_files)
                    )
                    async with contextlib.aclosing(turn_stream) as stream:
                        async for item in stream:
                            await queue.put(item)
                except Exception as exc:
                    logger.error(f"[SSE] producer error: {exc}")
                    error = {"message": exc.message, "type": exc.err_type, "code": exc.code} if isinstance(exc, BridgeError) else {"message": "Streaming response failed before completion.", "type": "upstream_error", "code": "stream_producer_error"}
                    await queue.put(("error", error))
                finally:
                    mgr.release_turn_lock(acquired_lock)
                    producer_done.set()

            prod_task = asyncio.create_task(producer())
            splitter = ToolCallAwareStreamer()
            done_text = None

            try:
                while True:
                    if producer_done.is_set() and queue.empty():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=m.SSE_HEARTBEAT_INTERVAL)
                    except asyncio.TimeoutError:
                        if producer_done.is_set() and queue.empty():
                            break
                        yield ": keep-alive\n\n"
                        continue

                    kind, payload = item
                    if kind == "error":
                        err_code = payload.get("code", "error") if isinstance(payload, dict) else "error"
                        stage = payload.get("stage") if isinstance(payload, dict) else None
                        actual_model, fb_extra = _turn_activity_model_fields(mgr, model_id)
                        m.activity.write(request_id=chat_id, model=actual_model, stream=True, chat_key=mgr.current_chat_key(), auth_ok=mgr.auth_ok, stage=stage, latency_ms=int((time.time() - t0) * 1000), outcome=f"error:{err_code}", **fb_extra)
                        yield f"data: {json.dumps({'error': payload}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    elif kind == "delta":
                        for piece in splitter.feed(payload):
                            yield f"data: {json.dumps(_content_chunk(piece), ensure_ascii=False)}\n\n"
                    elif kind == "text":
                        yield f"data: {json.dumps(_content_chunk(payload), ensure_ascii=False)}\n\n"
                    elif kind == "done":
                        done_text = payload

                for piece in splitter.flush():
                    yield f"data: {json.dumps(_content_chunk(piece), ensure_ascii=False)}\n\n"

                reply_text = done_text if done_text is not None else splitter.full_text
                actual_model, fb_extra = _turn_activity_model_fields(mgr, model_id)
                actual_prompt_chars = _actual_prompt_chars(mgr, turn_state["prompt_chars"])
                parse_res = parse_tool_calls_detailed(reply_text, tools_map)
                tool_calls = parse_res.tool_calls
                if (invalid_err := _invalid_tool_batch_error(parse_res, reply_text)) is not None:
                    record_failed_tool_trace(m.FAILURE_TRACES_FILE, request_id=chat_id, model=actual_model, reply_text=reply_text, reason="invalid_tool_call")
                    m.activity.write(request_id=chat_id, model=actual_model, stream=True, chat_key=mgr.current_chat_key(), auth_ok=mgr.auth_ok, stage="tool_parse", latency_ms=int((time.time() - t0) * 1000), outcome="error:invalid_tool_call", **fb_extra)
                    yield f"data: {json.dumps({'error': invalid_err}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                active_ledger = turn_state["ledger"]
                active_ledger.note_tool_calls(tool_calls)
                active_ledger.note_assistant(reply_text)

                if tool_calls:
                    tc_deltas = [{"index": idx, "id": tc["id"], "type": "function", "function": tc["function"]} for idx, tc in enumerate(tool_calls)]
                    yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created_ts, 'model': model_id, 'choices': [{'index': 0, 'delta': {'tool_calls': tc_deltas}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
                    finish_reason = "tool_calls"
                else:
                    finish_reason = "stop"

                completion_tokens = max(1, len(reply_text) // 4)
                prompt_tokens = max(turn_state["prompt_tokens"], actual_prompt_chars // 4)
                end_chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                    "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
                }
                m.activity.write(request_id=chat_id, model=actual_model, stream=True, chat_key=mgr.current_chat_key(), auth_ok=mgr.auth_ok, stage="complete", prompt_chars=actual_prompt_chars, reply_chars=len(reply_text), finish=finish_reason, tools=summarize_tool_calls(tool_calls), latency_ms=int((time.time() - t0) * 1000), outcome="ok", **fb_extra)
                yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                if not prod_task.done():
                    prod_task.cancel()
                await asyncio.gather(prod_task, return_exceptions=True)

        return StreamingResponse(sse_generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    acquired_lock = False
    try:
        try:
            acquired_lock = await mgr.acquire_turn_lock(LOCK_ACQUIRE_TIMEOUT)
        except asyncio.TimeoutError:
            mgr.total_errors += 1
            busy = BridgeBusyError("Bridge đang bận, thử lại sau vài giây.", stage="lock")
            m.activity.write(request_id=chat_id, model=model_id, stream=False, chat_key=mgr.current_chat_key(), auth_ok=mgr.auth_ok, stage=busy.stage, latency_ms=int((time.time() - t0) * 1000), outcome=f"error:{busy.code}")
            return JSONResponse(status_code=busy.status_code, content={"error": {"message": busy.message, "type": busy.err_type, "code": busy.code}})

        prompt_to_send, image_files, defer_tool_header, active_ledger = await _prepare_locked_turn()
        _reset_turn_activity_fields(mgr)
        prompt_tokens = max(1, len(prompt_to_send) // 4)
        reply_text = await (
            mgr.send_prepared_turn(prompt_to_send, image_files, include_tool_header=False)
            if defer_tool_header
            else mgr.send_prepared_turn(prompt_to_send, image_files)
        )
    except BridgeError as e:
        actual_model, fb_extra = _turn_activity_model_fields(mgr, model_id)
        m.activity.write(request_id=chat_id, model=actual_model, stream=False, chat_key=mgr.current_chat_key(), auth_ok=mgr.auth_ok, stage=e.stage, latency_ms=int((time.time() - t0) * 1000), outcome=f"error:{e.code}", **fb_extra)
        return JSONResponse(status_code=e.status_code, content={"error": {"message": e.message, "type": e.err_type, "code": e.code}})
    finally:
        mgr.release_turn_lock(acquired_lock)

    actual_model, fb_extra = _turn_activity_model_fields(mgr, model_id)
    actual_prompt_chars = _actual_prompt_chars(mgr, len(prompt_to_send))
    prompt_tokens = max(prompt_tokens, actual_prompt_chars // 4)
    parse_res = parse_tool_calls_detailed(reply_text, tools_map)
    tool_calls = parse_res.tool_calls
    if (invalid_err := _invalid_tool_batch_error(parse_res, reply_text)) is not None:
        record_failed_tool_trace(m.FAILURE_TRACES_FILE, request_id=chat_id, model=actual_model, reply_text=reply_text, reason="invalid_tool_call")
        m.activity.write(request_id=chat_id, model=actual_model, stream=False, chat_key=mgr.current_chat_key(), auth_ok=mgr.auth_ok, stage="tool_parse", latency_ms=int((time.time() - t0) * 1000), outcome="error:invalid_tool_call", **fb_extra)
        return JSONResponse(status_code=502, content={"error": invalid_err})

    active_ledger.note_tool_calls(tool_calls)
    active_ledger.note_assistant(reply_text)
    message_obj = {"role": "assistant", "content": None, "tool_calls": tool_calls} if tool_calls else {"role": "assistant", "content": reply_text}
    finish_reason = "tool_calls" if tool_calls else "stop"

    m.activity.write(request_id=chat_id, model=actual_model, stream=False, chat_key=mgr.current_chat_key(), auth_ok=mgr.auth_ok, stage="complete", prompt_chars=actual_prompt_chars, reply_chars=len(reply_text), finish=finish_reason, tools=summarize_tool_calls(tool_calls), latency_ms=int((time.time() - t0) * 1000), outcome="ok", **fb_extra)
    completion_tokens = max(1, len(reply_text) // 4)
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created_ts,
        "model": model_id,
        "choices": [{"index": 0, "message": message_obj, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
    }
