import asyncio
import json
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

os.environ.pop("BRIDGE_INSTANCE", None)
os.environ.setdefault("SECURE_1PSID", "mock_psid")
os.environ.setdefault("SECURE_1PSIDTS", "mock_psidts")

import main as app_main
from app.gateway_router import GatewayRouter
from app.lanes import LaneRegistry


class _MultiHostASGITransport(httpx.AsyncBaseTransport):
    def __init__(self, routes, connect_error_origins=None):
        self.routes = routes
        self.connect_error_origins = (
            connect_error_origins if connect_error_origins is not None else set()
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        origin = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
        if origin in self.connect_error_origins:
            raise httpx.ConnectError(f"Simulated connect error for {origin}", request=request)
        return await self.routes[origin].handle_async_request(request)


def _make_worker_app(lane_id: str, models: list[str], tracker: dict) -> FastAPI:
    w = FastAPI()

    @w.get("/v1/models")
    async def _models():
        tracker.setdefault("model_hits", []).append(lane_id)
        return {
            "object": "list",
            "data": [{"id": mid, "object": "model"} for mid in models],
        }

    @w.post("/v1/chat/completions")
    async def _chat(request: Request):
        body = await request.json()
        tracker["calls"].append({"lane": lane_id, "stream": bool(body.get("stream")), "messages": body.get("messages")})
        if tracker.get("force_status", {}).get(lane_id):
            code = tracker["force_status"][lane_id]
            return JSONResponse(status_code=code, content={"error": {"message": f"{lane_id} busy {code}", "code": "busy"}})

        tracker["active"] += 1
        tracker["max_active"] = max(tracker["max_active"], tracker["active"])
        try:
            delay = tracker.get("delay", 0.0)
            if body.get("stream"):
                async def _sse():
                    tracker["active_stream"] += 1
                    tracker["active_lanes"].add(lane_id)
                    tracker["max_active_stream"] = max(tracker["max_active_stream"], tracker["active_stream"])
                    tracker["max_concurrent_lanes"] = max(tracker["max_concurrent_lanes"], len(tracker["active_lanes"]))
                    try:
                        chunk1 = {"id": "c1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": f"hi-from-{lane_id}:"}, "finish_reason": None}]}
                        yield f"data: {json.dumps(chunk1)}\n\n".encode("utf-8")
                        if tracker.get("hold_stream_event") is not None:
                            await tracker["hold_stream_event"].wait()
                        elif delay:
                            await asyncio.sleep(delay)
                        chunk2 = {"id": "c1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]}
                        yield f"data: {json.dumps(chunk2)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                    except asyncio.CancelledError:
                        tracker["stream_cancelled"].append(lane_id)
                        raise
                    finally:
                        tracker["stream_closed"].append(lane_id)
                        tracker["active_lanes"].discard(lane_id)
                        tracker["active_stream"] -= 1

                # Hold worker concurrency until stream closes
                tracker["active"] -= 1
                return StreamingResponse(_sse(), media_type="text/event-stream")

            if delay:
                await asyncio.sleep(delay)
            return {
                "id": "c1",
                "object": "chat.completion",
                "model": body.get("model") or "gemini-4-neon",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"reply-from-{lane_id}"}, "finish_reason": "stop"}],
            }
        finally:
            if not body.get("stream"):
                tracker["active"] -= 1

    return w


