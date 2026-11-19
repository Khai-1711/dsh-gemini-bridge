import json
import uuid
from typing import Any, Dict, List, Optional, Tuple
from app.config import FORBIDDEN_TOOLS, logger
from app.security import (
    is_path_within_project,
    is_safe_path_no_c_drive,
    sanitize_shell_command_no_c_drive,
)


class InvalidToolArguments(ValueError):
    """Raised when a tool request violates its declared schema or path policy."""


def _is_schema_type(value: Any, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, dict)
    return True


def normalize_tool_arguments(
    fn_name: str, args: Dict[str, Any], tool_schema: Dict[str, Any]
) -> Dict[str, Any]:
    """Normalize declared aliases and reject arguments outside the tool schema."""
    if not isinstance(args, dict):
        raise InvalidToolArguments("Tool arguments must be a JSON object.")

    name_lower = fn_name.lower()
    props = (tool_schema.get("parameters") or {}).get("properties") or {}
    normalized = dict(args)

    aliases = {
        "file_path": ("path", "file", "filepath", "filename", "target_file", "filePath"),
        "filePath": ("path", "file", "file_path", "filepath", "filename", "target_file"),
        "path": ("file_path", "filePath", "file", "filepath", "filename", "target_file"),
        "command": ("cmd", "script", "shell", "code", "input"),
        "pattern": ("query", "path", "glob", "search", "file_pattern"),
    }
    for destination, sources in aliases.items():
        if destination not in props or destination in normalized:
            continue
        for source in sources:
            if source in normalized and source not in props:
                normalized[destination] = normalized.pop(source)
                break

    params = tool_schema.get("parameters") or {}
    if params.get("additionalProperties") is False:
        extras = set(normalized) - set(props)
        if extras:
            raise InvalidToolArguments("Tool arguments contain undeclared keys.")

    for key in params.get("required", []):
        if key not in normalized or normalized[key] is None:
            raise InvalidToolArguments("Tool arguments are missing required keys.")

    for key, value in normalized.items():
        property_schema = props.get(key)
        if not isinstance(property_schema, dict):
            continue
        schema_type = property_schema.get("type")
        if isinstance(schema_type, str) and not _is_schema_type(value, schema_type):
            raise InvalidToolArguments("Tool argument type does not match its schema.")
        if "enum" in property_schema and value not in property_schema["enum"]:
            raise InvalidToolArguments("Tool argument is not an allowed enum value.")

        if key in ("file_path", "path", "filePath", "filepath"):
            if not isinstance(value, str):
                raise InvalidToolArguments("File path must be a string.")
            if not is_safe_path_no_c_drive(value) or not is_path_within_project(value):
                raise InvalidToolArguments("File path is outside the project directory.")

    if name_lower in ("pwsh", "bash", "shell", "execute_command", "run_command"):
        command = normalized.get("command")
        if command is not None:
            if not isinstance(command, str) or not command.strip():
                raise InvalidToolArguments("Command must be a non-empty string.")
            sanitized = sanitize_shell_command_no_c_drive(command)
            if sanitized != command:
                raise InvalidToolArguments("Command was rejected by the safety policy.")
            normalized["command"] = sanitized

    return normalized


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


def _loads_tool_json(raw_json: str) -> Any:
    try:
        return json.loads(raw_json, strict=False)
    except json.JSONDecodeError as exc:
        if "Invalid \\escape" not in exc.msg:
            repaired = _remove_trailing_commas(raw_json)
            if repaired != raw_json:
                return json.loads(repaired, strict=False)
            raise

    repaired = []
    in_string = False
    i = 0
    while i < len(raw_json):
        char = raw_json[i]
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            i += 1
            continue

        if char == '"':
            repaired.append(char)
            in_string = False
            i += 1
            continue

        if char == "\\":
            if i + 1 < len(raw_json) and raw_json[i + 1] in '"\\/bfnrt':
                repaired.extend((char, raw_json[i + 1]))
                i += 2
                continue
            if (
                i + 5 < len(raw_json)
                and raw_json[i + 1] == "u"
                and all(c in "0123456789abcdefABCDEF" for c in raw_json[i + 2:i + 6])
            ):
                repaired.extend(raw_json[i:i + 6])
                i += 6
                continue
            repaired.extend(("\\\\",))
            i += 1
            continue

        repaired.append(char)
        i += 1

    repaired_json = "".join(repaired)
    try:
        return json.loads(repaired_json, strict=False)
    except json.JSONDecodeError:
        repaired_json = _remove_trailing_commas(repaired_json)
        return json.loads(repaired_json, strict=False)


