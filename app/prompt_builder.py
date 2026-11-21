import re
from typing import Any, Dict, List, Tuple
from app.config import (
    FORBIDDEN_TOOLS,
    MAX_TOOL_RESULT_CHARS,
    MAX_TOTAL_TOOL_RESULTS_CHARS,
    REPLAY_MAX_CHARS,
)
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

_WORKSPACE_PATTERNS = (
    re.compile(r"<cwd>\s*([^<\r\n]+?)\s*</cwd>", re.IGNORECASE),
    re.compile(
        r"(?:^|\n)\s*(?:Command Working Directory|Working [Dd]irectory|Workspace(?: [Rr]oot| [Pp]ath)?|Project [Dd]irectory|CWD)\s*:\s*[\"']?([A-Za-z]:[\\/][^\r\n\"'<>]+|/[^\r\n\"'<>]+)[\"']?",
        re.IGNORECASE,
    ),
    re.compile(
        r"Active Workspaces\s*:\s*(?:\r?\n)\s*-\s*[\"']?([A-Za-z]:[\\/][^\r\n\"'<>]+|/[^\r\n\"'<>]+)[\"']?",
        re.IGNORECASE,
    ),
)

_SYSTEM_CONTEXT_MAX_CHARS = 2400


def _raw_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and (item.get("type") == "text" or "text" in item):
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content or "")


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


def extract_workspace_root(messages: List[Dict[str, Any]]) -> str:
    """Trích xuất đường dẫn thư mục dự án hiện tại từ thông điệp system/context của IDE."""
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "")).lower()
        if role not in ("system", "developer", "user", "human"):
            continue
        raw = _raw_text_from_content(msg.get("content"))
        if not raw:
            continue
        for pat in _WORKSPACE_PATTERNS:
            m = pat.search(raw)
            if m:
                candidate = m.group(1).strip().rstrip(".,;)")
                if candidate and (re.match(r"^[A-Za-z]:[\\/]", candidate) or candidate.startswith("/")):
                    return candidate
    return ""


def extract_system_context(
    messages: List[Dict[str, Any]], max_chars: int = _SYSTEM_CONTEXT_MAX_CHARS
) -> str:
    """Rút gọn phần chỉ dẫn hệ thống (role=system/developer) để truyền vào Turn 1 của Gemini."""
    parts: List[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).lower() not in ("system", "developer"):
            continue
        raw = _raw_text_from_content(msg.get("content"))
        if not raw:
            continue
        cleaned = _DSH_TIME_CONTEXT_RE.sub("", raw).strip()
        if cleaned:
            parts.append(cleaned)
    if not parts:
        return ""
    combined = "\n\n".join(parts)
    if len(combined) <= max_chars:
        return combined
    head = max_chars * 2 // 3
    tail = max(0, max_chars - head - 30)
    return combined[:head].rstrip() + "\n...[SYSTEM CONTEXT TRIMMED]...\n" + (combined[-tail:].lstrip() if tail else "")


def build_context_bootstrap(
    messages: List[Dict[str, Any]],
    latest_turn_count: int,
    max_chars: int = None,
) -> str:
    """Dựng bản tóm tắt gọn lịch sử hội thoại khi phải mở ChatSession mới giữa chừng (failover/1097/restart)."""
    if max_chars is None:
        max_chars = min(max(REPLAY_MAX_CHARS, 2000), 6000)

    non_system = [
        m for m in (messages or [])
        if isinstance(m, dict) and str(m.get("role", "")).lower() not in ("system", "developer")
    ]
    if latest_turn_count <= 0 or len(non_system) <= latest_turn_count:
        return ""

    prior = non_system[:-latest_turn_count]
    if not any(str(m.get("role", "")).lower() in ("assistant", "tool") for m in prior):
        return ""

    first_user_goal = ""
    digest_lines: List[str] = []
    for m in prior:
        role = str(m.get("role", "user")).lower()
        raw_text = _raw_text_from_content(m.get("content"))
        if role in ("user", "human"):
            cleaned = _strip_injected_boilerplate(raw_text)
            if not cleaned:
                continue
            compact = " ".join(cleaned.split())
            if not first_user_goal:
                first_user_goal = compact[:500]
            digest_lines.append(f"user: {compact[:400]}")
        elif role == "assistant":
            compact = " ".join(raw_text.split())[:350]
            tc_names = []
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict):
                    fn = tc.get("function") or {}
                    name = fn.get("name") or "?"
                    args_str = str(fn.get("arguments") or "")[:120]
                    tc_names.append(f"{name}({args_str})")
            if tc_names:
                tc_summary = "; ".join(tc_names[:8])
                line = f"assistant: {compact} [called: {tc_summary}]" if compact else f"assistant: [called: {tc_summary}]"
            elif compact:
                line = f"assistant: {compact}"
            else:
                continue
            digest_lines.append(line[:500])
        elif role == "tool":
            tool_id = m.get("name") or m.get("tool_call_id") or "tool"
            compact = " ".join(raw_text.split())[:260]
            digest_lines.append(f"tool({tool_id}): {compact}")

    if not first_user_goal and not digest_lines:
        return ""

    header = "=== CONVERSATION CONTEXT (resumed on fresh chat) ===\n"
    footer = "\n=== END CONTEXT ==="
    goal_block = f"[ORIGINAL REQUEST]: {first_user_goal}\n" if first_user_goal else ""
    budget = max(max_chars - len(header) - len(footer) - len(goal_block) - 20, 200)

    kept: List[str] = []
    used = 0
    for line in reversed(digest_lines):
        cost = len(line) + 1
        if used + cost > budget:
            break
        kept.insert(0, line)
        used += cost

    body = goal_block + ("[RECENT HISTORY]:\n" + "\n".join(kept) if kept else "")
    return (header + body.strip() + footer)[:max_chars]


