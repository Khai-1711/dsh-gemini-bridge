from collections import Counter
from dataclasses import dataclass, field
import json
import re
import uuid
from typing import Any, Dict, List, Tuple
from app.config import FORBIDDEN_TOOLS, logger
from app.security import (
    is_safe_path,
    sanitize_shell_command_no_c_drive,
)


class InvalidToolArguments(ValueError):
    """Raised when a tool request violates its declared schema or path policy."""


_GEMINI_IMAGE_TAG_RE = re.compile(
    r"""<Image\b[^>]*\bsrc=["']image_agent_tag_[^"']*["'][^>]*/?>""",
    re.IGNORECASE,
)

# Gemini Web auto-linkifies raw URLs outside code fences into `[url](url)` or `[url](url/)`.
# Only unwrap links whose visible label is itself a URL matching the href target,
# leaving genuine Markdown links such as `[Docs](https://example.com)` untouched.
_GEMINI_AUTOLINK_RE = re.compile(
    r"\[((?:https?|ftp)://[^\]\s]+)\]\(((?:https?|ftp)://[^\)\s]+)\)",
    re.IGNORECASE,
)


def clean_gemini_web_artifacts(text: str) -> str:
    """Loại bỏ các thẻ pseudo-image rác do Gemini Web tự chèn vào phản hồi."""
    if not text or "image_agent_tag_" not in text:
        return text
    cleaned = _GEMINI_IMAGE_TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def unlinkify_gemini_urls(text: str) -> str:
    """Khử lỗi Gemini Web tự biến URL thành Markdown link `[http://x](http://x)` trong chuỗi."""
    if not text or "](" not in text or "://" not in text:
        return text

    def _replace(match: re.Match) -> str:
        label = match.group(1)
        href = match.group(2)
        if label.rstrip("/") == href.rstrip("/"):
            return label
        return match.group(0)

    out = text
    for _ in range(3):
        updated = _GEMINI_AUTOLINK_RE.sub(_replace, out)
        if updated == out:
            break
        out = updated
    return out


_MUTATION_OR_EXEC_TOOLS = frozenset(
    {
        "write",
        "write_file",
        "write_to_file",
        "create_file",
        "edit",
        "edit_file",
        "multi_edit",
        "notebook_edit",
        "str_replace",
        "str_replace_editor",
        "replace_file_content",
        "multi_replace_file_content",
        "apply_patch",
        "apply_diff",
        "patch",
        "patch_file",
        "insert_edit",
        "delete_file",
        "remove_file",
        "move_file",
        "rename_file",
        "pwsh",
        "powershell",
        "cmd",
        "bash",
        "sh",
        "shell",
        "terminal",
        "execute_command",
        "exec_command",
        "run_command",
        "run_terminal_cmd",
        "run_shell_command",
        "run_in_terminal",
    }
)

_SHELL_EXEC_TOOLS = frozenset(
    {
        "pwsh",
        "powershell",
        "cmd",
        "bash",
        "sh",
        "shell",
        "terminal",
        "execute_command",
        "exec_command",
        "run_command",
        "run_terminal_cmd",
        "run_shell_command",
        "run_in_terminal",
    }
)

_MARKDOWN_DOC_EXTS = (".md", ".mdx", ".markdown", ".rst")
_RAW_BODY_KEYS_IN_MARKDOWN = frozenset(
    {
        "content",
        "Content",
        "old_string",
        "new_string",
        "old_str",
        "new_str",
        "Target",
        "Replacement",
        "diff",
        "patch",
        "edits",
    }
)
_PROSE_FENCE_LANGS = frozenset(
    {
        "python",
        "py",
        "javascript",
        "js",
        "typescript",
        "ts",
        "tsx",
        "jsx",
        "markdown",
        "md",
        "xml",
        "html",
        "diff",
        "patch",
        "bash",
        "sh",
        "powershell",
        "pwsh",
        "cmd",
        "text",
        "txt",
        "yaml",
        "yml",
        "toml",
        "sql",
        "rust",
        "go",
        "java",
        "c",
        "cpp",
    }
)


