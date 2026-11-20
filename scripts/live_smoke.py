"""Manual live smoke check against real Gemini Web cookies (never run in mock CI)."""
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("BRIDGE_INSTANCE", "acc1")

from app.gemini_session import SingleChatManager  # noqa: E402
from app.prompt_builder import build_incremental_prompt_and_files  # noqa: E402
from app.tool_parser import parse_tool_calls_detailed  # noqa: E402


async def run_live_smoke() -> int:
    mgr = SingleChatManager()
    await mgr.enable()
    if mgr.is_guest or not mgr.auth_ok:
        print("FAIL: Bridge is in guest mode or unauthenticated; refresh cookies first.")
        await mgr.close()
        return 1

    tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a workspace file.",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    }]
    tools_map = {"read_file": tools[0]["function"]}
    messages = [{"role": "user", "content": "Hãy dùng read_file đọc file requirements.txt."}]
    prompt, images = build_incremental_prompt_and_files(messages, tools, is_first_turn=True)

    t0 = time.perf_counter()
    first_byte_s = None
    done_text = ""
    try:
        async for kind, payload in mgr.send_prepared_turn_stream(prompt, images, include_tool_header=False):
            if kind == "delta" and payload and first_byte_s is None:
                first_byte_s = time.perf_counter() - t0
            elif kind == "done":
                done_text = payload
            elif kind == "error":
                print(f"FAIL: upstream error -> {payload}")
                return 1
    finally:
        await mgr.close()

    lowered = done_text.lower()
    if "tôi chỉ là một mô hình ngôn ngữ" in lowered or "i am just a language model" in lowered:
        print(f"FAIL: canned refusal detected in reply: {done_text[:120]}")
        return 1
    if first_byte_s is None or first_byte_s >= 35.0:
        print(f"FAIL: first-byte latency too slow or missing: {first_byte_s}")
        return 1
    res = parse_tool_calls_detailed(done_text, tools_map)
    if res.malformed_tags or res.has_unclosed_tag or not res.tool_calls:
        print(f"FAIL: invalid or missing tool call: {res}")
        return 1
    print(f"PASS: model={mgr.default_model}, first_byte={first_byte_s:.2f}s, tool={res.tool_calls[0]['function']['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_live_smoke()))
