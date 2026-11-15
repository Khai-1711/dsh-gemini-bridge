import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Đảm bảo pop BRIDGE_INSTANCE trước khi import
os.environ.pop("BRIDGE_INSTANCE", None)
os.environ.setdefault("SECURE_1PSID", "canary_mock_psid_init_12345")
os.environ.setdefault("SECURE_1PSIDTS", "canary_mock_psidts_init_67890")

from app.env_update import (
    is_valid_cookie_value,
    is_valid_label,
    update_env_values,
)
from starlette.requests import Request


class TestEnvUpdatePure(unittest.TestCase):
    def test_is_valid_cookie_value(self):
        # Hợp lệ
        self.assertTrue(is_valid_cookie_value("abcXYZ123._-=+/%~"))
        self.assertTrue(is_valid_cookie_value("g.a.some_complex-cookie+value=="))

        # Không hợp lệ: rỗng, khoảng trắng, nháy kép, nháy đơn, xuống dòng, ký tự đặc biệt lạ
        self.assertFalse(is_valid_cookie_value(""))
        self.assertFalse(is_valid_cookie_value(None))
        self.assertFalse(is_valid_cookie_value("cookie with spaces"))
        self.assertFalse(is_valid_cookie_value('cookie"with"quotes'))
        self.assertFalse(is_valid_cookie_value("cookie'single'quotes"))
        self.assertFalse(is_valid_cookie_value("cookie\nnewline"))
        self.assertFalse(is_valid_cookie_value("cookie$with$dollar"))
        self.assertFalse(is_valid_cookie_value("cookie;with;semicolon"))

    def test_is_valid_label(self):
        # Hợp lệ: nhãn tiếng Việt, tiếng Anh, số, dấu cách
        self.assertTrue(is_valid_label("Tài khoản chính"))
        self.assertTrue(is_valid_label("acc_backup-1"))
        self.assertTrue(is_valid_label("A"))

        # Không hợp lệ: rỗng, nháy kép, gạch chéo ngược, xuống dòng, quá 40 ký tự
        self.assertFalse(is_valid_label(""))
        self.assertFalse(is_valid_label(None))
        self.assertFalse(is_valid_label('nhãn"ngoặc'))
        self.assertFalse(is_valid_label("nhãn\\backslash"))
        self.assertFalse(is_valid_label("nhãn\nxuống dòng"))
        self.assertFalse(is_valid_label("a" * 41))

    def test_update_env_values_preserves_comments_and_order(self):
        original = (
            "# Cấu hình bridge\n"
            'PORT="10000"\n'
            'INSTANCE_LABEL="Frontend"\n'
            "# Cookie chính\n"
            'SECURE_1PSID="old_psid_val"\n'
            'SECURE_1PSIDTS="old_psidts_val"\n'
            "\n"
            "# Hết file\n"
        )
        updates = {
            "SECURE_1PSID": "new_psid_123",
            "SECURE_1PSIDTS": "new_psidts_456",
        }

        result = update_env_values(original, updates)

        # Bảo toàn thứ tự và các dòng comment
        self.assertIn("# Cấu hình bridge", result)
        self.assertIn('PORT="10000"', result)
        self.assertIn('INSTANCE_LABEL="Frontend"', result)
        self.assertIn("# Cookie chính", result)
        self.assertIn('SECURE_1PSID="new_psid_123"', result)
        self.assertIn('SECURE_1PSIDTS="new_psidts_456"', result)
        self.assertIn("# Hết file", result)
        self.assertNotIn("old_psid_val", result)
        self.assertNotIn("old_psidts_val", result)

    def test_update_env_values_replaces_all_occurrences_and_adds_missing(self):
        original = (
            'SECURE_1PSID="old1"\n'
            'SECURE_1PSID="old2"\n'
        )
        updates = {
            "SECURE_1PSID": "new_psid",
            "BACKUP_1PSID": "backup_psid",
        }

        result = update_env_values(original, updates)
        lines = result.strip().splitlines()
        self.assertEqual(lines[0], 'SECURE_1PSID="new_psid"')
        self.assertEqual(lines[1], 'SECURE_1PSID="new_psid"')
        self.assertEqual(lines[2], 'BACKUP_1PSID="backup_psid"')