_KNOWN_ALIAS_KEYS = frozenset(
    {
        "file_path", "path", "file", "filepath", "filename", "target_file",
        "filePath", "Path", "TargetFile",
        "command", "cmd", "CommandLine", "command_line", "script", "shell", "code", "input",
        "pattern", "query", "glob", "search", "file_pattern",
    }
)
_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _verify_lossless_heuristic_repair(
    raw_json: str,
    data: Dict[str, Any],
    raw_args: Any,
    props: Dict[str, Any],
) -> None:
    """Đảm bảo `json_repair` KHÔNG âm thầm cắt cụt code hoặc bịa thêm key rác khi sửa dấu ngoặc kép."""
    allowed_top = {"name", "arguments", "parameters", "type", "id"} | set(props.keys()) | _KNOWN_ALIAS_KEYS
    extra_top = set(data.keys()) - allowed_top
    if extra_top:
        raise InvalidToolArguments(
            f"Heuristic JSON repair invented unexpected top-level keys {sorted(extra_top)}; rejecting to prevent silent truncation."
        )

    if isinstance(raw_args, dict) and props:
        allowed_arg_keys = set(props.keys()) | _KNOWN_ALIAS_KEYS
        extra_args = set(raw_args.keys()) - allowed_arg_keys
        if extra_args:
            raise InvalidToolArguments(
                f"Heuristic JSON repair split string into bogus argument keys {sorted(extra_args)}; rejecting to prevent file corruption."
            )

    # Bảo toàn từ khóa/định danh: mọi token chữ-số trong raw_json (sau khi đổi escape \n/\t/\r thành khoảng trắng)
    # vẫn phải còn mặt trong JSON sau khi repair (ngăn việc json_repair nuốt mất phần đuôi `return x` của code).
    raw_unescaped = re.sub(r"\\[nrtbf\"\\/]", " ", raw_json)
    repaired_dump = json.dumps(data, ensure_ascii=False)
    rep_unescaped = re.sub(r"\\[nrtbf\"\\/]", " ", repaired_dump)
    raw_counts = Counter(_WORD_TOKEN_RE.findall(raw_unescaped))
    rep_counts = Counter(_WORD_TOKEN_RE.findall(rep_unescaped))
    missing = [tok for tok, cnt in raw_counts.items() if rep_counts[tok] < cnt]
    if missing:
        raise InvalidToolArguments(
            f"Heuristic JSON repair dropped code tokens {missing[:5]}; rejecting to protect file integrity."
        )


def is_mutation_or_exec_tool(fn_name: str) -> bool:
    """Trả True nếu công cụ ghi/sửa/xóa file hoặc chạy lệnh shell (cấm dùng json_repair đoán mò)."""
    low = str(fn_name or "").strip().lower()
    if not low:
        return False
    if low in _MUTATION_OR_EXEC_TOOLS:
        return True
    return any(
        tok in low
        for tok in ("write_file", "edit_file", "apply_patch", "apply_diff", "replace_file", "delete_file", "run_command", "exec_command")
    )


