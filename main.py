import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import (
    API_KEY,
    BASE_DIR,
    FORBIDDEN_TOOLS,
    HOST,
    LOG_DIR,
    PORT,
    SSE_HEARTBEAT_INTERVAL,
    logger,
)
from app.gemini_session import clear_response_cache, session_manager
from app.prompt_builder import build_incremental_prompt_and_files
from app.streaming import ToolCallAwareStreamer
from app.tool_parser import parse_tool_calls_from_reply

FRONTEND_DIR = BASE_DIR / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await session_manager.initialize()
    yield
    await session_manager.close()


app = FastAPI(
    title="DSH Gemini Agent Bridge (Enterprise Edition)",
    version="3.0.0",
    lifespan=lifespan,
)

# Gắn thư mục frontend tĩnh
if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


# =====================================================================
# API KEY AUTHENTICATION MIDDLEWARE
# =====================================================================
@app.middleware("http")
async def api_key_auth_middleware(request: Request, call_next):
    # Nếu cấu hình API_KEY và đường dẫn bắt đầu bằng /v1/ -> Bắt buộc xác thực
    if API_KEY and request.url.path.startswith("/v1/"):
        auth_header = request.headers.get("Authorization", "").strip()
        token = auth_header.replace("Bearer ", "").strip()
        if token != API_KEY:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized: Invalid or missing API Key"},
            )
    return await call_next(request)


# =====================================================================
# SYSTEM & HEALTH ENDPOINTS
# =====================================================================
@app.get("/")
@app.get("/health")
async def health_check():
    """Trả về chi tiết tình trạng sức khỏe của Bridge Server."""
    return session_manager.get_status()


@app.get("/dashboard")
async def dashboard_ui():
    """Phục vụ giao diện Dashboard từ thư mục frontend/."""
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "Không tìm thấy frontend/index.html"},
        )
    return FileResponse(index_file)


@app.post("/v1/reset")
async def reset_chat_session():
    """Chủ động mở một đoạn chat mới tinh trên Gemini Web."""
    await session_manager.reset_session()
    return {"status": "ok", "message": "Chat session reset successfully"}


@app.post("/v1/update-cookies")
async def update_cookies(request: Request):
    """Hot-reload cookie trực tiếp từ API mà không cần restart server."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    psid = body.get("SECURE_1PSID", "").strip()
    psidts = body.get("SECURE_1PSIDTS", "").strip()
    if not psid:
        return JSONResponse(
            status_code=400, content={"error": "SECURE_1PSID is required"}
        )

    await session_manager.update_cookies(psid, psidts)
    return {
        "status": "ok",
        "message": "Cookies updated successfully",
        "model": session_manager.default_model,
    }


@app.post("/v1/switch-model")
async def switch_model(request: Request):
    """Chuyển đổi model Gemini đang sử dụng."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    model_name = body.get("model", "").strip()
    keep_chat = body.get("keep_chat", True)
    if not model_name:
        return JSONResponse(
            status_code=400, content={"error": "model name is required"}
        )

    try:
        new_model = await session_manager.switch_model(model_name, keep_chat=keep_chat)
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={
                "error": str(e),
                "available_models": session_manager.available_models,
            },
        )
    return {
        "status": "ok",
        "active_model": new_model,
        "available_models": session_manager.available_models,
    }


@app.post("/v1/cache/clear")
async def clear_cache():
    """Xóa toàn bộ bộ nhớ đệm cache."""
    cleared = clear_response_cache()
    return {"status": "ok", "cleared_entries": cleared}


@app.get("/v1/logs")
async def get_recent_logs(lines: int = 50):
    """Lấy danh sách các dòng log gần nhất để hiển thị lên Dashboard."""
    log_file = LOG_DIR / "bridge.log"
    if not log_file.exists():
        return {"logs": ["Chưa có tệp nhật ký logs/bridge.log"]}
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return {"logs": [line.strip() for line in all_lines[-lines:]]}
    except Exception as e:
        return {"logs": [f"Lỗi đọc nhật ký: {e}"]}


@app.get("/v1/models")
async def list_models():
    """Trả về danh sách model thực tế được phát hiện trên tài khoản."""
    details = {
        d["name"]: d for d in (session_manager.model_details or [])
    }
    models_data = []
    for m in session_manager.available_models:
        meta = details.get(m, {})
        models_data.append(
            {
                "id": m,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google",
                "display_name": meta.get("display_name", m),
                "description": meta.get("description", ""),
            }
        )
    return {"object": "list", "data": models_data}


