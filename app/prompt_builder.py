import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from app.config import (
    ALLOWED_TOOLS,
    BLOCKED_TOOLS,
    FORBIDDEN_TOOLS,
    MAX_TOOL_RESULT_CHARS,
    MAX_TOTAL_TOOL_RESULTS_CHARS,
    REPLAY_MAX_CHARS,
    TOOL_PROFILE,
)
from app.security import sanitize_no_media_triggers
from app.tool_profiles import filter_tools
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

_DSH_BRACKET_META_RE = re.compile(
    r"(?:^|\n)\s*\[(?:model changed|session resumed|mode changed|context note)\s*:[^\]]*\]\s*(?=\n|$)",
    re.IGNORECASE,
)

_DSH_GUI_HMR_NOISE_RE = re.compile(
    r"(?:^|\n)[^\n]*client-plugin HMR receiver is active.*?(?:window\.__DSH_BOOT__[^\n]*|verify its exact URL\.)",
    re.DOTALL | re.IGNORECASE,
)

_SENTENCE_END_RE = re.compile(r"(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\betc)\.\s+")

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
    """Loại bỏ toàn bộ block XML hệ thống, DSH time-context và thẻ [model changed: ...]; trả về '' nếu không còn nội dung người dùng."""
    if not isinstance(content, str):
        return ""
    stripped = _IDE_BOILERPLATE_RE.sub("", content)
    stripped = _DSH_TIME_CONTEXT_RE.sub("", stripped)
    stripped = _DSH_BRACKET_META_RE.sub("", stripped)
    return stripped.strip()


def _clip_at_word(text: str, limit: int) -> str:
    """Cắt chuỗi theo ranh giới từ và tự đóng ngoặc đơn bị hở (tránh cắt cụt kiểu `informati` hay `(e.g`)."""
    clean = " ".join(str(text or "").split()).strip()
    if len(clean) <= limit:
        return clean.rstrip(".")
    cut = clean[:limit]
    last_space = cut.rfind(" ")
    if last_space >= limit * 2 // 3:
        cut = cut[:last_space]
    cut = cut.rstrip(" ,;:-.")
    # Nếu mở `(` mà bị cắt mất `)`, bỏ phần mở ngoặc dở dang ở đuôi
    if cut.count("(") > cut.count(")"):
        open_paren = cut.rfind("(")
        if open_paren > limit // 2:
            cut = cut[:open_paren].rstrip(" ,;:-.")
        else:
            cut += ")"
    return cut


def _first_clean_sentence(raw: str, limit: int) -> str:
    clean = " ".join(str(raw or "").split()).strip()
    if not clean:
        return ""
    first = _SENTENCE_END_RE.split(clean, maxsplit=1)[0].strip()
    return _clip_at_word(first, limit)


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
        cleaned = _DSH_TIME_CONTEXT_RE.sub("", raw)
        cleaned = _DSH_GUI_HMR_NOISE_RE.sub("", cleaned).strip()
        if cleaned:
            parts.append(cleaned)
    if not parts:
        return ""
    combined = "\n\n".join(parts)
    if len(combined) <= max_chars:
        return combined
    head_budget = max_chars * 2 // 3
    tail_budget = max(0, max_chars - head_budget - 34)
    head_slice = combined[:head_budget]
    nl_head = head_slice.rfind("\n")
    if nl_head >= head_budget // 2:
        head_slice = head_slice[:nl_head]
    tail_slice = combined[-tail_budget:] if tail_budget else ""
    if tail_slice:
        nl_tail = tail_slice.find("\n")
        if 0 <= nl_tail <= len(tail_slice) // 2:
            tail_slice = tail_slice[nl_tail + 1 :]
    return head_slice.rstrip() + "\n...[SYSTEM CONTEXT TRIMMED]...\n" + tail_slice.lstrip()


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
    integrity_rule = "\n[INTEGRITY]: Condensed history — re-read target lines before editing any file."
    footer = f"{integrity_rule}\n=== END CONTEXT ==="
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

    total_len = len(text)
    if max_chars >= 500:
        end_marker_budget = 140
    else:
        end_marker_budget = 26
    err_header = "\n\n...[TRUNCATED — KEY ERRORS/WARNINGS PRESERVED BELOW]...\n"
    overhead = end_marker_budget + (len(err_header) if error_block else 0)
    remaining = max(0, max_chars - reserved_for_errors - overhead)
    head = remaining * 2 // 3
    tail = remaining - head
    omitted_start = head
    omitted_end = max(head, total_len - tail)
    omitted_chars = max(0, omitted_end - omitted_start)

    if max_chars >= 500:
        end_marker = (
            f"\n...[TRUNCATED: omitted {omitted_chars} chars ({omitted_start}..{omitted_end} of {total_len}); "
            "read exact range before editing omitted part]...\n"
        )
    else:
        end_marker = "\n...[END TRUNCATED]...\n"

    parts = [text[:head]]
    if error_block:
        parts.append(err_header + error_block)
    parts.append(end_marker)
    if tail > 0:
        parts.append(text[-tail:])

    return "".join(parts)


