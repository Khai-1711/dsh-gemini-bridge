import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Đảm bảo pop BRIDGE_INSTANCE trước khi import
os.environ.pop("BRIDGE_INSTANCE", None)
os.environ.setdefault("SECURE_1PSID", "canary_mock_psid_secret_12345")
os.environ.setdefault("SECURE_1PSIDTS", "canary_mock_psidts_secret_67890")

from app.lanes import (
    LaneRegistry,
    build_local_lane_status,
    _format_remote_status,
    check_env_file_id,
)
from starlette.requests import Request


class TestLanes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.canary_psid = "canary_mock_psid_secret_12345"
        self.canary_token = "canary_lane_token_super_secret_9999"

    def test_build_local_lane_status_schema_and_no_leak(self):
        manager = MagicMock()
        manager.state = "on"
        manager._inflight = 2
        manager.auth_ok = True
        manager.account_status_name = "AVAILABLE"
        manager.active_index = 0
        manager.start_time = 1000.0
        manager.default_model = "gemini-pro"
        manager.total_requests = 10
        manager.total_errors = 1
        manager.current_chat_key = MagicMock(return_value="abc12345")
        manager.get_status = MagicMock(return_value={
            "accounts": [
                {"label": "A", "status": "active", "resting_until": None, "reason": None},
                {"label": "B", "status": "ready", "resting_until": None, "reason": None},
            ],
            "active_account": "A",
            "last_activity_ts": "2026-11-15T10:00:00+09:00",
        })

        st = build_local_lane_status(
            manager,
            port=10000,
            instance_id="acc1",
            instance_label="Frontend",
            cache_enabled=False,
        )

        # Kiểm tra đầy đủ các trường theo đúng lược đồ JSON chuẩn
        expected_fields = {
            "id", "label", "url", "port", "is_self", "reachable", "control",
            "inflight", "auth_ok", "account_status", "active_account",
            "accounts", "model", "requests", "errors", "uptime_s",
            "chat_key", "last_activity_ts", "cache_enabled", "controllable",
            "has_env_id", "env_file", "id_status"
        }
        self.assertEqual(set(st.keys()), expected_fields)
        self.assertEqual(st["id"], "acc1")
        self.assertEqual(st["label"], "Frontend")
        self.assertEqual(st["url"], "http://127.0.0.1:10000")
        self.assertEqual(st["port"], 10000)
        self.assertTrue(st["is_self"])
        self.assertTrue(st["reachable"])
        self.assertEqual(st["control"], "on")
        self.assertEqual(st["inflight"], 2)
        self.assertTrue(st["auth_ok"])
        self.assertEqual(st["account_status"], "AVAILABLE")
        self.assertEqual(st["active_account"], {"label": "A", "index": 0, "total": 2})
        self.assertEqual(len(st["accounts"]), 2)
        self.assertEqual(st["model"], "gemini-pro")
        self.assertEqual(st["requests"], 10)
        self.assertEqual(st["errors"], 1)
        self.assertEqual(st["chat_key"], "abc12345")
        self.assertEqual(st["last_activity_ts"], "2026-11-15T10:00:00+09:00")
        self.assertFalse(st["cache_enabled"])
        self.assertTrue(st["controllable"])
        self.assertIn("has_env_id", st)
        self.assertIn("env_file", st)
        self.assertIn("id_status", st)

        # Bảo đảm tuyệt đối không rò rỉ cookie hoặc token bí mật
        dumped = json.dumps(st)
        self.assertNotIn(self.canary_psid, dumped)
        self.assertNotIn("1PSID", dumped)
        self.assertNotIn(self.canary_token, dumped)

    async def test_lane_registry_list_lanes(self):
        peers = {
            "acc2": "http://127.0.0.1:10001",
            "acc3": "http://127.0.0.1:10002",
        }
        requests = []

        async def fake_http(method, url, headers=None, body=None, timeout=2.0):
            requests.append(url)
            if "10001" in url:
                return 200, {
                    "instance_id": "acc2",
                    "instance_label": "Backend",
                    "state": "on",
                    "inflight": 1,
                    "auth_ok": True,
                    "account_status": "AVAILABLE",
                    "active_account": "B",
                    "accounts": [{"label": "B", "status": "active"}],
                    "model": "gemini-flash",
                    "total_requests": 5,
                    "total_errors": 0,
                    "uptime_seconds": 120,
                    "chat_key": "xyz98765",
                    "cache_enabled": True,
                }
            # acc3 không phản hồi / timeout
            return 0, {"error": "Connection refused"}

        reg = LaneRegistry("acc1", peers, token=self.canary_token, http_call=fake_http)
        local_st = {
            "id": "acc1", "label": "Frontend", "url": "http://127.0.0.1:10000",
            "is_self": True, "reachable": True, "control": "on", "controllable": True,
        }

        lanes = await reg.list_lanes(local_st)
        await reg.list_lanes(local_st)
        self.assertEqual(len(lanes), 3)

        # acc1 (self)
        self.assertEqual(lanes[0]["id"], "acc1")
        self.assertTrue(lanes[0]["is_self"])
        self.assertTrue(lanes[0]["reachable"])

        # acc2 (peer reachable)
        self.assertEqual(lanes[1]["id"], "acc2")
        self.assertFalse(lanes[1]["is_self"])
        self.assertTrue(lanes[1]["reachable"])
        self.assertEqual(lanes[1]["port"], 10001)
        self.assertEqual(lanes[1]["control"], "on")
        self.assertEqual(lanes[1]["model"], "gemini-flash")
        self.assertTrue(lanes[1]["cache_enabled"])
        self.assertTrue(lanes[1]["controllable"])

        # acc3 (peer down)
        self.assertEqual(lanes[2]["id"], "acc3")
        self.assertFalse(lanes[2]["is_self"])
        self.assertFalse(lanes[2]["reachable"])
        self.assertEqual(lanes[2]["port"], 10002)
        self.assertEqual(lanes[2]["control"], "off")
        self.assertIn("error", lanes[2])
        self.assertEqual(requests.count("http://127.0.0.1:10002/v1/lanes/acc3/status"), 1)

    async def test_lane_registry_list_lanes_frontend_gateway(self):
        peers = {
            "acc1": "http://127.0.0.1:10001",
            "acc2": "http://127.0.0.1:10002",
        }

        async def fake_http(method, url, headers=None, body=None, timeout=2.0):
            if "10001" in url:
                return 200, {
                    "instance_id": "acc1",
                    "instance_label": "Luong 1",
                    "state": "on",
                    "inflight": 0,
                    "auth_ok": True,
                }
            return 0, {"error": "Connection refused"}

        reg = LaneRegistry("frontend", peers, token=self.canary_token, http_call=fake_http, is_frontend_gateway=True)
        local_st = {
            "id": "frontend", "label": "Frontend Gateway", "url": "http://127.0.0.1:10000",
            "is_self": True, "reachable": True, "control": "on", "controllable": True,
        }

        lanes = await reg.list_lanes(local_st)
        # Port 10000 Frontend không nằm trong danh sách worker flows
        self.assertEqual(len(lanes), 2)
        self.assertEqual(lanes[0]["id"], "acc1")
        self.assertEqual(lanes[0]["port"], 10001)
        self.assertTrue(lanes[0]["reachable"])
        self.assertEqual(lanes[1]["id"], "acc2")
        self.assertEqual(lanes[1]["port"], 10002)
        self.assertFalse(lanes[1]["reachable"])

    async def test_off_peer_is_not_probed_until_forced_refresh_or_enable(self):
        peer_state = "off"
        calls = []

        async def fake_http(method, url, headers=None, body=None, timeout=2.0):
            nonlocal peer_state
            calls.append(method)
            if method == "POST":
                peer_state = "on"
                return 200, {"state": peer_state, "changed": True}
            return 200, {"instance_label": "Backend", "state": peer_state}

        reg = LaneRegistry(
            "acc1",
            {"acc2": "http://127.0.0.1:10001"},
            token=self.canary_token,
            http_call=fake_http,
        )
        local_st = {"id": "acc1", "reachable": True, "control": "on"}

        first = await reg.list_lanes(local_st)
        second = await reg.list_lanes(local_st)
        self.assertEqual(calls, ["GET"])
        self.assertEqual(first[1]["port"], 10001)
        self.assertEqual(second[1]["control"], "off")

        await reg.control("acc2", "enable", AsyncMock())
        await reg.list_lanes(local_st)
        self.assertEqual(calls, ["GET", "POST", "GET"])

        peer_state = "off"
        await reg.list_lanes(local_st, force_refresh=True)
        self.assertEqual(calls, ["GET", "POST", "GET", "GET"])

    async def test_lane_registry_control_self(self):
        reg = LaneRegistry("acc1", {}, token=self.canary_token)
        local_mock = AsyncMock(return_value=(200, {"changed": True, "state": "on"}))

        code, data = await reg.control("acc1", "enable", local_mock)
        self.assertEqual(code, 200)
        self.assertTrue(data["changed"])
        local_mock.assert_awaited_once_with("enable")

    async def test_lane_registry_control_peer(self):
        peers = {"acc2": "http://127.0.0.1:10001"}
        called_requests = []

        async def fake_http(method, url, headers=None, body=None, timeout=2.0):
            called_requests.append({"method": method, "url": url, "headers": headers, "timeout": timeout})
            return 200, {"changed": True, "state": "off"}

        reg = LaneRegistry("acc1", peers, token=self.canary_token, http_call=fake_http)
        local_mock = AsyncMock()

        code, data = await reg.control("acc2", "disable", local_mock)
        self.assertEqual(code, 200)
        self.assertTrue(data["changed"])
        local_mock.assert_not_awaited()

        self.assertEqual(len(called_requests), 1)
        req = called_requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["url"], "http://127.0.0.1:10001/v1/lanes/acc2/disable")
        self.assertEqual(req["headers"]["X-Lane-Token"], self.canary_token)
        self.assertGreaterEqual(req["timeout"], 120.0)

    async def test_lane_registry_control_peer_down(self):
        peers = {"acc2": "http://127.0.0.1:10001"}

        async def fake_http(method, url, headers=None, body=None, timeout=2.0):
            return 0, {"error": "Connection timed out"}

        reg = LaneRegistry("acc1", peers, token=self.canary_token, http_call=fake_http)
        code, data = await reg.control("acc2", "enable", AsyncMock())
        self.assertEqual(code, 502)
        self.assertEqual(data["error"]["code"], "peer_unreachable")

    async def test_lane_registry_control_missing_token(self):
        peers = {"acc2": "http://127.0.0.1:10001"}
        reg = LaneRegistry("acc1", peers, token="", http_call=AsyncMock())
        code, data = await reg.control("acc2", "enable", AsyncMock())
        self.assertEqual(code, 403)
        self.assertEqual(data["error"]["code"], "lane_not_controllable")

    async def test_lane_registry_control_unknown_lane(self):
        peers = {"acc2": "http://127.0.0.1:10001"}
        reg = LaneRegistry("acc1", peers, token=self.canary_token, http_call=AsyncMock())
        code, data = await reg.control("acc99", "enable", AsyncMock())
        self.assertEqual(code, 404)
        self.assertEqual(data["error"]["code"], "lane_not_found")

    async def test_main_lane_endpoints_and_activity(self):
        from main import (
            app,
            lane_enable,
            lane_disable,
            list_lanes,
            lane_registry,
            activity,
        )

        # Mock lane_registry
        with patch.object(lane_registry, "list_lanes", new_callable=AsyncMock) as mock_list, \
             patch.object(lane_registry, "control", new_callable=AsyncMock) as mock_ctrl, \
             patch.object(activity, "write") as mock_act:

            mock_list.return_value = [{"id": "acc1", "reachable": True}]
            mock_ctrl.return_value = (200, {"changed": True, "state": "on"})

            # 1. GET /v1/lanes
            res_lanes = await list_lanes()
            self.assertIn("self", res_lanes)
            self.assertEqual(res_lanes["lanes"], [{"id": "acc1", "reachable": True}])

            await list_lanes(refresh=True)
            mock_list.assert_awaited_with(unittest.mock.ANY, force_refresh=True)

            # 2. POST /v1/lanes/acc1/enable
            req = Request({"type": "http", "headers": []})
            resp_en = await lane_enable("acc1", req)
            self.assertEqual(resp_en.status_code, 200)
            mock_ctrl.assert_awaited_with("acc1", "enable", unittest.mock.ANY)
            mock_act.assert_called_with(
                event="lane_control",
                lane_id="acc1",
                action="enable",
                status=200,
                outcome="ok",
            )

            # 3. POST /v1/lanes/acc2/disable
            mock_ctrl.return_value = (200, {"changed": True, "state": "off"})
            resp_dis = await lane_disable("acc2", req)
            self.assertEqual(resp_dis.status_code, 200)
            mock_ctrl.assert_awaited_with("acc2", "disable", unittest.mock.ANY)
            mock_act.assert_called_with(
                event="lane_control",
                lane_id="acc2",
                action="disable",
                status=200,
                outcome="ok",
            )

    async def test_lane_registry_proxy_request(self):
        peers = {"acc2": "http://127.0.0.1:10001"}
        calls = []

        async def fake_http(method, url, headers=None, body=None, timeout=5.0):
            calls.append({"method": method, "url": url, "headers": headers, "body": body})
            return 200, {"logs": ["log 1 from acc2"]}

        reg = LaneRegistry("acc1", peers, token=self.canary_token, http_call=fake_http)

        # 1. Proxy GET logs
        code, data = await reg.proxy_request("acc2", "GET", "logs", query="limit=20")
        self.assertEqual(code, 200)
        self.assertEqual(data["logs"], ["log 1 from acc2"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["url"], "http://127.0.0.1:10001/v1/lanes/acc2/logs?limit=20")
        self.assertEqual(calls[0]["headers"]["X-Lane-Token"], self.canary_token)

        # 2. Proxy tới unknown lane -> 404
        code_404, _ = await reg.proxy_request("acc99", "GET", "logs")
        self.assertEqual(code_404, 404)

        # 3. Proxy tới chính mình -> 400
        code_self, _ = await reg.proxy_request("acc1", "GET", "logs")
        self.assertEqual(code_self, 400)

    async def test_failed_peer_proxy_is_cached_without_followup_probe(self):
        calls = []

        async def fake_http(method, url, headers=None, body=None, timeout=5.0):
            calls.append(url)
            return 0, {"error": "Connection refused"}

        reg = LaneRegistry(
            "acc1",
            {"acc2": "http://127.0.0.1:10001"},
            token=self.canary_token,
            http_call=fake_http,
        )

        status_code, _ = await reg.proxy_request("acc2", "GET", "logs")
        lanes = await reg.list_lanes({"id": "acc1", "reachable": True, "control": "on"})

        self.assertEqual(status_code, 502)
        self.assertFalse(lanes[1]["reachable"])
        self.assertEqual(len(calls), 1)

    async def test_local_switch_model_validation(self):
        from main import _local_switch_model, session_manager

        # Case 1: Luồng đang OFF -> 409 lane_not_ready
        session_manager.state = "off"
        session_manager.auth_ok = True
        code, err = await _local_switch_model("gemini-pro")
        self.assertEqual(code, 409)
        self.assertEqual(err["error"]["code"], "lane_not_ready")

        # Case 2: Luồng ON nhưng chưa đăng nhập (auth_ok=False) -> 409 lane_not_ready
        session_manager.state = "on"
        session_manager.auth_ok = False
        code, err = await _local_switch_model("gemini-pro")
        self.assertEqual(code, 409)
        self.assertEqual(err["error"]["code"], "lane_not_ready")

        # Case 3: Luồng ON và auth_ok=True, nhưng gửi model không tồn tại -> 400 model_not_available
        session_manager.state = "on"
        session_manager.auth_ok = True
        session_manager.available_models = ["unspecified", "gemini-pro", "gemini-flash"]
        session_manager.default_model = "gemini-pro"

        # Mock resolve_model ném exception
        session_manager.client = MagicMock()
        session_manager.client.resolve_model.side_effect = Exception("Not found")

        code, err = await _local_switch_model("nonexistent-model-xyz")
        self.assertEqual(code, 400)
        self.assertEqual(err["error"]["code"], "model_not_available")
        self.assertIn("nonexistent-model-xyz", err["error"]["message"])
        # Bridge KHÔNG đổi model
        self.assertEqual(session_manager.default_model, "gemini-pro")

        # Case 4: Đổi model hợp lệ -> 200 và default_model đổi
        with patch.object(session_manager, "switch_model", new_callable=AsyncMock) as mock_sw:
            mock_sw.return_value = "gemini-flash"
            code, res = await _local_switch_model("gemini-flash")
            self.assertEqual(code, 200)
            self.assertEqual(res["active_model"], "gemini-flash")

    async def test_lane_models_format(self):
        from main import _local_lane_models, session_manager

        session_manager.default_model = "gemini-flash"
        session_manager.available_models = ["unspecified", "gemini-flash"]
        session_manager.model_details = [
            {"name": "gemini-flash", "display_name": "Gemini 1.5 Flash"}
        ]

        models_data = _local_lane_models()
        self.assertEqual(models_data["current"], "gemini-flash")
        avail = models_data["available"]
        self.assertEqual(len(avail), 2)
        # unspecified phải có nhãn "Mặc định (unspecified)"
        self.assertEqual(avail[0], {"id": "unspecified", "label": "Mặc định (unspecified)"})
        self.assertEqual(avail[1], {"id": "gemini-flash", "label": "Gemini 1.5 Flash"})

    async def test_lane_proxy_endpoints(self):
        from main import (
            lane_logs,
            lane_activity,
            lane_models,
            lane_switch_model,
            lane_reset,
            lane_cache_clear,
            lane_registry,
        )

        with patch.object(lane_registry, "proxy_request", new_callable=AsyncMock) as mock_proxy, \
             patch("main.LANE_ADMIN_TOKEN", "test-token"):

            mock_proxy.return_value = (200, {"logs": ["remote log"]})

            # 1. GET /v1/lanes/acc2/logs
            resp_logs = await lane_logs("acc2", limit=30)
            self.assertEqual(resp_logs.status_code, 200)
            mock_proxy.assert_awaited_with("acc2", "GET", "logs", query="limit=30", timeout=5.0)

            # 2. GET /v1/lanes/acc2/activity
            mock_proxy.return_value = (200, {"instance": "acc2", "entries": []})
            resp_act = await lane_activity("acc2", limit=15)
            self.assertEqual(resp_act.status_code, 200)
            mock_proxy.assert_awaited_with("acc2", "GET", "activity", query="limit=15", timeout=5.0)

            # 3. GET /v1/lanes/acc2/models
            mock_proxy.return_value = (200, {"current": "gemini-pro", "available": []})
            resp_mod = await lane_models("acc2")
            self.assertEqual(resp_mod.status_code, 200)
            mock_proxy.assert_awaited_with("acc2", "GET", "models", timeout=5.0)

            # 4. POST /v1/lanes/acc2/switch-model
            mock_proxy.return_value = (200, {"status": "ok", "active_model": "gemini-pro"})
            req_sw = Request({"type": "http", "headers": []})
            req_sw._json = {"model": "gemini-pro"}
            resp_sw = await lane_switch_model("acc2", req_sw)
            self.assertEqual(resp_sw.status_code, 200)

            # 5. POST /v1/lanes/acc2/reset
            mock_proxy.return_value = (200, {"status": "ok"})
            req_rst = Request({"type": "http", "headers": []})
            resp_rst = await lane_reset("acc2", req_rst)
            self.assertEqual(resp_rst.status_code, 200)

            # 6. POST /v1/lanes/acc2/cache/clear
            mock_proxy.return_value = (200, {"status": "ok", "cleared_entries": 3})
            req_clr = Request({"type": "http", "headers": []})
            resp_clr = await lane_cache_clear("acc2", req_clr)
            self.assertEqual(resp_clr.status_code, 200)

    def test_check_env_file_id(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            # Luồng acc1 có SECURE_1PSID
            (tmppath / ".env.acc1").write_text("SECURE_1PSID=abcdefghijklmnop123456\nPORT=10001", encoding="utf-8")
            has_id, env_f = check_env_file_id("acc1", base_dir=tmppath)
            self.assertTrue(has_id)
            self.assertEqual(env_f, ".env.acc1")

            # Luồng acc2 thiếu SECURE_1PSID
            (tmppath / ".env.acc2").write_text("PORT=10002", encoding="utf-8")
            has_id2, env_f2 = check_env_file_id("acc2", base_dir=tmppath)
            self.assertFalse(has_id2)
            self.assertEqual(env_f2, ".env.acc2")

            # Luồng không tồn tại file
            has_id3, env_f3 = check_env_file_id("nonexistent", base_dir=tmppath)
            self.assertFalse(has_id3)
            self.assertEqual(env_f3, ".env.nonexistent")



if __name__ == "__main__":
    unittest.main()