def _remove_trailing_commas(raw_json: str) -> str:
    """Remove commas immediately before a closing JSON delimiter, outside strings."""
    out: List[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(raw_json):
        char = raw_json[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            out.append(char)
        elif char == ",":
            lookahead = index + 1
            while lookahead < len(raw_json) and raw_json[lookahead].isspace():
                lookahead += 1
            if lookahead >= len(raw_json) or raw_json[lookahead] not in "}]":
                out.append(char)
        else:
            out.append(char)
        index += 1
    return "".join(out)


def has_tool_call_intent(reply_text: str) -> bool:
    """Identify explicit tool-call markup, including a labeled tool_call fence."""
    if not reply_text:
        return False
    if "<tool_call>" in reply_text:
        return True
    for block in _extract_tag_contents(reply_text, "```", "```"):
        lines = block.lstrip().splitlines()
        if lines and lines[0].strip().lower() == "tool_call":
            return True
        if (
            lines
            and lines[0].strip().lower() == "json"
            and '"name"' in block
            and ('"arguments"' in block or '"parameters"' in block)
        ):
            return True
    return False


def extract_malformed_tool_call_candidate(
    reply_text: str, max_chars: int = 4000
) -> Optional[str]:
    """Return one explicitly marked, syntactically invalid tool-call candidate."""
    if not reply_text or len(reply_text) > 100_000:
        return None

    tagged = _extract_tag_contents(reply_text, "<tool_call>", "</tool_call>")
    if tagged:
        if len(tagged) != 1:
            return None
        candidate = tagged[0]
    else:
        if len(reply_text) >= 4000:
            return None
        candidates = []
        for block in _extract_tag_contents(reply_text, "```", "```"):
            clean_block = block.strip()
            first_line, separator, remainder = clean_block.partition("\n")
            label = first_line.strip().lower()
            candidate_text = remainder.strip() if separator else ""
            if label == "tool_call" or (
                label == "json"
                and '"name"' in candidate_text
                and (
                    '"arguments"' in candidate_text
                    or '"parameters"' in candidate_text
                )
            ):
                candidates.append(candidate_text)
        if len(candidates) != 1:
            return None
        candidate = candidates[0]

    candidate = candidate.strip()
    if not candidate or len(candidate) > max_chars or '"name"' not in candidate:
        return None
    try:
        _loads_tool_json(candidate)
    except (json.JSONDecodeError, TypeError, ValueError):
        return candidate
    return None


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
    rejected_candidate = False

    # 1. Quét thẻ <tool_call>...</tool_call> bằng find() O(N) -> is_tagged=True
    for tag_content in _extract_tag_contents(reply_text, "<tool_call>", "</tool_call>"):
        candidates.append((tag_content, True))

    # 2. Fallback: ```json ... ``` (chỉ khi phản hồi ngắn < 4000 chars và chưa có thẻ <tool_call>) -> is_tagged=False
    if not candidates and len(reply_text) < 4000:
        for block in _extract_tag_contents(reply_text, "```", "```"):
            clean_block = block.strip()
            first_line, separator, remainder = clean_block.partition("\n")
            if first_line.strip().lower() in ("json", "tool_call"):
                clean_block = remainder.strip() if separator else ""
            if clean_block.startswith("{") and clean_block.endswith("}"):
                candidates.append((clean_block, False))

    # 3. Fallback: toàn bộ reply là JSON -> is_tagged=False
    stripped = reply_text.strip()
    if not candidates and len(stripped) < 4000 and stripped.startswith("{") and stripped.endswith("}"):
        candidates.append((stripped, False))

    for raw_json, is_tagged in candidates:
        try:
            data = _loads_tool_json(raw_json)
            if not isinstance(data, dict) or "name" not in data:
                rejected_candidate = rejected_candidate or is_tagged
                continue
            fn_name = str(data["name"]).strip()
            for prefix in ("functions.", "tools."):
                if fn_name.startswith(prefix):
                    fn_name = fn_name[len(prefix):]

            if (
                fn_name.lower() in FORBIDDEN_TOOLS
                or not tools_map
                or fn_name not in tools_map
            ):
                rejected_candidate = True
                continue

            if "arguments" in data:
                raw_args = data["arguments"]
            elif "parameters" in data:
                raw_args = data["parameters"]
            else:
                raw_args = {
                    key: value
                    for key, value in data.items()
                    if key not in {"name", "type", "id"}
                }
            if isinstance(raw_args, str):
                raw_args = _loads_tool_json(raw_args)

            schema = tools_map[fn_name]
            clean_args = normalize_tool_arguments(fn_name, raw_args, schema)
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
        except (json.JSONDecodeError, TypeError, ValueError):
            rejected_candidate = True
            logger.warning("[PARSER] Rejected malformed or schema-invalid tool call.")

    return extracted_calls if extracted_calls else ([] if rejected_candidate else [])