class TestEnvUpdateEndpoints(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.env_file = Path(self.td.name) / ".env.test"
        self.env_file.write_text(
            '# File .env mẫu\nPORT="10000"\nSECURE_1PSID="canary_init_psid"\nSECURE_1PSIDTS="canary_init_psidts"\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.td.cleanup()

    async def test_local_update_cookies_success_and_atomic_persist(self):
        from main import _local_update_cookies, session_manager

        canary_new_psid = "canary_brand_new_psid_9999"
        canary_new_psidts = "canary_brand_new_psidts_8888"

        with patch("main.INSTANCE_PATHS", {"env_file": self.env_file}):
            # Giả lập luồng đang off để nạp cookie an toàn không gọi mạng
            session_manager.state = "off"
            code, data = await _local_update_cookies(0, canary_new_psid, canary_new_psidts)

            self.assertEqual(code, 200)
            self.assertTrue(data["ok"])
            self.assertTrue(data["persisted"])
            self.assertIn("account_key", data)

            # Kiểm tra file .env đã được ghi bền nguyên tử
            content = self.env_file.read_text(encoding="utf-8")
            self.assertIn(f'SECURE_1PSID="{canary_new_psid}"', content)
            self.assertIn(f'SECURE_1PSIDTS="{canary_new_psidts}"', content)
            self.assertIn('PORT="10000"', content)
            self.assertIn("# File .env mẫu", content)

            # Phản hồi tuyệt đối không chứa cookie thô
            dumped = json.dumps(data)
            self.assertNotIn(canary_new_psid, dumped)
            self.assertNotIn(canary_new_psidts, dumped)

    async def test_local_update_cookies_invalid_format_rejected(self):
        from main import _local_update_cookies

        with patch("main.INSTANCE_PATHS", {"env_file": self.env_file}):
            # Ký tự lạ (nháy kép) -> 400
            code, data = await _local_update_cookies(0, 'invalid"psid', "valid_psidts")
            self.assertEqual(code, 400)
            self.assertEqual(data["error"]["code"], "invalid_cookie_format")

            # File không bị sửa
            content = self.env_file.read_text(encoding="utf-8")
            self.assertNotIn('invalid"psid', content)

    async def test_duplicate_cookie_rejected(self):
        from main import _local_update_cookies, session_manager

        with patch("main.INSTANCE_PATHS", {"env_file": self.env_file}):
            session_manager.state = "off"
            psid_1 = "duplicate_test_psid_unique_1"
            psidts_1 = "duplicate_test_psidts_unique_1"

            # Nạp vào slot 0
            code1, _ = await _local_update_cookies(0, psid_1, psidts_1)
            self.assertEqual(code1, 200)

            # Cố nạp cùng PSID đó vào slot 1 -> 400 duplicate
            code2, data2 = await _local_update_cookies(1, psid_1, "different_psidts")
            self.assertEqual(code2, 400)
            self.assertEqual(data2["error"]["code"], "invalid_cookie_request")
            self.assertIn("trùng với ô tài khoản slot 0", data2["error"]["message"])

    async def test_slot_gap_rejected(self):
        from main import _local_update_cookies, session_manager

        with patch("main.INSTANCE_PATHS", {"env_file": self.env_file}):
            session_manager.state = "off"
            # Chuỗi chỉ có slot 0, cố nạp vào slot 3 (nhảy cóc) -> 400
            code, data = await _local_update_cookies(3, "valid_psid_slot3", "valid_psidts_slot3")
            self.assertEqual(code, 400)
            self.assertIn("Chưa có slot", data["error"]["message"])

    async def test_proxy_cookie_lan_forbidden(self):
        from main import lane_account_cookies, lane_registry

        # Peer chạy trên IP LAN (192.168.1.5) -> HTTP 403 cookie_proxy_forbidden
        with patch.object(lane_registry, "peers", {"acc2": "http://192.168.1.5:10001"}), \
             patch("main.LANE_ADMIN_TOKEN", "test-token"):

            req = Request({"type": "http", "headers": []})
            req._json = {"psid": "valid_psid", "psidts": "valid_psidts"}
            resp = await lane_account_cookies("acc2", 0, req)

            self.assertEqual(resp.status_code, 403)
            body = json.loads(resp.body)
            self.assertEqual(body["error"]["code"], "cookie_proxy_forbidden")
            self.assertIn("không gửi cookie qua mạng", body["error"]["message"])

    async def test_proxy_cookie_loopback_allowed(self):
        from main import lane_account_cookies, lane_registry

        # Peer chạy trên Loopback (127.0.0.1) -> Cho phép proxy
        with patch.object(lane_registry, "peers", {"acc2": "http://127.0.0.1:10001"}), \
             patch.object(lane_registry, "proxy_request", new_callable=AsyncMock) as mock_proxy, \
             patch("main.LANE_ADMIN_TOKEN", "test-token"):

            mock_proxy.return_value = (200, {"ok": True, "persisted": True})

            req = Request({"type": "http", "headers": []})
            req._json = {"psid": "valid_psid", "psidts": "valid_psidts"}
            resp = await lane_account_cookies("acc2", 0, req)

            self.assertEqual(resp.status_code, 200)
            mock_proxy.assert_awaited_with(
                "acc2",
                "POST",
                "accounts/0/cookies",
                body=unittest.mock.ANY,
                timeout=30.0,
            )


if __name__ == "__main__":
    unittest.main()
