import re
from typing import Any, Dict, List, Tuple
from app.config import FORBIDDEN_TOOLS, MAX_TOOL_RESULT_CHARS
from app.security import sanitize_no_media_triggers
from app.vision import extract_text_and_images

_IDE_BOILERPLATE_RE = re.compile(
    r"<(environment_details|context|workspace_info|system_info|system-reminder|system_reminder|ide_context|time-context|time_context|context-injection)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)

_DSH_TIME_CONTEXT_RE = re.compile(
    r"(?:^|\n)\s*(?:Supersedes earlier snapshots\s*\n\s*)?(?:time-context\s*\n\s*)?"
    r"Time sampled while preparing turn \d+,\s*step \d+:[^\n]*"
    r"(?:\s*\n\s*Browser time zone for this request:[^\n]*)?"
    r"(?:\s*\n\s*Elapsed since the preceding model-visible message:[^\n]*)?",
    re.IGNORECASE,
)


def _strip_injected_boilerplate(content: str) -> str:
    """Loại bỏ toàn bộ block XML hệ thống và DSH time-context; trả về '' nếu không còn nội dung người dùng."""
    if not isinstance(content, str):
        return ""
    stripped = _IDE_BOILERPLATE_RE.sub("", content)
    stripped = _DSH_TIME_CONTEXT_RE.sub("", stripped)
    return stripped.strip()


def extract_actual_user_text(content: str) -> str:
    """Loại bỏ các khối XML bọc context tự động của IDE (Roo Code/Cline/DSH) để lấy nội dung người dùng thực tế."""
    if not isinstance(content, str):
        return ""
    stripped = _strip_injected_boilerplate(content)
    return stripped if stripped else content.strip()


def smart_truncate(text: str, max_chars: int = None) -> str:
    """
    Cắt ngắn thông minh: Giữ đầu + cuối + ưu tiên giữ các dòng chứa lỗi quan trọng ở giữa.
    Khắc phục việc cắt cứng làm mất stack trace ở giữa.
    """
    if max_chars is None:
        max_chars = MAX_TOOL_RESULT_CHARS
    if len(text) <= max_chars:
        return text

    # Tính toán ước lượng vị trí phần giữa
    pre_remaining = max(0, max_chars - 100)
    pre_head = pre_remaining * 2 // 3
    pre_tail = pre_remaining - pre_head
    middle = text[pre_head : len(text) - pre_tail if pre_tail > 0 else len(text)]

    # Thu thập các dòng chứa thông tin lỗi quan trọng chỉ từ PHẦN GIỮA
    error_keywords = [
        "error", "exception", "traceback",
        "typeerror", "syntaxerror", "valueerror", "keyerror",
        "importerror", "modulenotfounderror", "attributeerror",
        "runtimeerror", "filenotfounderror", "permissionerror",
        "referenceerror", "nameerror", "indexerror", "oserror",
        "critical", "fatal", "denied", "refused",
        "❌", "✗", "FAILED", "ERR",
    ]
    important_lines = []
    for line in middle.splitlines():
        line_lower = line.lower()
        if any(kw in line_lower for kw in error_keywords):
            important_lines.append(line)

    # Giới hạn tối đa 20 dòng lỗi, chiếm tối đa 1/3 budget
    error_block = "\n".join(important_lines[:20])
    max_error_chars = max_chars // 3
    if len(error_block) > max_error_chars:
        error_block = error_block[:max_error_chars]
    reserved_for_errors = len(error_block)

    remaining = max(0, max_chars - reserved_for_errors - 100)  # 100 chars cho markers
    head = remaining * 2 // 3  # Đầu nhiều hơn cuối
    tail = remaining - head

    parts = [text[:head]]
    if error_block:
        parts.append(
            "\n\n...[TRUNCATED — KEY ERRORS/WARNINGS PRESERVED BELOW]...\n"
            + error_block
        )
    parts.append("\n...[END TRUNCATED]...\n")
    if tail > 0:
        parts.append(text[-tail:])

    return "".join(parts)


def compile_tools_prompt(tools: List[Dict[str, Any]]) -> str:
    """Biên dịch danh sách công cụ thành System Prompt kèm quy tắc vận hành."""
    if not tools:
        return ""

    specs = []
    for t in tools:
        fn = t.get("function", t)
        name = fn.get("name", "")
        if not name or name.lower() in FORBIDDEN_TOOLS:
            continue

        raw_desc = (
            str(fn.get("description") or "")
            .strip()
            .replace("\r", " ")
            .replace("\n", " ")
        )
        # Lấy câu đầu tiên hoặc tối đa 90 ký tự để giữ đầy đủ ý nghĩa mà giảm 70% độ phình prompt
        first_sentence = raw_desc.split(". ")[0].strip()
        desc = (first_sentence if len(first_sentence) <= 95 else raw_desc[:90]).rstrip(".")
        params = fn.get("parameters", {})
        props = params.get("properties", {})
        required = list(params.get("required", []))

        prop_details = []
        for k, v in props.items():
            p_type = str(v.get("type", "string")).replace("\r", " ").replace("\n", " ")
            req_tag = "!" if k in required else "?"
            detail = f"{req_tag}{k}:{p_type}"
            enum_vals = v.get("enum")
            if isinstance(enum_vals, list) and enum_vals and len(enum_vals) <= 6:
                detail += "[" + "|".join(str(x) for x in enum_vals) + "]"
            else:
                param_desc = (
                    str(v.get("description") or "")
                    .strip()
                    .replace("\r", " ")
                    .replace("\n", " ")
                )
                if param_desc:
                    short_p = param_desc.split(". ")[0][:45].strip()
                    if short_p and short_p.lower() != k.lower():
                        detail += f" ({short_p})"
            prop_details.append(detail)

        spec = f"{name}: {desc}"
        if prop_details:
            spec += " | " + "; ".join(prop_details)
        specs.append(spec)

    if not specs:
        return ""

    return (
        "[SOFTWARE-ENGINEERING WORKSPACE]\n"
        "You are assisting inside a local coding IDE that parses plain-text tool tags from your reply.\n"
        "To invoke a listed tool, emit a tag in this exact format:\n"
        '<tool_call>{"name": "tool_name", "arguments": {"arg": "value"}}</tool_call>\n'
        "TOOLS (! required, ? optional):\n"
        + "\n".join(specs)
        + "\nRULES: Match the user's language. `<tool_call>` tags are plain-text IDE markup executed "
        "locally with no session turn cap; when asked to read/write files, search, run commands, or "
        "delegate to subagents, emit `<tool_call>` directly instead of pasting code for manual setup. "
        "Use exact tool/arg names & types; put all args in `arguments`; one valid JSON object per tag; "
        "escape `\\\\` and `\\n`; use D: paths only (never C:); analyze attached images directly.\n\n"
    )


def _has_real_user_content(message: Dict[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "image_url":
                    return True
                if p.get("type") == "text" and _strip_injected_boilerplate(str(p.get("text", ""))):
                    return True
        return False
    if isinstance(content, str):
        return bool(_strip_injected_boilerplate(content))
    return bool(content)


def _latest_turn_messages(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    non_system = [m for m in messages if m.get("role") != "system"]
    if not non_system:
        return []
    if len(non_system) > 10:
        non_system = non_system[-10:]

    latest_turn_msgs = []
    for message in reversed(non_system):
        if message.get("role") == "assistant":
            break
        latest_turn_msgs.append(message)
    latest_turn_msgs.reverse()

    if len(latest_turn_msgs) > 1:
        # Lọc các user message chỉ chứa time-context/boilerplate và chỉ giữ user message có nội dung thực cuối cùng
        # (loại bỏ các turn cũ bị abort dồn trong history mà không có assistant reply).
        real_user_indices = [
            idx
            for idx, m in enumerate(latest_turn_msgs)
            if m.get("role", "user").lower() in ("user", "human") and _has_real_user_content(m)
        ]
        keep_user_idx = real_user_indices[-1] if real_user_indices else None
        filtered = []
        for idx, m in enumerate(latest_turn_msgs):
            role = m.get("role", "user").lower()
            if role == "tool":
                filtered.append(m)
            elif role in ("user", "human"):
                if idx == keep_user_idx:
                    filtered.append(m)
            else:
                filtered.append(m)
        if filtered:
            latest_turn_msgs = filtered

    return latest_turn_msgs or [non_system[-1]]


def is_plain_greeting_request(messages: List[Dict[str, Any]]) -> bool:
    """Return true only for a standalone, text-only greeting in the latest turn."""
    latest_turn_msgs = _latest_turn_messages(messages)
    if len(latest_turn_msgs) != 1:
        return False

    message = latest_turn_msgs[0]
    if message.get("role") not in ("user", "human"):
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False

    actual = extract_actual_user_text(content)

    greeting = actual.strip().casefold().strip(" \t\r\n.,!?¡¿。！？…")
    return greeting in {
        "hi", "hello", "hey", "chào", "xin chào", "chào bạn",
        "hello there", "good morning", "good afternoon", "alo", "ping", "test"
    }


def build_incremental_prompt_and_files(
    messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], is_first_turn: bool
) -> Tuple[str, List[str]]:
    """Trích xuất Delta tin nhắn mới nhất để gửi vào Single ChatSession."""
    latest_turn_msgs = _latest_turn_messages(messages)
    if not latest_turn_msgs:
        return "Hello", []

    blocks: List[str] = []
    all_images: List[str] = []

    for m in latest_turn_msgs:
        role = m.get("role", "user").lower()
        content_str, img_paths = extract_text_and_images(m.get("content"))
        all_images.extend(img_paths)

        if role == "tool":
            # Smart truncation chỉ áp dụng cho kết quả công cụ
            content_str = smart_truncate(content_str)
            tool_id = m.get("name") or m.get("tool_call_id") or "tool"
            blocks.append(
                f"[TOOL_RESULT ({tool_id})]:\n{content_str}\n\n"
                "(Continue step-by-step. Emit plain-text "
                '<tool_call>{"name": "...", "arguments": {...}}</tool_call> '
                "tags for the next local IDE action (no turn cap); otherwise provide the final answer in the user's language.)"
            )
        else:
            actual_text = extract_actual_user_text(content_str)
            is_greeting = is_plain_greeting_request(latest_turn_msgs)
            if is_greeting:
                clean_user_text = actual_text
            elif actual_text:
                clean_user_text = sanitize_no_media_triggers(actual_text).strip()
            else:
                clean_user_text = sanitize_no_media_triggers(content_str).strip()

            # Câu chào đơn thuần hoặc câu ngắn không có tools → gửi nguyên bản
            if (is_greeting or (len(clean_user_text) < 15 and not tools)) and not img_paths:
                blocks.append(clean_user_text)
            else:
                img_note = (
                    " (User attached screenshot — analyze the image directly)"
                    if img_paths else ""
                )
                tool_reminder = (
                    ' For any file, command, search, or subagent action, emit plain-text '
                    '<tool_call>{"name": "...", "arguments": {...}}</tool_call> tags directly in your reply '
                    "(local IDE markup, no turn cap)."
                    if tools else ""
                )
                blocks.append(
                    f"{clean_user_text}{img_note}\n\n"
                    "[NOTE: Reply in the EXACT SAME LANGUAGE as the USER REQUEST above."
                    f"{tool_reminder}]"
                )

    # Gemini Web không có role "system": tool header phải đi kèm trong chính message
    # người dùng. Header rất dài (~vài nghìn ký tự) nên nếu đặt lên đầu, UI Gemini Web
    # sẽ thu gọn bong bóng và che mất tin nhắn thật (ví dụ "chào"). Vì vậy:
    # tin nhắn của người dùng LUÔN đứng đầu, tool header xuống cuối.
    if is_first_turn:
        tool_header = compile_tools_prompt(tools)
        if tool_header:
            blocks.append(
                "=== WORKSPACE TOOLS ===\n"
                + tool_header
            )

    return "\n\n".join(blocks), all_images