def _sanitize_tool_argument_values(value: Any, *, preserve_markdown_links: bool = False) -> Any:
    if isinstance(value, str):
        return value if preserve_markdown_links else unlinkify_gemini_urls(value)
    if isinstance(value, list):
        return [
            _sanitize_tool_argument_values(item, preserve_markdown_links=preserve_markdown_links)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            k: _sanitize_tool_argument_values(
                v,
                preserve_markdown_links=(preserve_markdown_links or k in _RAW_BODY_KEYS_IN_MARKDOWN)
                if preserve_markdown_links
                else False,
            )
            for k, v in value.items()
        }
    return value


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
    fn_name: str,
    args: Dict[str, Any],
    tool_schema: Dict[str, Any],
    workspace_root: str = "",
) -> Dict[str, Any]:
    """Normalize declared aliases, clean Gemini URL artifacts, and validate against tool schema."""
    if not isinstance(args, dict):
        raise InvalidToolArguments("Tool arguments must be a JSON object.")

    name_lower = fn_name.lower()
    params = tool_schema.get("parameters") or {}
    props = params.get("properties") or {}
    required_keys = set(params.get("required") or [])

    target_path_hint = ""
    for pkey in ("file_path", "path", "filePath", "filepath", "Path", "TargetFile", "target_file"):
        pval = args.get(pkey)
        if isinstance(pval, str) and pval.strip():
            target_path_hint = pval.strip().lower()
            break
    is_markdown_target = target_path_hint.endswith(_MARKDOWN_DOC_EXTS)

    normalized = {
        k: _sanitize_tool_argument_values(
            v,
            preserve_markdown_links=(is_markdown_target and k in _RAW_BODY_KEYS_IN_MARKDOWN),
        )
        for k, v in args.items()
    }

    # Drop explicit null values for optional parameters (common LLM quirk)
    for key in list(normalized.keys()):
        if normalized[key] is None and key not in required_keys:
            normalized.pop(key, None)

    aliases = {
        "file_path": ("path", "file", "filepath", "filename", "target_file", "filePath"),
        "filePath": ("path", "file", "file_path", "filepath", "filename", "target_file"),
        "path": ("file_path", "filePath", "file", "filepath", "filename", "target_file"),
        "Path": ("file_path", "path", "filePath", "filepath", "target_file"),
        "command": ("cmd", "CommandLine", "command_line", "script", "shell", "code", "input"),
        "CommandLine": ("command", "cmd", "command_line", "script"),
        "pattern": ("query", "path", "glob", "search", "file_pattern"),
    }
    for destination, sources in aliases.items():
        if destination not in props or destination in normalized:
            continue
        for source in sources:
            if source in normalized and source not in props:
                normalized[destination] = normalized.pop(source)
                break

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

        if key in ("file_path", "path", "filePath", "filepath", "Path", "TargetFile", "target_file"):
            if not isinstance(value, str):
                raise InvalidToolArguments("File path must be a string.")
            if not is_safe_path(value, workspace_root=workspace_root):
                raise InvalidToolArguments("File path targets a protected OS directory or is outside workspace.")

    if name_lower in _SHELL_EXEC_TOOLS:
        for cmd_key in ("command", "CommandLine", "cmd", "script"):
            command = normalized.get(cmd_key)
            if command is not None:
                if not isinstance(command, str) or not command.strip():
                    raise InvalidToolArguments("Command must be a non-empty string.")
                sanitized = sanitize_shell_command_no_c_drive(command)
                if sanitized != command:
                    raise InvalidToolArguments("Command was rejected by the safety policy.")
                normalized[cmd_key] = sanitized

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


@dataclass
class ToolParseResult:
    """Kết quả parse chi tiết kèm chẩn đoán từng thẻ cho cơ chế fail-closed (4A)."""

    tool_calls: List[Dict[str, Any]]
    malformed_tags: List[Dict[str, Any]] = field(default_factory=list)
    has_unclosed_tag: bool = False


def _find_prose_code_fence_ranges(text: str) -> List[Tuple[int, int]]:
    """Tìm các vùng code fence minh họa (```python, ```xml, ```markdown...) để không bắt nhầm <tool_call> mẫu."""
    if "```" not in text:
        return []
    ranges: List[Tuple[int, int]] = []
    idx = 0
    n = len(text)
    while idx < n:
        open_idx = text.find("```", idx)
        if open_idx == -1:
            break
        # Chỉ tính fence mở ở đầu chuỗi hoặc đầu dòng
        if open_idx > 0 and text[open_idx - 1] not in ("\n", "\r"):
            idx = open_idx + 3
            continue
        line_end = text.find("\n", open_idx + 3)
        if line_end == -1:
            break
        lang = text[open_idx + 3 : line_end].strip().split()[0].lower() if text[open_idx + 3 : line_end].strip() else ""
        close_idx = text.find("\n```", line_end)
        if close_idx == -1:
            break
        after_close = close_idx + 4
        if lang in _PROSE_FENCE_LANGS:
            ranges.append((open_idx, after_close))
        idx = after_close
    return ranges


def _in_ranges(pos: int, ranges: List[Tuple[int, int]]) -> bool:
    for lo, hi in ranges:
        if lo <= pos < hi:
            return True
    return False