def smart_truncate(text: str, max_chars: int = None) -> str:
    """
    Cắt ngắn thông minh: Giữ đầu + cuối + ưu tiên giữ các dòng chứa lỗi quan trọng ở giữa.
    Khắc phục việc cắt cứng làm mất stack trace ở giữa.
    """
    if max_chars is None:
        max_chars = MAX_TOOL_RESULT_CHARS
    if len(text) <= max_chars:
        return text

    pre_remaining = max(0, max_chars - 100)
    pre_head = pre_remaining * 2 // 3
    pre_tail = pre_remaining - pre_head
    middle = text[pre_head : len(text) - pre_tail if pre_tail > 0 else len(text)]

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

    error_block = "\n".join(important_lines[:20])
    max_error_chars = max_chars // 3
    if len(error_block) > max_error_chars:
        error_block = error_block[:max_error_chars]
    reserved_for_errors = len(error_block)

    remaining = max(0, max_chars - reserved_for_errors - 100)
    head = remaining * 2 // 3
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
        "Use exact tool/arg names & types; put args in `arguments`; one valid JSON object per tag; "
        "keep raw URLs (no markdown links); escape `\\\\` and `\\n`; work in active workspace; inspect attached images directly.\n\n"
    )


def _has_real_user_content(message: Dict[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict):
                if p.get("type") in ("image_url", "image"):
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
    non_system = [
        m for m in messages
        if isinstance(m, dict) and str(m.get("role", "")).lower() not in ("system", "developer")
    ]
    if not non_system:
        return []

    latest_turn_msgs = []
    for message in reversed(non_system):
        if str(message.get("role", "")).lower() == "assistant":
            break
        latest_turn_msgs.append(message)
        if len(latest_turn_msgs) >= 40:
            break
    latest_turn_msgs.reverse()

    if len(latest_turn_msgs) > 1:
        real_user_indices = [
            idx
            for idx, m in enumerate(latest_turn_msgs)
            if str(m.get("role", "user")).lower() in ("user", "human") and _has_real_user_content(m)
        ]
        keep_user_idx = real_user_indices[-1] if real_user_indices else None
        filtered = []
        for idx, m in enumerate(latest_turn_msgs):
            role = str(m.get("role", "user")).lower()
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
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    is_first_turn: bool,
    include_context_bootstrap: bool = None,
) -> Tuple[str, List[str]]:
    """Trích xuất Delta tin nhắn mới nhất để gửi vào ChatSession.

    Khi `is_first_turn=True`:
    - Kèm phần cốt lõi của `system`/`developer` prompt (workspace, OS/Shell, vai trò agent).
    - Nếu cuộc hội thoại đã có lượt trước đó (failover sang worker mới / reset do lỗi 1097 / restart),
      tự động gắn Context Bootstrap để Gemini không bị mất trí nhớ.
    """
    latest_turn_msgs = _latest_turn_messages(messages)
    if not latest_turn_msgs:
        return "Hello", []

    blocks: List[str] = []
    all_images: List[str] = []
    has_tool_results = False
    is_greeting = is_plain_greeting_request(latest_turn_msgs)

    tool_msg_count = sum(
        1 for m in latest_turn_msgs if str(m.get("role", "user")).lower() == "tool"
    )
    per_tool_budget = MAX_TOOL_RESULT_CHARS
    if tool_msg_count >= 2 and MAX_TOTAL_TOOL_RESULTS_CHARS > 0:
        fair_share = MAX_TOTAL_TOOL_RESULTS_CHARS // tool_msg_count
        per_tool_budget = min(MAX_TOOL_RESULT_CHARS, max(1500, fair_share))

    for m in latest_turn_msgs:
        role = str(m.get("role", "user")).lower()
        content_str, img_paths = extract_text_and_images(m.get("content"))
        all_images.extend(img_paths)

        if role == "tool":
            has_tool_results = True
            content_str = smart_truncate(content_str, max_chars=per_tool_budget)
            tool_id = m.get("name") or m.get("tool_call_id") or "tool"
            blocks.append(f"[TOOL_RESULT ({tool_id})]:\n{content_str}")
        else:
            actual_text = extract_actual_user_text(content_str)
            if is_greeting:
                clean_user_text = actual_text
            elif actual_text:
                clean_user_text = sanitize_no_media_triggers(actual_text).strip()
            else:
                clean_user_text = sanitize_no_media_triggers(content_str).strip()

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

    if has_tool_results:
        blocks.append(
            "(Continue step-by-step based on the tool result(s) above. Emit plain-text "
            '<tool_call>{"name": "...", "arguments": {...}}</tool_call> '
            "tags for the next local IDE action (no turn cap); otherwise provide the final answer in the user's language.)"
        )

    should_bootstrap = (
        include_context_bootstrap
        if include_context_bootstrap is not None
        else (is_first_turn and not is_greeting)
    )
    if should_bootstrap:
        bootstrap = build_context_bootstrap(messages, len(latest_turn_msgs))
        if bootstrap:
            blocks.insert(0, bootstrap)

    if is_first_turn and not is_greeting:
        sys_ctx = extract_system_context(messages)
        if sys_ctx:
            blocks.append("[SYSTEM & WORKSPACE CONTEXT]\n" + sys_ctx)
        tool_header = compile_tools_prompt(tools)
        if tool_header:
            blocks.append("=== WORKSPACE TOOLS ===\n" + tool_header)

    return "\n\n".join(blocks), all_images