# =====================================================================
# OPENAI CHAT COMPLETIONS API (Core Agent Pipeline)
# =====================================================================
@app.post("/v1/chat/completions")
@app.post("/v1/responses")
async def chat_completions(request: Request):
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
        if t.get("function", t).get("name")
        and t.get("function", t).get("name").lower() not in FORBIDDEN_TOOLS
    }

    model_id = body.get("model", session_manager.default_model)
    is_stream = body.get("stream", False)
    chat_id = f"chatcmpl-{uuid.uuid4().hex}"
    created_ts = int(time.time())

    # Rút trích prompt gia tăng
    prompt_to_send, image_files = build_incremental_prompt_and_files(
        messages, tools, is_first_turn=session_manager.needs_tool_header
    )
    del body, messages, tools

    # Tính toán ước tính số token (OpenAI Usage Stats)
    prompt_tokens = max(1, len(prompt_to_send) // 4)

    def _content_chunk(piece: str) -> dict:
        return {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": model_id,
            "choices": [
                {"index": 0, "delta": {"content": piece}, "finish_reason": None}
            ],
        }

    # ─────────────────────────────────────────────────────────────────
    # LUỒNG STREAMING (SSE + Keep-Alive Heartbeat + real text deltas)
    # ─────────────────────────────────────────────────────────────────
    if is_stream:
        async def sse_generator():
            first_chunk = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

            queue: asyncio.Queue = asyncio.Queue()
            producer_done = asyncio.Event()

            async def producer():
                try:
                    async for item in session_manager.send_prepared_turn_stream(
                        prompt_to_send, image_files
                    ):
                        await queue.put(item)
                except Exception as exc:  # defensive
                    logger.error(f"[SSE] producer error: {exc}")
                finally:
                    producer_done.set()

            prod_task = asyncio.create_task(producer())
            splitter = ToolCallAwareStreamer()
            done_text = None

            try:
                while True:
                    if producer_done.is_set() and queue.empty():
                        break
                    try:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=SSE_HEARTBEAT_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        if producer_done.is_set() and queue.empty():
                            break
                        yield ": keep-alive\n\n"
                        continue

                    kind, payload = item
                    if kind == "delta":
                        for piece in splitter.feed(payload):
                            yield f"data: {json.dumps(_content_chunk(piece), ensure_ascii=False)}\n\n"
                    elif kind == "text":
                        yield f"data: {json.dumps(_content_chunk(payload), ensure_ascii=False)}\n\n"
                    elif kind == "done":
                        done_text = payload

                for piece in splitter.flush():
                    yield f"data: {json.dumps(_content_chunk(piece), ensure_ascii=False)}\n\n"

                reply_text = done_text if done_text is not None else splitter.full_text
                tool_calls = parse_tool_calls_from_reply(reply_text, tools_map)

                if tool_calls:
                    tc_deltas = [
                        {
                            "index": idx,
                            "id": tc["id"],
                            "type": "function",
                            "function": tc["function"],
                        }
                        for idx, tc in enumerate(tool_calls)
                    ]
                    tool_chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model_id,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"tool_calls": tc_deltas},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(tool_chunk, ensure_ascii=False)}\n\n"
                    finish_reason = "tool_calls"
                else:
                    finish_reason = "stop"

                completion_tokens = max(1, len(reply_text) // 4)
                end_chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }
                yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except asyncio.CancelledError:
                prod_task.cancel()
                try:
                    await prod_task
                except (asyncio.CancelledError, Exception):
                    pass
                raise

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ─────────────────────────────────────────────────────────────────
    # LUỒNG NON-STREAMING
    # ─────────────────────────────────────────────────────────────────
    reply_text = await session_manager.send_prepared_turn(prompt_to_send, image_files)
    tool_calls = parse_tool_calls_from_reply(reply_text, tools_map)

    if tool_calls:
        message_obj = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        finish_reason = "tool_calls"
    else:
        message_obj = {"role": "assistant", "content": reply_text}
        finish_reason = "stop"

    completion_tokens = max(1, len(reply_text) // 4)
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created_ts,
        "model": model_id,
        "choices": [
            {"index": 0, "message": message_obj, "finish_reason": finish_reason}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
        limit_concurrency=200,
        timeout_keep_alive=15,
    )
