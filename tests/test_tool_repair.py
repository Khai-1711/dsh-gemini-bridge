import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import tool_repair


class TestToolRepairShadow(unittest.IsolatedAsyncioTestCase):
    endpoint = "http://127.0.0.1:8000/v1/chat/completions"
    candidate = (
        '<tool_call>{"name":"Inspect","arguments":{"format":"summary" '
        '"max_items":3}}</tool_call>'
    )
    tools = {
        "Inspect": {
            "parameters": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["summary"]},
                    "max_items": {"type": "integer"},
                },
                "required": ["format", "max_items"],
                "additionalProperties": False,
            }
        }
    }

    def _transport(self, output=None, busy=False, health_status=200, delay=0):
        requests = []

        async def handler(request):
            requests.append((request.method, request.url.path))
            if request.url.path == "/health":
                return httpx.Response(health_status)
            if request.url.path == "/slots":
                return httpx.Response(
                    200,
                    json=[{"id": 0, "is_processing": busy}],
                )
            if delay:
                await asyncio.sleep(delay)
            if output is None:
                return httpx.Response(500)
            body = json.loads(request.content)
            self.assertEqual(body["model"], "test-model")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": output}}]},
            )

        return httpx.MockTransport(handler), requests

    async def _evaluate(self, output, **transport_options):
        transport, requests = self._transport(output, **transport_options)
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", True), patch.object(
            tool_repair, "TOOL_REPAIR_ENDPOINT", self.endpoint
        ), patch.object(tool_repair, "TOOL_REPAIR_MODEL", "test-model"), patch.object(
            tool_repair, "TOOL_REPAIR_TIMEOUT_SECONDS", 0.5
        ):
            result = await tool_repair.evaluate_tool_repair_shadow(
                self.candidate,
                self.tools,
                request_id="test-request",
                transport=transport,
            )
        return result, requests

    async def test_syntax_only_repair_is_schema_checked_and_never_executed(self):
        output = (
            '{"name":"Inspect","arguments":'
            '{"format":"summary","max_items":3}}'
        )
        result, requests = await self._evaluate(output)

        self.assertEqual(result.outcome, "valid_repair")
        self.assertEqual(
            requests,
            [
                ("GET", "/health"),
                ("GET", "/slots"),
                ("POST", "/v1/chat/completions"),
            ],
        )

    async def test_changed_argument_values_are_rejected_from_shadow_success(self):
        output = (
            '{"name":"Inspect","arguments":'
            '{"format":"summary","max_items":4}}'
        )
        result, _ = await self._evaluate(output)

        self.assertEqual(result.outcome, "changed_keys_or_values")

    async def test_hallucinated_tool_name_is_rejected(self):
        output = '{"name":"RunCommand","arguments":{"command":"whoami"}}'
        result, _ = await self._evaluate(output)

        self.assertEqual(result.outcome, "changed_tool_name")

    async def test_invalid_model_json_is_rejected(self):
        result, _ = await self._evaluate("not json")

        self.assertEqual(result.outcome, "invalid_model_json")

    async def test_missing_required_argument_is_rejected_by_schema(self):
        output = '{"name":"Inspect","arguments":{"format":"summary"}}'
        result, _ = await self._evaluate(output)

        self.assertEqual(result.outcome, "schema_rejected")

    async def test_busy_or_unready_model_is_not_sent_a_repair_request(self):
        busy_transport, busy_requests = self._transport(
            '{"name":"Inspect","arguments":{"format":"summary","max_items":3}}',
            busy=True,
        )
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", True), patch.object(
            tool_repair, "TOOL_REPAIR_ENDPOINT", self.endpoint
        ), patch.object(tool_repair, "TOOL_REPAIR_MODEL", "test-model"):
            result = await tool_repair.evaluate_tool_repair_shadow(
                self.candidate, self.tools, transport=busy_transport
            )
        self.assertEqual(result.outcome, "model_busy")
        self.assertNotIn(("POST", "/v1/chat/completions"), busy_requests)

        unready_transport, unready_requests = self._transport(
            '{"name":"Inspect","arguments":{"format":"summary","max_items":3}}',
            health_status=503,
        )
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", True), patch.object(
            tool_repair, "TOOL_REPAIR_ENDPOINT", self.endpoint
        ), patch.object(tool_repair, "TOOL_REPAIR_MODEL", "test-model"):
            result = await tool_repair.evaluate_tool_repair_shadow(
                self.candidate, self.tools, transport=unready_transport
            )
        self.assertEqual(result.outcome, "model_not_ready")
        self.assertNotIn(("POST", "/v1/chat/completions"), unready_requests)

    async def test_timeout_does_not_retry(self):
        transport, requests = self._transport(
            '{"name":"Inspect","arguments":{"format":"summary","max_items":3}}',
            delay=0.1,
        )
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", True), patch.object(
            tool_repair, "TOOL_REPAIR_ENDPOINT", self.endpoint
        ), patch.object(tool_repair, "TOOL_REPAIR_MODEL", "test-model"), patch.object(
            tool_repair, "TOOL_REPAIR_TIMEOUT_SECONDS", 0.01
        ):
            result = await tool_repair.evaluate_tool_repair_shadow(
                self.candidate, self.tools, transport=transport
            )

        self.assertEqual(result.outcome, "timeout")
        self.assertEqual(
            requests.count(("POST", "/v1/chat/completions")),
            1,
        )

    async def test_transport_exception_is_reported_without_post_retry(self):
        requests = []

        async def handler(request):
            requests.append((request.method, request.url.path))
            raise httpx.ConnectError("offline", request=request)

        transport = httpx.MockTransport(handler)
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", True), patch.object(
            tool_repair, "TOOL_REPAIR_ENDPOINT", self.endpoint
        ), patch.object(tool_repair, "TOOL_REPAIR_MODEL", "test-model"):
            result = await tool_repair.evaluate_tool_repair_shadow(
                self.candidate, self.tools, transport=transport
            )

        self.assertEqual(result.outcome, "http_error")
        self.assertEqual(requests, [("GET", "/health")])

    async def test_prose_and_protected_paths_never_contact_local_ai(self):
        transport, requests = self._transport(
            '{"name":"Inspect","arguments":{"format":"summary","max_items":3}}'
        )
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", True), patch.object(
            tool_repair, "TOOL_REPAIR_ENDPOINT", self.endpoint
        ), patch.object(tool_repair, "TOOL_REPAIR_MODEL", "test-model"):
            prose_result = await tool_repair.evaluate_tool_repair_shadow(
                "Please call Inspect to summarize.", self.tools, transport=transport
            )
            path_result = await tool_repair.evaluate_tool_repair_shadow(
                '<tool_call>{"name":"Inspect","arguments":{"file_path":"a.txt" '
                '"format":"summary"}}</tool_call>',
                self.tools,
                transport=transport,
            )

        self.assertEqual(prose_result.outcome, "skipped_no_malformed_candidate")
        self.assertEqual(path_result.outcome, "skipped_protected_argument")
        self.assertEqual(requests, [])

    async def test_valid_but_schema_invalid_call_does_not_use_local_ai(self):
        transport, requests = self._transport(
            '{"name":"Inspect","arguments":{"format":"summary","max_items":3}}'
        )
        reply = (
            '<tool_call>{"name":"Inspect","arguments":{"unexpected":1}}'
            "</tool_call>"
        )
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", True), patch.object(
            tool_repair, "TOOL_REPAIR_ENDPOINT", self.endpoint
        ), patch.object(tool_repair, "TOOL_REPAIR_MODEL", "test-model"):
            result = await tool_repair.evaluate_tool_repair_shadow(
                reply, self.tools, transport=transport
            )

        self.assertEqual(result.outcome, "skipped_no_malformed_candidate")
        self.assertEqual(requests, [])

    async def test_scheduler_is_disabled_without_configuration(self):
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", False):
            scheduled = tool_repair.schedule_tool_repair_shadow(
                self.candidate, self.tools, "test-request"
            )

        self.assertFalse(scheduled)

    async def test_scheduler_runs_evaluation_in_background_only(self):
        evaluator = AsyncMock(
            return_value=tool_repair.ToolRepairShadowResult("valid_repair", 1, 65)
        )
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", True), patch.object(
            tool_repair, "_evaluate_prepared_candidate", evaluator
        ):
            scheduled = tool_repair.schedule_tool_repair_shadow(
                self.candidate, self.tools, "test-request"
            )
            self.assertTrue(scheduled)
            task = next(iter(tool_repair._PENDING_TASKS))
            await task
            await asyncio.sleep(0)

        evaluator.assert_awaited_once()
        self.assertNotIn(task, tool_repair._PENDING_TASKS)

    async def test_public_endpoint_is_rejected_before_network_access(self):
        transport, requests = self._transport(
            '{"name":"Inspect","arguments":{"format":"summary","max_items":3}}'
        )
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", True), patch.object(
            tool_repair,
            "TOOL_REPAIR_ENDPOINT",
            "http://192.196.10.2:8000/v1/chat/completions",
        ), patch.object(tool_repair, "TOOL_REPAIR_MODEL", "test-model"), patch.object(
            tool_repair, "TOOL_REPAIR_TRUSTED_IPS", ()
        ):
            result = await tool_repair.evaluate_tool_repair_shadow(
                self.candidate, self.tools, transport=transport
            )

        self.assertEqual(result.outcome, "rejected_nonlocal_endpoint")
        self.assertEqual(requests, [])

    async def test_explicitly_trusted_lan_ip_allows_shadow_requests(self):
        output = (
            '{"name":"Inspect","arguments":'
            '{"format":"summary","max_items":3}}'
        )
        transport, requests = self._transport(output)
        endpoint = "http://192.196.10.2:8000/v1/chat/completions"
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", True), patch.object(
            tool_repair, "TOOL_REPAIR_ENDPOINT", endpoint
        ), patch.object(tool_repair, "TOOL_REPAIR_TRUSTED_IPS", ("192.196.10.2",)), patch.object(
            tool_repair, "TOOL_REPAIR_MODEL", "test-model"
        ):
            tool_repair.validate_tool_repair_settings()
            result = await tool_repair.evaluate_tool_repair_shadow(
                self.candidate, self.tools, transport=transport
            )

        self.assertEqual(result.outcome, "valid_repair")
        self.assertEqual(
            requests,
            [
                ("GET", "/health"),
                ("GET", "/slots"),
                ("POST", "/v1/chat/completions"),
            ],
        )

    def test_enabled_configuration_rejects_public_endpoint_without_trust(self):
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", True), patch.object(
            tool_repair,
            "TOOL_REPAIR_ENDPOINT",
            "http://192.196.10.2:8000/v1/chat/completions",
        ), patch.object(tool_repair, "TOOL_REPAIR_MODEL", "test-model"), patch.object(
            tool_repair, "TOOL_REPAIR_TRUSTED_IPS", ()
        ):
            with self.assertRaisesRegex(ValueError, "TOOL_REPAIR_TRUSTED_IPS"):
                tool_repair.validate_tool_repair_settings()

    async def test_disabled_feature_never_contacts_local_ai(self):
        transport, requests = self._transport(
            '{"name":"Inspect","arguments":{"format":"summary","max_items":3}}'
        )
        with patch.object(tool_repair, "TOOL_REPAIR_ENABLED", False):
            result = await tool_repair.evaluate_tool_repair_shadow(
                self.candidate, self.tools, transport=transport
            )

        self.assertEqual(result.outcome, "disabled")
        self.assertEqual(requests, [])


if __name__ == "__main__":
    unittest.main()