def _is_prose_tool_tag_mention(text: str, open_pos: int, open_len: int) -> bool:
    """Trả True nếu `<tool_call>` tại vị trí `open_pos` chỉ là trích dẫn trong văn bản (vd `` `<tool_call>` ``)."""
    if open_pos > 0 and text[open_pos - 1] == "`":
        return True
    after_pos = open_pos + open_len
    if after_pos < len(text) and text[after_pos] == "`":
        return True
    return False


def _scan_tool_call_blocks(text: str) -> Tuple[List[str], bool]:
    """Tách các thẻ <tool_call>...</tool_call> nhận biết chuỗi JSON (" và \\).

    Bỏ qua các lần nhắc thẻ `<tool_call>` trong inline backtick, code fence minh họa
    (```python/```xml/```markdown...) hoặc văn bản giải thích.
    Trả về ``(blocks, has_unclosed)``; nếu có thẻ mở thực sự nhưng tới EOF vẫn chưa
    đóng hợp lệ bên ngoài chuỗi -> ``has_unclosed=True`` (Gate 0 fail-closed).
    """
    open_tag = "<tool_call>"
    close_tag = "</tool_call>"
    results: List[str] = []
    has_unclosed = False
    prose_fences = _find_prose_code_fence_ranges(text)
    pos = 0
    n = len(text)
    while pos < n:
        start = text.find(open_tag, pos)
        if start == -1:
            break
        if _is_prose_tool_tag_mention(text, start, len(open_tag)) or _in_ranges(start, prose_fences):
            pos = start + len(open_tag)
            continue
        body_start = start + len(open_tag)
        i = body_start
        in_string = False
        escaped = False
        found_close = False
        while i < n:
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                i += 1
            else:
                if ch == "<" and text.startswith(close_tag, i):
                    results.append(text[body_start:i].strip())
                    pos = i + len(close_tag)
                    found_close = True
                    break
                if ch == '"':
                    in_string = True
                    escaped = False
                i += 1
        if not found_close:
            tail = text[body_start:]
            tail_stripped = tail.lstrip()
            # Genuinely truncated tool call starts with JSON object/array or ends immediately after tag.
            # Plain prose mentions like "emit <tool_call> to run commands" should NOT trigger false fail-closed.
            if not tail_stripped or tail_stripped[0] in ("{", "["):
                has_unclosed = True
                break
            pos = body_start
    return results, has_unclosed


def _extract_tool_call_blocks(text: str) -> List[str]:
    return _scan_tool_call_blocks(text)[0]


def _has_unclosed_fenced_tool_block(text: str) -> bool:
    parts = text.split("```")
    if len(parts) % 2 == 1:
        return False
    trailing = parts[-1].lstrip()
    first_line = trailing.splitlines()[0].strip().lower() if trailing else ""
    return first_line == "tool_call" or (first_line == "json" and '"name"' in trailing)


def _repair_tool_json_deterministic(raw_json: str) -> Any:
    """Gate 1 deterministic JSON repair (<2ms) for already-closed tool call blocks."""
    try:
        import json_repair
    except ImportError:
        return None
    try:
        repaired = json_repair.repair_json(raw_json, return_objects=True)
    except Exception:
        return None
    if isinstance(repaired, dict) and "name" in repaired:
        return repaired
    return None


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
    """Identify explicit tool-call markup, ignoring inline-backtick prose mentions."""
    if not reply_text:
        return False
    tagged_blocks, has_unclosed = _scan_tool_call_blocks(reply_text)
    if tagged_blocks or has_unclosed:
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
    if _has_unclosed_fenced_tool_block(reply_text):
        return True
    return False