class TestGatewayRouter(unittest.IsolatedAsyncioTestCase):
    def _build_env(self, lane_statuses=None, acc1_models=None, acc2_models=None):
        tracker = {
            "calls": [],
            "model_hits": [],
            "active": 0,
            "max_active": 0,
            "active_stream": 0,
            "max_active_stream": 0,
            "active_lanes": set(),
            "max_concurrent_lanes": 0,
            "force_status": {},
            "delay": 0.0,
            "hold_stream_event": None,
            "stream_cancelled": [],
            "stream_closed": [],
        }
        statuses = lane_statuses or {
            "acc1": {"state": "on", "auth_ok": True, "account_status": "AVAILABLE", "inflight": 0, "accounts": [{"label": "a1", "status": "active"}]},
            "acc2": {"state": "on", "auth_ok": True, "account_status": "AVAILABLE", "inflight": 0, "accounts": [{"label": "a2", "status": "active"}]},
        }

        async def _fake_http(method, url, headers=None, body=None, timeout=2.0):
            for lid, port in (("acc1", "10001"), ("acc2", "10002")):
                if f":{port}" in url:
                    st = statuses[lid]
                    if st.get("unreachable"):
                        return 0, {"error": "down"}
                    payload = dict(st)
                    payload.setdefault("accounts", [])
                    return 200, payload
            return 404, {"error": "not found"}

        peers = {"acc1": "http://127.0.0.1:10001", "acc2": "http://127.0.0.1:10002"}
        reg = LaneRegistry("frontend", peers, token="tok", http_call=_fake_http, cache_ttl=0.0, is_frontend_gateway=True)

        w1 = _make_worker_app("acc1", acc1_models or ["gemini-4-neon", "gemini-4-hydrogen"], tracker)
        w2 = _make_worker_app("acc2", acc2_models or ["gemini-4-hydrogen", "gemini-carbon"], tracker)
        transport = _MultiHostASGITransport(
            {
                "http://127.0.0.1:10001": httpx.ASGITransport(app=w1),
                "http://127.0.0.1:10002": httpx.ASGITransport(app=w2),
            }
        )
        gw = GatewayRouter(
            lane_registry=reg,
            api_key="",
            client_factory=lambda: httpx.AsyncClient(transport=transport),
        )
        gw.active = True
        return gw, tracker, statuses, transport

    async def test_lead_and_three_concurrent_subagents_execute_in_parallel_across_lanes(self):
        gw, tracker, _, _ = self._build_env()
        tracker["delay"] = 0.08
        try:
            with patch.object(app_main, "is_frontend_gateway", True), \
                 patch.object(app_main, "gateway_router", gw), \
                 patch.object(app_main, "API_KEY", ""), \
                 patch.object(app_main.activity, "write"):
                gw_transport = httpx.ASGITransport(app=app_main.app)
                async with httpx.AsyncClient(transport=gw_transport, base_url="http://127.0.0.1:10000") as ac:
                    # Lead Turn 1 -> acc1
                    r_lead = await ac.post(
                        "/v1/chat/completions",
                        json={
                            "model": "gemini-4-neon",
                            "stream": False,
                            "messages": [
                                {"role": "user", "content": '<system-reminder>\nYour name is "lead".\n</system-reminder>'},
                                {"role": "user", "content": "xay game co tuong"},
                            ],
                        },
                    )
                    self.assertEqual(r_lead.status_code, 200)
                    self.assertEqual(r_lead.headers.get("X-Gateway-Lane"), "acc1")

                    # Fire 3 concurrent Subagent SSE calls simultaneously -> must fan out cleanly onto both acc1 & acc2
                    sub_reqs = [
                        ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "gemini-4-neon",
                                "stream": True,
                                "messages": [
                                    {"role": "user", "content": f'<system-reminder>\nYou are a subagent.\nYour name is "{name}".\n</system-reminder>'},
                                    {"role": "user", "content": f"task for {name}"},
                                ],
                            },
                        )
                        for name in ("backend-agent", "web-agent", "pygame-agent")
                    ]
                    r1, r2, r3 = await asyncio.gather(*sub_reqs)

                for r in (r1, r2, r3):
                    self.assertEqual(r.status_code, 200)
                    self.assertIn("[DONE]", r.text)

                lanes = [r.headers.get("X-Gateway-Lane") for r in (r1, r2, r3)]
                self.assertEqual(lanes[0], "acc2")
                self.assertIn("acc1", lanes)
                self.assertIn("acc2", lanes)
                self.assertGreaterEqual(tracker["max_active_stream"], 2)
                self.assertEqual(tracker["max_concurrent_lanes"], 2)
                self.assertEqual(gw.get_stats()["inflight"].get("acc1", 0), 0)
                self.assertEqual(gw.get_stats()["inflight"].get("acc2", 0), 0)
        finally:
            await gw.close()

    async def test_sticky_pins_subsequent_turns_and_new_conv_rebalances(self):
        gw, tracker, _, _ = self._build_env()
        try:
            with patch.object(app_main, "is_frontend_gateway", True), \
                 patch.object(app_main, "gateway_router", gw), \
                 patch.object(app_main, "API_KEY", ""), \
                 patch.object(app_main.activity, "write"):
                gw_transport = httpx.ASGITransport(app=app_main.app)
                async with httpx.AsyncClient(transport=gw_transport, base_url="http://127.0.0.1:10000") as ac:
                    lead_t1 = [
                        {"role": "user", "content": '<system-reminder>\nYour name is "lead".\n</system-reminder>'},
                        {"role": "user", "content": "build xiangqi"},
                    ]
                    sub_t1 = [
                        {"role": "user", "content": '<system-reminder>\nYou are a subagent.\nYour name is "sub-a".\n</system-reminder>'},
                        {"role": "user", "content": "build backend"},
                    ]
                    r_lead1 = await ac.post("/v1/chat/completions", json={"messages": lead_t1})
                    r_sub1 = await ac.post("/v1/chat/completions", json={"messages": sub_t1})
                    self.assertEqual(r_lead1.headers.get("X-Gateway-Lane"), "acc1")
                    self.assertEqual(r_sub1.headers.get("X-Gateway-Lane"), "acc2")

                    # Turn 2 of sub-a must stick to acc2 even though acc1 is completely idle (inflight=0)
                    sub_t2 = sub_t1 + [
                        {"role": "assistant", "content": "ok"},
                        {"role": "user", "content": "next step"},
                    ]
                    r_sub2 = await ac.post("/v1/chat/completions", json={"messages": sub_t2})
                    self.assertEqual(r_sub2.headers.get("X-Gateway-Lane"), "acc2")

                    # Turn 2 of Lead must stick to acc1
                    lead_t2 = lead_t1 + [
                        {"role": "assistant", "content": "ok"},
                        {"role": "user", "content": "continue"},
                    ]
                    r_lead2 = await ac.post("/v1/chat/completions", json={"messages": lead_t2})
                    self.assertEqual(r_lead2.headers.get("X-Gateway-Lane"), "acc1")

                    # Brand-new conversation (is_new_conv=True) with same X-Session-Id header does not stick blindly if re-created, or balances when new key
                    r_sub_new = await ac.post(
                        "/v1/chat/completions",
                        json={
                            "messages": [
                                {"role": "user", "content": '<system-reminder>\nYou are a subagent.\nYour name is "sub-b".\n</system-reminder>'},
                                {"role": "user", "content": "brand new task"},
                            ]
                        },
                    )
                    self.assertEqual(r_sub_new.headers.get("X-Gateway-Lane"), "acc2")
        finally:
            await gw.close()

    async def test_failover_when_lane_off_503_connect_error_and_all_down(self):
        gw, tracker, statuses, transport = self._build_env()
        try:
            with patch.object(app_main, "is_frontend_gateway", True), \
                 patch.object(app_main, "gateway_router", gw), \
                 patch.object(app_main, "API_KEY", ""), \
                 patch.object(app_main.activity, "write"):
                gw_transport = httpx.ASGITransport(app=app_main.app)
                async with httpx.AsyncClient(transport=gw_transport, base_url="http://127.0.0.1:10000") as ac:
                    # 1. acc2 is off -> subagent routes cleanly to acc1
                    statuses["acc2"]["state"] = "off"
                    r1 = await ac.post(
                        "/v1/chat/completions",
                        json={
                            "messages": [
                                {"role": "user", "content": '<system-reminder>\nYou are a subagent.\nYour name is "sub-off".\n</system-reminder>'},
                                {"role": "user", "content": "task 1"},
                            ]
                        },
                    )
                    self.assertEqual(r1.status_code, 200)
                    self.assertEqual(r1.headers.get("X-Gateway-Lane"), "acc1")

                    # 2. Both online; acc1 returns 503 -> failover retries on acc2 transparently
                    statuses["acc2"]["state"] = "on"
                    tracker["force_status"]["acc1"] = 503
                    r2 = await ac.post(
                        "/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "lead failover 503"}]},
                    )
                    self.assertEqual(r2.status_code, 200)
                    self.assertEqual(r2.headers.get("X-Gateway-Lane"), "acc2")
                    self.assertIn("acc1", gw.get_stats()["cooldowns"])

                    # 3. Clear cooldowns & force_status, test ConnectError on acc2 -> falls back to acc1
                    tracker["force_status"].clear()
                    gw._cooldown_until.clear()
                    transport.connect_error_origins.add("http://127.0.0.1:10002")
                    r3 = await ac.post(
                        "/v1/chat/completions",
                        json={
                            "messages": [
                                {"role": "user", "content": '<system-reminder>\nYou are a subagent.\nYour name is "sub-conn".\n</system-reminder>'},
                                {"role": "user", "content": "connect fail"},
                            ]
                        },
                    )
                    self.assertEqual(r3.status_code, 200)
                    self.assertEqual(r3.headers.get("X-Gateway-Lane"), "acc1")

                    # 4. Both lanes off -> 503 no_healthy_lane
                    statuses["acc1"]["state"] = "off"
                    statuses["acc2"]["state"] = "off"
                    r4 = await ac.post(
                        "/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "all down"}]},
                    )
                    self.assertEqual(r4.status_code, 503)
                    self.assertEqual(r4.json()["error"]["code"], "no_healthy_lane")
        finally:
            await gw.close()

    async def test_local_title_and_models_aggregation(self):
        gw, tracker, _, _ = self._build_env(
            acc1_models=["gemini-4-neon", "gemini-4-hydrogen"],
            acc2_models=["gemini-4-hydrogen", "gemini-carbon"],
        )
        try:
            with patch.object(app_main, "is_frontend_gateway", True), \
                 patch.object(app_main, "gateway_router", gw), \
                 patch.object(app_main, "API_KEY", ""), \
                 patch.object(app_main.activity, "write"):
                gw_transport = httpx.ASGITransport(app=app_main.app)
                async with httpx.AsyncClient(transport=gw_transport, base_url="http://127.0.0.1:10000") as ac:
                    # Session title generation must be handled locally without calling either worker
                    r_title = await ac.post(
                        "/v1/chat/completions",
                        json={
                            "model": "gemini-4-neon",
                            "stream": False,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": (
                                        "Generate the session title from this JSON array of human messages:\n"
                                        + json.dumps([{"role": "user", "text": "Xay dung he thong quan ly ban co tuong"}])
                                    ),
                                },
                            ],
                        },
                    )
                    self.assertEqual(r_title.status_code, 200)
                    self.assertIn("Xay dung he thong", r_title.json()["choices"][0]["message"]["content"])
                    self.assertEqual(len(tracker["calls"]), 0)

                    # /v1/models aggregates unique models across workers
                    r_models = await ac.get("/v1/models")
                    self.assertEqual(r_models.status_code, 200)
                    ids = [m["id"] for m in r_models.json()["data"]]
                    self.assertEqual(ids, ["gemini-4-neon", "gemini-4-hydrogen", "gemini-carbon"])

                    # /v1/gateway/routes exposes live gateway state
                    r_stats = await ac.get("/v1/gateway/routes")
                    self.assertEqual(r_stats.status_code, 200)
                    self.assertTrue(r_stats.json()["active"])
        finally:
            await gw.close()

    async def test_inflight_released_when_sse_client_aborts_mid_stream(self):
        hold_event = asyncio.Event()
        closed_event = asyncio.Event()

        class _FakeAsyncStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"hi-from-acc1"}}]}\n\n'
                await hold_event.wait()

            async def aclose(self) -> None:
                closed_event.set()
                hold_event.set()

        class _StreamingAbortTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_FakeAsyncStream(),
                )

        statuses = {
            "acc1": {"state": "on", "auth_ok": True, "account_status": "AVAILABLE", "inflight": 0, "accounts": [{"label": "a1", "status": "active"}]},
            "acc2": {"state": "on", "auth_ok": True, "account_status": "AVAILABLE", "inflight": 0, "accounts": [{"label": "a2", "status": "active"}]},
        }

        async def _fake_http(method, url, headers=None, body=None, timeout=2.0):
            for lid, port in (("acc1", "10001"), ("acc2", "10002")):
                if f":{port}" in url:
                    return 200, dict(statuses[lid])
            return 404, {"error": "not found"}

        peers = {"acc1": "http://127.0.0.1:10001", "acc2": "http://127.0.0.1:10002"}
        reg = LaneRegistry("frontend", peers, token="tok", http_call=_fake_http, cache_ttl=0.0, is_frontend_gateway=True)
        gw = GatewayRouter(
            lane_registry=reg,
            api_key="",
            client_factory=lambda: httpx.AsyncClient(transport=_StreamingAbortTransport()),
        )
        gw.active = True

        try:
            req = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})
            msgs = [{"role": "user", "content": "abort mid stream"}]
            resp = await gw.proxy_chat_completions(
                req,
                {"model": "gemini-4-neon", "stream": True, "messages": msgs},
                msgs,
                "chatcmpl-abort-test",
                "gemini-4-neon",
                True,
                0.0,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("X-Gateway-Lane"), "acc1")
            first_chunk = await anext(resp.body_iterator)
            self.assertIn(b"hi-from-acc1", first_chunk)
            self.assertEqual(gw.get_stats()["inflight"].get("acc1"), 1)
            await resp.body_iterator.aclose()
            self.assertTrue(closed_event.is_set())
            self.assertEqual(gw.get_stats()["inflight"].get("acc1", 0), 0)
        finally:
            hold_event.set()
            await gw.close()


if __name__ == "__main__":
    unittest.main()
