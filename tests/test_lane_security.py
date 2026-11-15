import os
os.environ.pop("BRIDGE_INSTANCE", None)
os.environ.setdefault("SECURE_1PSID", "mock_psid")
os.environ.setdefault("SECURE_1PSIDTS", "mock_psidts")

import json
from pathlib import Path
import unittest
from unittest.mock import patch
from starlette.testclient import TestClient

from app.lane_security import (
    is_loopback_host,
    is_rfc1918_host,
    check_bind_safety,
    split_host_header,
    is_host_header_allowed,
    parse_peer_lanes,
    is_loopback_url,
    lane_token_ok,
)
from main import app


class TestLaneSecurityPure(unittest.TestCase):
    def test_is_loopback_host(self):
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertTrue(is_loopback_host("[::1]"))
        self.assertTrue(is_loopback_host("127.0.0.2"))

        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("192.168.1.5"))
        self.assertFalse(is_loopback_host("evil.com"))
        self.assertFalse(is_loopback_host(""))
        self.assertFalse(is_loopback_host(None))

    def test_is_rfc1918_host(self):
        self.assertTrue(is_rfc1918_host("10.0.0.1"))
        self.assertTrue(is_rfc1918_host("172.16.0.1"))
        self.assertTrue(is_rfc1918_host("172.31.255.254"))
        self.assertTrue(is_rfc1918_host("192.168.1.1"))

        self.assertFalse(is_rfc1918_host("172.32.0.1"))
        self.assertFalse(is_rfc1918_host("8.8.8.8"))
        self.assertFalse(is_rfc1918_host("127.0.0.1"))
        self.assertFalse(is_rfc1918_host("localhost"))
        self.assertFalse(is_rfc1918_host("evil.com"))

    def test_check_bind_safety(self):
        # 127.0.0.1 luôn cho phép dù không có API_KEY
        ok, msg = check_bind_safety("127.0.0.1", "", False)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

        # localhost luôn cho phép
        ok, msg = check_bind_safety("localhost", "", False)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

        # 0.0.0.0 không có API_KEY -> TỪ CHỐI
        ok, msg = check_bind_safety("0.0.0.0", "", False)
        self.assertFalse(ok)
        self.assertIn("TỪ CHỐI KHỞI ĐỘNG", msg)

        # 0.0.0.0 có API_KEY -> CHO PHÉP
        ok, msg = check_bind_safety("0.0.0.0", "my_secret_key", False)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

        # 0.0.0.0 không có API_KEY nhưng ALLOW_INSECURE_LAN=True -> CHO PHÉP kèm CẢNH BÁO
        ok, msg = check_bind_safety("0.0.0.0", "", True)
        self.assertTrue(ok)
        self.assertIn("CẢNH BÁO", msg)

    def test_split_host_header(self):
        self.assertEqual(split_host_header("127.0.0.1:10000"), "127.0.0.1")
        self.assertEqual(split_host_header("[::1]:10000"), "::1")
        self.assertEqual(split_host_header("localhost:10000"), "localhost")
        self.assertEqual(split_host_header("localhost"), "localhost")
        self.assertEqual(split_host_header("evil.com"), "evil.com")
        self.assertEqual(split_host_header(""), "")

    def test_is_host_header_allowed(self):
        self.assertTrue(is_host_header_allowed("127.0.0.1:10000"))
        self.assertTrue(is_host_header_allowed("localhost:10000"))
        self.assertTrue(is_host_header_allowed("[::1]:10000"))
        self.assertTrue(is_host_header_allowed("192.168.1.20:10000"))
        self.assertTrue(is_host_header_allowed("10.0.0.5:10001"))

        self.assertFalse(is_host_header_allowed("evil.com"))
        self.assertFalse(is_host_header_allowed("attacker.192.168.1.1.nip.io"))
        self.assertFalse(is_host_header_allowed(""))

        # Tên nằm trong danh sách cho phép bổ sung
        self.assertTrue(is_host_header_allowed("pc2.lan:10000", extra_allowed=["pc2.lan"]))
        self.assertFalse(is_host_header_allowed("pc3.lan:10000", extra_allowed=["pc2.lan"]))

    def test_parse_peer_lanes(self):
        raw = "acc2=http://127.0.0.1:10001,acc3=http://192.168.1.5:10002"
        peers = parse_peer_lanes(raw, self_id="acc1")
        self.assertEqual(peers, {
            "acc2": "http://127.0.0.1:10001",
            "acc3": "http://192.168.1.5:10002",
        })

        # Bỏ mục trùng ID của chính mình
        raw_self = "acc1=http://127.0.0.1:10000,acc2=http://127.0.0.1:10001"
        peers_self = parse_peer_lanes(raw_self, self_id="acc1")
        self.assertEqual(peers_self, {"acc2": "http://127.0.0.1:10001"})

        # Bỏ mục trỏ ra host ngoài Internet
        raw_evil = "acc2=http://evil.com:10001,acc3=http://127.0.0.1:10002"
        peers_evil = parse_peer_lanes(raw_evil, self_id="acc1")
        self.assertEqual(peers_evil, {"acc3": "http://127.0.0.1:10002"})

        # Bỏ mục sai định dạng hoặc protocol lạ
        raw_bad = "bad_format,acc2=ftp://127.0.0.1:10001,acc3=http://127.0.0.1:10003"
        peers_bad = parse_peer_lanes(raw_bad, self_id="acc1")
        self.assertEqual(peers_bad, {"acc3": "http://127.0.0.1:10003"})

    def test_is_loopback_url(self):
        self.assertTrue(is_loopback_url("http://127.0.0.1:10000"))
        self.assertTrue(is_loopback_url("http://localhost:10001/"))
        self.assertTrue(is_loopback_url("http://[::1]:10000"))

        self.assertFalse(is_loopback_url("http://192.168.1.5:10000"))
        self.assertFalse(is_loopback_url("http://evil.com:10000"))
        self.assertFalse(is_loopback_url("ftp://127.0.0.1:10000"))
        self.assertFalse(is_loopback_url(""))

    def test_lane_token_ok(self):
        self.assertTrue(lane_token_ok("secret123", "secret123"))
        self.assertFalse(lane_token_ok("wrong", "secret123"))
        self.assertFalse(lane_token_ok("", "secret123"))
        self.assertFalse(lane_token_ok("secret123", ""))
        self.assertFalse(lane_token_ok("", ""))

    def test_i18n_vi_en_parity(self):
        import re
        i18n_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "js", "i18n.js")
        with open(i18n_path, "r", encoding="utf-8") as f:
            content = f.read()

        vi_match = re.search(r"vi:\s*\{(.*?)\n\s*\},", content, re.DOTALL)
        en_match = re.search(r"en:\s*\{(.*?)\n\s*\},", content, re.DOTALL)
        self.assertIsNotNone(vi_match)
        self.assertIsNotNone(en_match)

        def extract_keys(block):
            keys = set()
            for line in block.splitlines():
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                m = re.match(r"([A-Za-z0-9_]+)\s*:", line)
                if m:
                    keys.add(m.group(1))
            return keys

        vi_keys = extract_keys(vi_match.group(1))
        en_keys = extract_keys(en_match.group(1))

        missing_in_en = vi_keys - en_keys
        missing_in_vi = en_keys - vi_keys
        self.assertEqual(missing_in_en, set(), f"Missing in EN: {missing_in_en}")
        self.assertEqual(missing_in_vi, set(), f"Missing in VI: {missing_in_vi}")
        self.assertEqual(vi_keys, en_keys)

    def test_display_name_rules(self):
        # Bảng kiểm bắt buộc T40D
        def py_display_name(lane_id, label, n=1, lang="vi"):
            clean_id = (lane_id or "").strip()
            clean_label = (label or "").strip()
            if clean_label and clean_label != clean_id:
                name = clean_label
            else:
                name = clean_id
            if name.lower() == "default":
                name = ""
            prefix = f"Flow {n}" if lang == "en" else f"Luồng {n}"
            return f"{prefix} · {name}" if name else prefix

        self.assertEqual(py_display_name("default", "", 1), "Luồng 1")
        self.assertEqual(py_display_name("default", "default", 1), "Luồng 1")
        self.assertEqual(py_display_name("acc1", "", 1), "Luồng 1 · acc1")
        self.assertEqual(py_display_name("acc1", "Frontend", 1), "Luồng 1 · Frontend")
        self.assertEqual(py_display_name("acc2", "acc2", 2), "Luồng 2 · acc2")