def parse_tool_calls_detailed(
    reply_text: str,
    tools_map: Dict[str, Dict[str, Any]],
    workspace_root: str = "",
) -> ToolParseResult:
    """Bóc tách toàn bộ batch tool call theo nguyên tắc fail-closed tuyệt đối (4A)."""
    if not reply_text or len(reply_text) > 500_000:
        return ToolParseResult(tool_calls=[], malformed_tags=[], has_unclosed_tag=False)

    extracted_calls: List[Dict[str, Any]] = []
    malformed_tags: List[Dict[str, Any]] = []
    candidates: List[Tuple[str, bool]] = []

    # 1. Quét thẻ <tool_call>...</tool_call> theo state machine nhận biết chuỗi JSON & backtick prose
    tagged_blocks, has_unclosed = _scan_tool_call_blocks(reply_text)
    for tag_content in tagged_blocks:
        candidates.append((tag_content, True))

    # 2. Fallback: ```tool_call / ```json ... ```
    if not candidates and not has_unclosed and len(reply_text) < 500_000:
        for block in _extract_tag_contents(reply_text, "```", "```"):
            clean_block = block.strip()
            first_line, separator, remainder = clean_block.partition("\n")
            label = first_line.strip().lower()
            is_labeled_tool_fence = label == "tool_call" or (
                label == "json"
                and '"name"' in clean_block
                and ('"arguments"' in clean_block or '"parameters"' in clean_block)
            )
            if not is_labeled_tool_fence and len(reply_text) >= 4000:
                continue
            if label in ("json", "tool_call"):
                clean_block = remainder.strip() if separator else ""
            if clean_block.startswith("{") and clean_block.endswith("}"):
                candidates.append((clean_block, is_labeled_tool_fence))
            elif is_labeled_tool_fence:
                malformed_tags.append(
                    {
                        "index": len(candidates) + len(malformed_tags) + 1,
                        "reason": "Malformed JSON inside tool_call fence",
                        "snippet": (clean_block or block)[:160],
                    }
                )
        if not candidates and not malformed_tags and _has_unclosed_fenced_tool_block(reply_text):
            has_unclosed = True

    # 3. Fallback: toàn bộ reply là JSON (mở rộng trần tới 200k khi mang cấu trúc tool call)
    stripped = reply_text.strip()
    has_tool_shape = '"name"' in stripped and (
        '"arguments"' in stripped or '"parameters"' in stripped
    )
    raw_limit = 200_000 if has_tool_shape else 4000
    if (
        not candidates
        and not malformed_tags
        and not has_unclosed
        and len(stripped) < raw_limit
        and stripped.startswith("{")
        and stripped.endswith("}")
    ):
        candidates.append((stripped, has_tool_shape))

    for tag_idx, (raw_json, is_tagged) in enumerate(candidates, 1):
        try:
            used_heuristic_repair = False
            try:
                data = _loads_tool_json(raw_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                data = _repair_tool_json_deterministic(raw_json) if is_tagged else None
                if data is None:
                    raise
                used_heuristic_repair = True
            if not isinstance(data, dict) or "name" not in data:
                if is_tagged:
                    malformed_tags.append(
                        {
                            "index": tag_idx,
                            "reason": "Missing 'name' field",
                            "snippet": raw_json[:160],
                        }
                    )
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
                if is_tagged:
                    malformed_tags.append(
                        {
                            "index": tag_idx,
                            "reason": f"Unknown or forbidden tool '{fn_name}'",
                            "snippet": raw_json[:160],
                        }
                    )
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
            if used_heuristic_repair and is_mutation_or_exec_tool(fn_name):
                props = (schema.get("parameters") or {}).get("properties") or {}
                _verify_lossless_heuristic_repair(raw_json, data, raw_args, props)
            clean_args = normalize_tool_arguments(
                fn_name, raw_args, schema, workspace_root=workspace_root
            )
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
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("[PARSER] Rejected malformed or schema-invalid tool call (#%d): %s", tag_idx, exc)
            if is_tagged:
                malformed_tags.append(
                    {
                        "index": tag_idx,
                        "reason": str(exc) or type(exc).__name__,
                        "snippet": raw_json[:160],
                    }
                )

    if malformed_tags or has_unclosed:
        return ToolParseResult(
            tool_calls=[],
            malformed_tags=malformed_tags,
            has_unclosed_tag=has_unclosed,
        )
    return ToolParseResult(
        tool_calls=extracted_calls,
        malformed_tags=[],
        has_unclosed_tag=False,
    )


def parse_tool_calls_from_reply(
    reply_text: str,
    tools_map: Dict[str, Dict[str, Any]],
    workspace_root: str = "",
) -> List[Dict[str, Any]]:
    """Bóc tách danh sách lệnh gọi công cụ từ phản hồi văn bản của Gemini."""
    return parse_tool_calls_detailed(
        reply_text, tools_map, workspace_root=workspace_root
    ).tool_calls
