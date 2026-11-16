from typing import Any, Dict, List, Tuple
from app.config import FORBIDDEN_TOOLS, MAX_TOOL_RESULT_CHARS
from app.security import sanitize_no_media_triggers
from app.vision import extract_text_and_images


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

        desc = (fn.get("description") or "").strip().replace("\n", " ")[:140]
        params = fn.get("parameters", {})
        props = params.get("properties", {})
        required = list(params.get("required", []))

        prop_details = []
        for k, v in props.items():
            p_type = v.get("type", "string")
            req_tag = "REQUIRED" if k in required else "optional"
            prop_details.append(f'"{k}" ({p_type}, {req_tag})')

        specs.append(f"- Tool `{name}`: {desc} | Keys: {', '.join(prop_details)}")

    return (
        "[SYSTEM MODE: SOFTWARE ENGINEERING AGENT. DO NOT trigger video/image generation tools.]\n"
        "Available local tools:\n"
        + "\n".join(specs)
        + "\n\nCRITICAL OPERATING RULES:\n"
        "1. LANGUAGE MATCHING: Always reply in the EXACT SAME LANGUAGE as the user's latest message.\n"
        "2. CLARIFICATION FIRST: If the user's request is vague, incomplete, or ambiguous, "
        "DO NOT guess and DO NOT call modifying tools (`Write`, `Edit`, `Pwsh`). "
        "Ask clarifying questions first in the user's language!\n"
        "3. When calling tools, output XML blocks (you MAY output MULTIPLE <tool_call> blocks "
        "if the calls are independent and can run in parallel):\n"
        "<tool_call>\n"
        '{"name": "ToolName", "arguments": {"required_key": "value"}}\n'
        "</tool_call>\n"
        "4. Put every tool parameter inside the `arguments` object; never put parameters at the top level. "
        "Return valid JSON: escape backslashes as `\\\\` and encode newlines inside strings as `\\n`.\n"
        '5. For `Pwsh`, ALWAYS include both `"command"` and `"description"`. '
        "Operate strictly on the D: drive (NEVER touch C:).\n"
        "6. When user attaches an image, analyze it directly (do NOT call `read_image`).\n\n"
    )


def build_incremental_prompt_and_files(
    messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], is_first_turn: bool
) -> Tuple[str, List[str]]:
    """Trích xuất Delta tin nhắn mới nhất để gửi vào Single ChatSession."""
    non_system = [m for m in messages if m.get("role") != "system"]
    if not non_system:
        return "Hello", []

    # Giữ tối đa 10 tin nhắn gần nhất
    if len(non_system) > 10:
        non_system = non_system[-10:]

    # Lấy các tin nhắn mới phát sinh kể từ sau câu trả lời gần nhất của Assistant
    latest_turn_msgs = []
    for m in reversed(non_system):
        if m.get("role") == "assistant":
            break
        latest_turn_msgs.append(m)
    latest_turn_msgs.reverse()

    if not latest_turn_msgs:
        latest_turn_msgs = [non_system[-1]]

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
                "(Continue step-by-step. If anything is unclear, STOP calling tools and "
                "ask the user in the SAME LANGUAGE. Otherwise output <tool_call>{...}</tool_call>.)"
            )
        else:
            clean_user_text = sanitize_no_media_triggers(content_str).strip()
            # Câu ngắn → gửi nguyên bản
            if len(clean_user_text) < 15 and not img_paths:
                blocks.append(clean_user_text)
            else:
                img_note = (
                    " (User attached screenshot — analyze the image directly)"
                    if img_paths else ""
                )
                blocks.append(
                    f"{clean_user_text}{img_note}\n\n"
                    "[NOTE: Reply in the EXACT SAME LANGUAGE as the USER REQUEST above. "
                    "If the request is ambiguous, ask for clarification before calling tools.]"
                )

    # Gemini Web không có role "system": tool header phải đi kèm trong chính message
    # người dùng. Header rất dài (~vài nghìn ký tự) nên nếu đặt lên đầu, UI Gemini Web
    # sẽ thu gọn bong bóng và che mất tin nhắn thật (ví dụ "chào"). Vì vậy:
    # tin nhắn của người dùng LUÔN đứng đầu, operator instructions xuống cuối.
    if is_first_turn:
        tool_header = compile_tools_prompt(tools)
        if tool_header:
            blocks.append(
                "=== OPERATOR INSTRUCTIONS (apply to every reply in this chat) ===\n"
                + tool_header
            )

    return "\n\n".join(blocks), all_images