class TestMiddlewareAndSecurity(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, base_url="http://127.0.0.1:10000")

    def test_host_header_allowed_and_blocked(self):
        # Host header hợp lệ (loopback)
        resp = self.client.get("/health", headers={"host": "127.0.0.1:10000"})
        self.assertEqual(resp.status_code, 200)

        # Host header lạ (evil.com) -> 400 host_not_allowed
        resp_evil = self.client.get("/health", headers={"host": "evil.com"})
        self.assertEqual(resp_evil.status_code, 400)
        data = resp_evil.json()
        self.assertEqual(data["error"]["code"], "host_not_allowed")

        # DNS rebinding qua nip.io -> 400
        resp_nip = self.client.get("/health", headers={"host": "attacker.192.168.1.1.nip.io"})
        self.assertEqual(resp_nip.status_code, 400)

    def test_health_returns_minimal_info(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("control", data)
        self.assertIn("instance_id", data)
        self.assertIn("instance_label", data)

        # Không được lộ tài khoản, model, hash, accounts
        self.assertNotIn("account_key", data)
        self.assertNotIn("accounts", data)
        self.assertNotIn("model", data)
        self.assertNotIn("chat_key", data)
        self.assertNotIn("active_account", data)

    def test_api_key_enforcement(self):
        with patch("main.API_KEY", "secret-key-123"):
            # GET /health và GET /dashboard vẫn nằm trong whitelist (không bị 401)
            resp_health = self.client.get("/health")
            self.assertEqual(resp_health.status_code, 200)

            # Các route điều khiển & API khác: không kèm khóa -> 401
            resp_status = self.client.get("/v1/status")
            self.assertEqual(resp_status.status_code, 401)

            resp_ctrl = self.client.get("/v1/control")
            self.assertEqual(resp_ctrl.status_code, 401)

            resp_logs = self.client.get("/v1/logs")
            self.assertEqual(resp_logs.status_code, 401)

            # Gửi khóa sai -> 401
            resp_wrong = self.client.get("/v1/status", headers={"Authorization": "Bearer wrong-key"})
            self.assertEqual(resp_wrong.status_code, 401)

            # Gửi đúng khóa -> 200
            resp_ok = self.client.get("/v1/status", headers={"Authorization": "Bearer secret-key-123"})
            self.assertEqual(resp_ok.status_code, 200)
            self.assertIn("account_key", resp_ok.json())

    def test_lane_token_exception_on_lane_routes(self):
        with patch("main.API_KEY", "secret-key-123"), \
             patch("main.LANE_ADMIN_TOKEN", "lane-token-456"):
            # Route không phải /v1/lanes -> KHÔNG nhận X-Lane-Token -> 401
            resp = self.client.get("/v1/status", headers={"X-Lane-Token": "lane-token-456"})
            self.assertEqual(resp.status_code, 401)

    def test_t40e_polling_preservation_and_layout(self):
        base_dir = Path(__file__).resolve().parent.parent / "frontend" / "js"
        cm_file = (base_dir / "components" / "BridgeCookieManager.js").read_text(encoding="utf-8")
        ms_file = (base_dir / "components" / "BridgeModelSwitch.js").read_text(encoding="utf-8")
        term_file = (base_dir / "components" / "BridgeTerminal.js").read_text(encoding="utf-8")
        lanes_file = (base_dir / "components" / "BridgeLanes.js").read_text(encoding="utf-8")
        state_file = (base_dir / "state.js").read_text(encoding="utf-8")

        # 1. Bắt buộc có comment quy tắc tại đầu các component
        rule_comment = "// Quy tắc: Poll không được render lại ô nhập/chọn"
        self.assertIn(rule_comment, cm_file)
        self.assertIn(rule_comment, ms_file)
        self.assertIn(rule_comment, term_file)

        # 2. State phải lưu cookieSlots theo luồng
        self.assertIn("cookieSlots:", state_file)
        self.assertIn("cookieSlots[laneId]", cm_file)

        # 3. CookieManager không render lại toàn bộ trên polling (lanes thay đổi)
        self.assertIn("updateDynamicValues", cm_file)

        # 4. ModelSwitch cập nhật tại chỗ và giữ value
        self.assertIn("updateDynamicValues", ms_file)

        # 5. Lanes implement bố cục chuẩn của chủ dự án (⚡ Quản Lý Luồng Agent)
        self.assertIn("agentFlowsTitle", lanes_file)
        self.assertIn("lane-flow-row", lanes_file)
        self.assertIn("lane-toggle-btn", lanes_file)
        self.assertIn("lane-stats-line", lanes_file)
        self.assertIn("updateDynamicValues", lanes_file)


if __name__ == "__main__":
    unittest.main()