def filter_request_tools(
    tools: Optional[List[Dict[str, Any]]],
    *,
    tool_profile: Optional[str] = None,
    allowed_tools: Optional[Sequence[str]] = None,
    blocked_tools: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Lọc danh sách tool đầu vào theo TOOL_PROFILE / ALLOWED_TOOLS / BLOCKED_TOOLS hiện hành."""
    return filter_tools(
        tools,
        profile=tool_profile if tool_profile is not None else TOOL_PROFILE,
        allowed_patterns=allowed_tools if allowed_tools is not None else ALLOWED_TOOLS,
        blocked_patterns=blocked_tools if blocked_tools is not None else BLOCKED_TOOLS,
        forbidden_tools=FORBIDDEN_TOOLS,
    )


def compile_tools_prompt(
    tools: List[Dict[str, Any]],
    *,
    tool_profile: Optional[str] = None,
    allowed_tools: Optional[Sequence[str]] = None,
    blocked_tools: Optional[Sequence[str]] = None,
) -> str:
    """Biên dịch danh sách công cụ (sau khi lọc theo Profile) thành System Prompt kèm quy tắc vận hành."""
    if not tools:
        return ""

    filtered_tools, stats = filter_request_tools(
        tools,
        tool_profile=tool_profile,
        allowed_tools=allowed_tools,
        blocked_tools=blocked_tools,
    )
    if not filtered_tools:
        return ""

    specs = []
    for t in filtered_tools:
        fn = t.get("function", t)
        name = fn.get("name", "")
        if not name:
            continue

        desc = _first_clean_sentence(fn.get("description") or "", 92)
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
                short_p = _first_clean_sentence(v.get("description") or "", 48)
                if short_p and short_p.lower() != k.lower():
                    detail += f" ({short_p})"
            prop_details.append(detail)

        spec = f"{name}: {desc}"
        if prop_details:
            spec += " | " + "; ".join(prop_details)
        specs.append(spec)

    if not specs:
        return ""

    actions_clause = (
        "read/write files, search, run commands, or delegate to subagents"
        if stats.get("has_subagent_tools")
        else "read/write files, search, or run commands"
    )
    exec_clause = (
        " When asked to implement or edit code, invoke tools directly to finish changes instead of stopping at a plan; "
        "prefer targeted edit over full-file write and split large files (>250 lines) into smaller steps."
        if stats.get("has_edit_tools")
        else ""
    )

    return (
        "[SOFTWARE-ENGINEERING WORKSPACE]\n"
        "Local coding IDE that parses plain-text tool tags from your reply.\n"
        'Tag syntax: <tool_call>{"name": "tool_name", "arguments": {"arg": "value"}}</tool_call>\n'
        "TOOLS (! required, ? optional):\n"
        + "\n".join(specs)
        + "\nRULES: Match user language. Chat naturally first: reply in plain text WITHOUT `<tool_call>` for greetings/chitchat/general Q&A. "
        f"`<tool_call>` is local IDE markup with no session turn cap; when asked to {actions_clause}, emit `<tool_call>` directly "
        f"instead of manual code.{exec_clause} Exact names/types; args in `arguments`; one JSON object per tag; "
        "raw URLs only; escape `\\\\` and `\\n`; work in workspace; read attached images directly.\n\n"
    )


_GREETING_EXACT_SET = frozenset(
    {
        "hi", "hello", "hey", "yo", "sup", "alo", "ping", "test",
        "ok", "oke", "okay", "thanks", "thank you", "cảm ơn", "cam on", "cám ơn",
        "chào", "xin chào", "chao", "xin chao",
        "chào bạn", "chào anh", "chào em", "chào bé",
        "hi em", "hi bé", "hi anh", "hi bạn", "hello em", "hello bạn", "hello there",
        "good morning", "good afternoon", "good evening",
        "bạn là ai", "em là ai", "em tên gì", "bạn tên gì", "em là model gì",
        "bạn khỏe không", "em khỏe không", "khỏe không",
    }
)

_NON_GREETING_ACTION_HINTS = (
    "/", "\\", "@", "`", ".py", ".js", ".ts", ".json", ".md", ".html", ".css",
    "file", "code", "bug", "fix", "read", "edit", "write", "run", "test ",
    "check", "search", "inspect", "đọc", "sửa", "viết", "tạo", "xóa",
    "chạy", "kiểm tra", "tìm",
)

_WORKSPACE_TASK_INDICATORS_RE = re.compile(
    r"(?:@|`|[/\\][A-Za-z0-9_.\-]+|\.(?:py|ts|tsx|js|jsx|json|md|html|css|bat|sh|yaml|yml|env|txt)\b|"
    r"\b(?:file|folder|dir|directory|repo|project|workspace|code|function|class|bug|error|fix|debug|"
    r"read|write|edit|create|delete|remove|update|refactor|implement|build|run|exec|command|shell|"
    r"terminal|powershell|pwsh|bash|git|commit|push|pull|test|lint|check|review|inspect|analyze|"
    r"screenshot|image|search|find|grep|glob|api|backend|frontend|server|database|continue)\b|"
    r"(?:^|\s)(?:tiếp tục|thực hiện|triển khai|đọc|sửa|viết|tạo|xóa|chạy|kiểm tra|tìm|xem|mở|cài|làm|dự án|thư mục|lỗi|hàm|phân tích|ảnh)(?:\s|$|[.,!?]))",
    re.IGNORECASE,
)


def _extract_text_without_images(content: Any) -> Tuple[str, bool]:
    if isinstance(content, str):
        return content, False
    if isinstance(content, list):
        texts: List[str] = []
        has_image = False
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                texts.append(str(part.get("text", "")))
            elif ptype in ("image_url", "image") or ptype is not None:
                has_image = True
        return "\n".join(texts), has_image
    return str(content or ""), False


def _is_single_message_greeting(text: str) -> bool:
    if not text or not isinstance(text, str):
        return False
    stripped = _strip_injected_boilerplate(text)
    if not stripped:
        return False
    norm = " ".join(
        stripped.casefold().strip(" \t\r\n.,!?¡¿。！？…~`'\":;-()[]").split()
    )
    if not norm or len(norm) > 45:
        return False
    if any(tok in norm for tok in _NON_GREETING_ACTION_HINTS):
        return False
    return norm in _GREETING_EXACT_SET


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
        real_user_set = {
            idx
            for idx, m in enumerate(latest_turn_msgs)
            if str(m.get("role", "user")).lower() in ("user", "human") and _has_real_user_content(m)
        }
        fallback_user_idx = None
        if not real_user_set:
            user_indices = [
                idx
                for idx, m in enumerate(latest_turn_msgs)
                if str(m.get("role", "user")).lower() in ("user", "human")
            ]
            fallback_user_idx = user_indices[-1] if user_indices else None

        filtered = []
        for idx, m in enumerate(latest_turn_msgs):
            role = str(m.get("role", "user")).lower()
            if role == "tool":
                filtered.append(m)
            elif role in ("user", "human"):
                if idx in real_user_set or idx == fallback_user_idx:
                    filtered.append(m)
            else:
                filtered.append(m)
        if filtered:
            latest_turn_msgs = filtered

    # Nếu không có tool result và có nhiều tin user liên tiếp (vd user hủy "xin chào" rồi gửi "hi"),
    # bỏ qua các tin chào hỏi mồ côi đứng trước tin user cuối cùng.
    if len(latest_turn_msgs) > 1 and not any(
        str(m.get("role", "")).lower() == "tool" for m in latest_turn_msgs
    ):
        user_positions = [
            idx
            for idx, m in enumerate(latest_turn_msgs)
            if str(m.get("role", "user")).lower() in ("user", "human")
        ]
        if len(user_positions) > 1:
            last_user_pos = user_positions[-1]
            cleaned_multi = []
            for idx, m in enumerate(latest_turn_msgs):
                role = str(m.get("role", "user")).lower()
                if role in ("user", "human") and idx < last_user_pos:
                    raw_t, has_img = _extract_text_without_images(m.get("content"))
                    if (not has_img) and _is_single_message_greeting(raw_t):
                        continue
                cleaned_multi.append(m)
            if cleaned_multi:
                latest_turn_msgs = cleaned_multi

    return latest_turn_msgs or [non_system[-1]]


def is_plain_greeting_request(messages: List[Dict[str, Any]]) -> bool:
    """Return true only for a standalone, text-only greeting/chitchat in the latest turn."""
    latest_turn_msgs = _latest_turn_messages(messages)
    if len(latest_turn_msgs) != 1:
        return False

    message = latest_turn_msgs[0]
    if str(message.get("role", "")).lower() not in ("user", "human"):
        return False
    raw_text, has_image = _extract_text_without_images(message.get("content"))
    if has_image:
        return False
    return _is_single_message_greeting(raw_text)


def is_conversational_chat_request(messages: List[Dict[str, Any]]) -> bool:
    """Trả True cho lời chào hoặc câu hỏi trò chuyện ngắn KHÔNG liên quan tới thao tác workspace/code."""
    if is_plain_greeting_request(messages):
        return True
    latest_turn_msgs = _latest_turn_messages(messages)
    if len(latest_turn_msgs) != 1:
        return False
    message = latest_turn_msgs[0]
    if str(message.get("role", "")).lower() not in ("user", "human"):
        return False
    raw_text, has_image = _extract_text_without_images(message.get("content"))
    if has_image:
        return False
    stripped = _strip_injected_boilerplate(raw_text)
    if not stripped or len(stripped) > 160:
        return False
    return not bool(_WORKSPACE_TASK_INDICATORS_RE.search(stripped))


def build_incremental_prompt_and_files(
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    is_first_turn: bool,
    include_context_bootstrap: bool = None,
    *,
    tool_profile: Optional[str] = None,
    allowed_tools: Optional[Sequence[str]] = None,
    blocked_tools: Optional[Sequence[str]] = None,
    breakdown_out: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[str]]:
    """Trích xuất Delta tin nhắn mới nhất để gửi vào ChatSession.

    Khi `is_first_turn=True`:
    - Kèm phần cốt lõi của `system`/`developer` prompt (workspace, OS/Shell, vai trò agent).
    - Nếu cuộc hội thoại đã có lượt trước đó (failover sang worker mới / reset do lỗi 1097 / restart),
      tự động gắn Context Bootstrap để Gemini không bị mất trí nhớ.
    """
    latest_turn_msgs = _latest_turn_messages(messages)
    if not latest_turn_msgs:
        if breakdown_out is not None:
            breakdown_out.update(
                {"user": 5, "sys": 0, "tools": 0, "tool_res": 0, "boot": 0, "tools_kept": 0, "tools_total": 0}
            )
        return "Hello", []

    active_tools, tool_stats = filter_request_tools(
        tools,
        tool_profile=tool_profile,
        allowed_tools=allowed_tools,
        blocked_tools=blocked_tools,
    )

    blocks: List[str] = []
    all_images: List[str] = []
    has_tool_results = False
    is_greeting = is_plain_greeting_request(latest_turn_msgs)
    user_chars = 0
    tool_res_chars = 0
    sys_chars = 0
    tools_chars = 0
    boot_chars = 0

    tool_msg_count = sum(
        1 for m in latest_turn_msgs if str(m.get("role", "user")).lower() == "tool"
    )
    per_tool_budget = MAX_TOOL_RESULT_CHARS
    if tool_msg_count >= 2 and MAX_TOTAL_TOOL_RESULTS_CHARS > 0:
        fair_share = MAX_TOTAL_TOOL_RESULTS_CHARS // tool_msg_count
        per_tool_budget = min(MAX_TOOL_RESULT_CHARS, max(1500, fair_share))

    # Chuẩn bị phần đầu cố định (Primacy zone) ở Turn 1 để USER REQUEST luôn nằm cuối cùng (Recency zone)
    prefix_blocks: List[str] = []
    should_bootstrap = (
        include_context_bootstrap
        if include_context_bootstrap is not None
        else (is_first_turn and not is_greeting)
    )
    if should_bootstrap:
        bootstrap = build_context_bootstrap(messages, len(latest_turn_msgs))
        if bootstrap:
            boot_chars = len(bootstrap)
            prefix_blocks.append(bootstrap)

    tool_header = ""
    if is_first_turn and not is_greeting:
        sys_ctx = extract_system_context(messages)
        if sys_ctx:
            sys_block = "[SYSTEM & WORKSPACE CONTEXT]\n" + sys_ctx
            sys_chars = len(sys_block)
            prefix_blocks.append(sys_block)
        tool_header = compile_tools_prompt(
            active_tools,
            tool_profile=tool_profile,
            allowed_tools=allowed_tools,
            blocked_tools=blocked_tools,
        )
        if tool_header:
            tools_block = "=== WORKSPACE TOOLS ===\n" + tool_header.rstrip()
            tools_chars = len(tools_block)
            prefix_blocks.append(tools_block)

    for m in latest_turn_msgs:
        role = str(m.get("role", "user")).lower()
        content_str, img_paths = extract_text_and_images(m.get("content"))
        all_images.extend(img_paths)

        if role == "tool":
            has_tool_results = True
            content_str = smart_truncate(content_str, max_chars=per_tool_budget)
            tool_id = m.get("name") or m.get("tool_call_id") or "tool"
            block = f"[TOOL_RESULT ({tool_id})]:\n{content_str}"
            tool_res_chars += len(block)
            blocks.append(block)
        else:
            actual_text = extract_actual_user_text(content_str)
            if is_greeting:
                clean_user_text = actual_text
            elif actual_text:
                clean_user_text = sanitize_no_media_triggers(actual_text).strip()
            else:
                clean_user_text = sanitize_no_media_triggers(content_str).strip()

            if is_greeting and not img_paths:
                if not is_first_turn and active_tools:
                    block = (
                        f"{clean_user_text}\n\n"
                        "[NOTE: Reply briefly and naturally in the user's language WITHOUT calling any tools.]"
                    )
                else:
                    block = clean_user_text
                user_chars += len(block)
                blocks.append(block)
            elif len(clean_user_text) < 15 and not active_tools and not img_paths:
                user_chars += len(clean_user_text)
                blocks.append(clean_user_text)
            else:
                img_note = (
                    " (User attached screenshot — analyze the image directly)"
                    if img_paths else ""
                )
                action_scope = (
                    "file, command, search, or subagent action"
                    if tool_stats.get("has_subagent_tools")
                    else "file, command, or search action"
                )
                is_work_request = bool(_WORKSPACE_TASK_INDICATORS_RE.search(clean_user_text))
                if active_tools and is_work_request:
                    # Recency zone: việc workspace -> nhắc GỌI TOOL trước, không để "chat thường" đứng cuối.
                    tool_reminder = (
                        f" This is a workspace task and you HAVE the listed tools: do the {action_scope} now by "
                        'emitting plain-text <tool_call>{"name": "...", "arguments": {...}}</tool_call> tags '
                        "(the local IDE executes them, no turn cap) instead of pasting whole files as chat text."
                    )
                elif active_tools and tool_header:
                    tool_reminder = (
                        " Answer directly in plain text for normal chat/questions; "
                        "invoke listed `<tool_call>` tags directly when workspace action is needed (no turn cap)."
                    )
                elif active_tools:
                    tool_reminder = (
                        f" Answer directly in plain text for normal chat/questions; for any {action_scope}, emit plain-text "
                        '<tool_call>{"name": "...", "arguments": {...}}</tool_call> tags directly in your reply '
                        "(local IDE markup, no turn cap)."
                    )
                else:
                    tool_reminder = ""
                header_tag = "[USER REQUEST]\n" if prefix_blocks else ""
                block = (
                    f"{header_tag}{clean_user_text}{img_note}\n\n"
                    "[NOTE: Reply in the EXACT SAME LANGUAGE as the USER REQUEST above."
                    f"{tool_reminder}]"
                )
                user_chars += len(block)
                blocks.append(block)

    if has_tool_results:
        if active_tools:
            blocks.append(
                "(Continue step-by-step based on the tool result(s) above. Emit plain-text "
                '<tool_call>{"name": "...", "arguments": {...}}</tool_call> '
                "tags for the next local IDE action (no turn cap); otherwise provide the final answer in the user's language.)"
            )
        else:
            blocks.append(
                "(Continue step-by-step based on the tool result(s) above and provide the final answer in the user's language.)"
            )

    all_blocks = prefix_blocks + blocks

    if breakdown_out is not None:
        breakdown_out.update(
            {
                "profile": tool_stats["profile"],
                "tools_total": tool_stats["total"],
                "tools_kept": tool_stats["kept"],
                "tools_dropped": tool_stats["dropped"],
                "user": user_chars,
                "sys": sys_chars,
                "tools": tools_chars,
                "tool_res": tool_res_chars,
                "boot": boot_chars,
            }
        )

    return "\n\n".join(all_blocks), all_images
