import json
import os
import uuid
from typing import Any, Dict, List, Tuple
from app.config import DEFAULT_PROJECT_DIR, FORBIDDEN_TOOLS, logger
from app.security import is_safe_path_no_c_drive, sanitize_shell_command_no_c_drive


def normalize_tool_arguments(
    fn_name: str, args: Dict[str, Any], tool_schema: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Auto-Mapper: Tự động ánh xạ tên biến bị lệch của Gemini về chuẩn schema,
    đồng thời áp dụng bộ lọc bảo mật chặn ổ C:\\.
    """
    if not isinstance(args, dict):
        args = {}
    name_lower = fn_name.lower()
    props = (tool_schema.get("parameters") or {}).get("properties") or {}

    # 1. Chuẩn hóa file_path cho Read / Write / Edit (chỉ chạy khi "path" KHÔNG nằm trong props)
    if "path" not in props and "file_path" not in args:
        for alias in ("path", "file", "filepath", "filename", "target_file"):
            if alias in args:
                args["file_path"] = args.pop(alias)
                break

    if "file_path" in args and not is_safe_path_no_c_drive(str(args["file_path"])):
        logger.warning(f"[SECURITY] Chặn truy cập ổ C: {args['file_path']}")
        args["file_path"] = os.path.join(DEFAULT_PROJECT_DIR, "BLOCKED_C_DRIVE_ACCESS.txt")

    # 2. Chuẩn hóa pattern cho Glob / Grep
    if "glob" in name_lower and "pattern" not in args:
        for alias in ("query", "path", "glob", "search", "file_pattern"):
            if alias in args:
                args["pattern"] = args.pop(alias)
                break
        if "pattern" not in args:
            args["pattern"] = "*"

    # 3. Chuẩn hóa command & description cho Pwsh / Shell
    if name_lower in ("pwsh", "bash", "shell", "execute_command", "run_command"):
        if "command" not in args:
            for alias in ("cmd", "script", "shell", "code", "input"):
                if alias in args:
                    args["command"] = args.pop(alias)
                    break
        if "command" in args:
            raw_cmd = str(args["command"])
            args["command"] = sanitize_shell_command_no_c_drive(raw_cmd)
            if "description" not in args or not args["description"]:
                args["description"] = f"Run in project: {args['command'][:50]}"

    # 4. Đảm bảo điền đủ các trường required còn thiếu (bỏ tự bịa command="dir")
    params = tool_schema.get("parameters", {})
    for req_key in params.get("required", []):
        if req_key not in args or args[req_key] is None:
            if req_key == "description":
                args[req_key] = f"Execute {fn_name}"
            elif req_key == "pattern":
                args[req_key] = "*"

    return args


def _extract_tag_contents(text: str, open_tag: str, close_tag: str) -> List[str]:
    """Quét chuỗi tuyến tính O(N) bằng find(), không dùng Regex."""
    results = []
    start_idx = 0
    while True:
        idx1 = text.find(open_tag, start_idx)
        if idx1 == -1:
            break
        idx1 += len(open_tag)
        idx2 = text.find(close_tag, idx1)
        if idx2 == -1:
            break
        results.append(text[idx1:idx2].strip())
        start_idx = idx2 + len(close_tag)
    return results


def parse_tool_calls_from_reply(
    reply_text: str, tools_map: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Bóc tách danh sách lệnh gọi công cụ từ phản hồi văn bản của Gemini.
    Hỗ trợ MULTIPLE <tool_call> blocks (parallel tool calls).
    """
    if not reply_text or len(reply_text) > 100_000:
        return []

    extracted_calls: List[Dict[str, Any]] = []
    candidates: List[Tuple[str, bool]] = []

    # 1. Quét thẻ <tool_call>...</tool_call> bằng find() O(N) -> is_tagged=True
    for tag_content in _extract_tag_contents(reply_text, "<tool_call>", "</tool_call>"):
        candidates.append((tag_content, True))

    # 2. Fallback: ```json ... ``` (chỉ khi phản hồi ngắn < 4000 chars và chưa có thẻ <tool_call>) -> is_tagged=False
    if not candidates and len(reply_text) < 4000:
        for block in _extract_tag_contents(reply_text, "```", "```"):
            clean_block = block.strip()
            for prefix in ("json", "tool_call"):
                if clean_block.lower().startswith(prefix):
                    clean_block = clean_block[len(prefix):].strip()
            if clean_block.startswith("{") and clean_block.endswith("}"):
                candidates.append((clean_block, False))

    # 3. Fallback: toàn bộ reply là JSON -> is_tagged=False
    stripped = reply_text.strip()
    if not candidates and len(stripped) < 4000 and stripped.startswith("{") and stripped.endswith("}"):
        candidates.append((stripped, False))

    for raw_json, is_tagged in candidates:
        try:
            data = json.loads(raw_json, strict=False)
            if isinstance(data, dict) and "name" in data:
                fn_name = str(data["name"]).strip()
                for prefix in ("functions.", "tools."):
                    if fn_name.startswith(prefix):
                        fn_name = fn_name[len(prefix):]
                if fn_name.lower() in FORBIDDEN_TOOLS:
                    continue

                # Quy tắc chấp nhận:
                # - tagged: nhận nếu tools_map rỗng HOẶC fn_name trong tools_map
                # - fallback: CHỈ nhận khi tools_map KHÔNG rỗng VÀ fn_name trong tools_map
                if is_tagged:
                    accept = (not tools_map) or (fn_name in tools_map)
                else:
                    accept = bool(tools_map) and (fn_name in tools_map)

                if accept:
                    raw_args = data.get("arguments", data.get("parameters", {}))
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args, strict=False)
                        except Exception as arg_exc:
                            logger.warning(
                                f"[PARSER] Không parse được raw_args JSON: {arg_exc} | đầu: {raw_args[:200]!r}"
                            )
                            raw_args = {"command": raw_args}

                    schema = tools_map.get(fn_name, {}) if tools_map else {}
                    clean_args = normalize_tool_arguments(fn_name, raw_args, schema)

                    # Bỏ tool call nếu thiếu tham số bắt buộc 'command' (không tự bịa 'dir')
                    req_params = (schema.get("parameters") or {}).get("required", [])
                    if "command" in req_params and not str(clean_args.get("command", "")).strip():
                        logger.warning(f"[PARSER] Bỏ tool call '{fn_name}': thiếu tham số bắt buộc 'command'.")
                        continue

                    extracted_calls.append(
                        {
                            "id": f"call_{uuid.uuid4().hex[:16]}",
                            "type": "function",
                            "function": {
                                "name": fn_name,
                                "arguments": json.dumps(clean_args, ensure_ascii=False),
                            },
                        }
                    )
        except Exception as exc:
            logger.warning(f"[PARSER] Không parse được tool_call JSON: {exc} | đầu: {raw_json[:200]!r}")

    return extracted_calls
