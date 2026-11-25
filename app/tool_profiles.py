"""Lọc danh sách công cụ theo Profile (Tool Allowlist / Denylist) để giảm phình Turn 1
và ngăn Gemini Web tự ý gọi subagent đẻ đoạn chat mới ngoài ý muốn.

Module THUẦN TÚY: không import app.config để dùng lại an toàn ở mọi tầng và dễ test offline.
"""
import fnmatch
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

VALID_TOOL_PROFILES: Tuple[str, ...] = (
    "no_subagents",
    "coding",
    "coding_parallel",
    "research",
    "none",
    "all",
)

PROFILE_DESCRIPTIONS: Dict[str, str] = {
    "no_subagents": "Giữ toàn bộ tool của IDE/MCP, chỉ chặn các tool đẻ subagent/swarm để không tự mở chat web mới.",
    "coding": "Chỉ giữ bộ tool lập trình cốt lõi (read/edit/write/glob/grep/bash/search), bỏ subagent & MCP thừa để nhẹ Turn 1 tối đa.",
    "coding_parallel": "Giữ bộ tool lập trình cốt lõi + cho phép gọi subagent chạy song song nhiều lane (acc1/acc2).",
    "research": "Chỉ giữ tool đọc & tra cứu (read/search/web/fetch), chặn ghi file, shell và subagent.",
    "none": "Tắt toàn bộ tool (thuần hội thoại / JSON — hợp cho web tranh luận đa AI).",
    "all": "Giữ nguyên mọi tool từ client (chỉ chặn FORBIDDEN_TOOLS và BLOCKED_TOOLS).",
}

_EXACT_SUBAGENT_TOOLS = frozenset(
    {
        "agent",
        "subagent",
        "subagents",
        "subagent_fork",
        "list_subagent_models",
        "list_agents",
        "wait_agent",
        "stop_agent",
        "kill_agent",
        "resume_agent",
        "message_agent",
        "task",
        "spawn_agent",
        "spawn_subagent",
        "invoke_subagent",
        "define_subagent",
        "manage_subagents",
        "create_agent",
        "run_subagent",
        "delegate",
        "delegate_task",
        "swarm",
        "workflow",
        "run_workflow",
        "send_message",
        "create_teammate",
        "spawn_worker",
        "manage_workers",
        "new_task",
        "switch_mode",
    }
)

_SUBAGENT_NAME_RE = re.compile(
    r"(?:^|_|-)(?:subagents?|swarm|teammates?)(?:$|_|-)"
    r"|^(?:spawn|invoke|define|manage|create|run|delegate)_(?:sub)?agent"
    r"|^(?:team|worker)_[a-z0-9_]+$",
    re.IGNORECASE,
)

_EXACT_CODING_TOOLS = frozenset(
    {
        # File read/inspect
        "read",
        "read_file",
        "view_file",
        "view_file_outline",
        "view_code_item",
        "cat",
        "get_file",
        "open_file",
        # File write/edit
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
        # Search & directory listing
        "ls",
        "list_dir",
        "list_directory",
        "list_files",
        "directory_tree",
        "dir",
        "tree",
        "glob",
        "find",
        "find_files",
        "find_by_name",
        "file_search",
        "text_search",
        "grep",
        "grep_search",
        "ripgrep",
        "rg",
        "search_files",
        "search_code",
        "codebase_search",
        "get_file_outline",
        "symbol_search",
        "workspace_symbols",
        # Shell & execution
        "bash",
        "sh",
        "shell",
        "powershell",
        "pwsh",
        "cmd",
        "terminal",
        "run_command",
        "run_terminal_cmd",
        "run_shell_command",
        "run_in_terminal",
        "read_terminal",
        "execute_command",
        "exec_command",
        "command_status",
        "send_command_input",
        "run_tests",
        "working_directory",
        "job_output",
        "job_list",
        "job_kill",
        # Git & diagnostics / web lookup / lightweight planning & DSH workspace helpers
        "create_worktree",
        "load_workspace_dependencies",
        "skill",
        "present",
        "ask_user_question",
        "ask_question",
        "git_status",
        "git_diff",
        "git_log",
        "get_diagnostics",
        "get_errors",
        "diagnostics",
        "lint",
        "web_search",
        "search_web",
        "web_fetch",
        "fetch_url",
        "fetch_webpage",
        "read_url_content",
        "fetch",
        "todo_write",
        "todowrite",
        "todo_read",
        "todoread",
        "update_plan",
    }
)

_EDIT_TOOL_HINTS = frozenset(
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
    }
)

_EXACT_RESEARCH_TOOLS = frozenset(
    {
        "read",
        "read_file",
        "view_file",
        "view_file_outline",
        "view_code_item",
        "ls",
        "list_dir",
        "list_files",
        "glob",
        "find",
        "find_files",
        "find_by_name",
        "grep",
        "grep_search",
        "ripgrep",
        "rg",
        "search_files",
        "search_code",
        "codebase_search",
        "web_search",
        "search_web",
        "web_fetch",
        "fetch_url",
        "read_url_content",
        "fetch",
    }
)


def normalize_tool_profile(raw: Any, default: str = "no_subagents") -> str:
    """Chuẩn hóa tên profile; trả về `default` nếu rỗng hoặc không hợp lệ."""
    text = str(raw or "").strip().lower().replace("-", "_")
    if text in VALID_TOOL_PROFILES:
        return text
    return default


def parse_tool_list(raw: Any) -> Tuple[str, ...]:
    """Tách danh sách tên/pattern tool từ chuỗi cấu hình (phân cách bằng dấu phẩy/chấm phẩy/xuống dòng)."""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple, set, frozenset)):
        items = [str(x).strip().lower() for x in raw]
    else:
        items = [s.strip().lower() for s in re.split(r"[,;\r\n]+", str(raw))]
    seen: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return tuple(seen)


def _matches_any(name_lower: str, patterns: Sequence[str]) -> bool:
    for pat in patterns:
        if not pat:
            continue
        if pat == name_lower or fnmatch.fnmatchcase(name_lower, pat):
            return True
    return False


def is_subagent_tool_name(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return False
    if low in _EXACT_SUBAGENT_TOOLS:
        return True
    return bool(_SUBAGENT_NAME_RE.search(low))


def is_coding_tool_name(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return False
    if low in _EXACT_CODING_TOOLS:
        return True
    return bool(
        re.match(
            r"^(?:file|code|workspace|fs|git)_(?:read|write|edit|search|list|find|diff|status|log|grep|glob)$",
            low,
        )
    )


def is_research_tool_name(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return False
    if low in _EXACT_RESEARCH_TOOLS:
        return True
    return bool(
        re.match(
            r"^(?:web|doc|docs|paper|arxiv|wiki|search|fetch|read)_[a-z0-9_]+$",
            low,
        )
    )


def is_edit_tool_name(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return False
    if low in _EDIT_TOOL_HINTS:
        return True
    return any(tok in low for tok in ("edit_file", "write_file", "apply_patch", "apply_diff", "replace_file"))


def filter_tools(
    tools: Optional[Iterable[Dict[str, Any]]],
    *,
    profile: str = "no_subagents",
    allowed_patterns: Sequence[str] = (),
    blocked_patterns: Sequence[str] = (),
    forbidden_tools: Optional[Iterable[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Lọc danh sách OpenAI tool definitions theo profile + allow/block override.

    Thứ tự ưu tiên:
      1. `forbidden_tools` (bảo vệ hệ thống) -> luôn chặn.
      2. `blocked_patterns` (`BLOCKED_TOOLS`) -> luôn chặn.
      3. `allowed_patterns` (`ALLOWED_TOOLS`) -> luôn cho phép (vượt profile).
      4. Quy tắc profile (`none`, `all`, `no_subagents`, `coding`, `coding_parallel`, `research`).
    """
    prof = normalize_tool_profile(profile)
    forbidden = {str(x).strip().lower() for x in (forbidden_tools or ()) if str(x).strip()}
    allowed = parse_tool_list(allowed_patterns)
    blocked = parse_tool_list(blocked_patterns)

    kept: List[Dict[str, Any]] = []
    dropped_names: List[str] = []
    total = 0

    for item in tools or ():
        if not isinstance(item, dict):
            continue
        fn = item.get("function", item)
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        total += 1
        low = name.lower()

        if low in forbidden or _matches_any(low, blocked):
            dropped_names.append(name)
            continue

        if allowed and _matches_any(low, allowed):
            kept.append(item)
            continue

        keep = False
        if prof == "all":
            keep = True
        elif prof == "none":
            keep = False
        elif prof == "no_subagents":
            keep = not is_subagent_tool_name(low)
        elif prof == "coding":
            keep = is_coding_tool_name(low) and not is_subagent_tool_name(low)
        elif prof == "coding_parallel":
            keep = is_coding_tool_name(low) or is_subagent_tool_name(low)
        elif prof == "research":
            keep = is_research_tool_name(low) and not is_subagent_tool_name(low)

        if keep:
            kept.append(item)
        else:
            dropped_names.append(name)

    kept_names = [
        str((t.get("function", t) or {}).get("name") or "").strip()
        for t in kept
    ]
    stats = {
        "profile": prof,
        "total": total,
        "kept": len(kept),
        "dropped": len(dropped_names),
        "dropped_names": dropped_names,
        "has_subagent_tools": any(is_subagent_tool_name(n) for n in kept_names),
        "has_edit_tools": any(is_edit_tool_name(n) for n in kept_names),
    }
    return kept, stats


def extract_forced_tool_names(tool_choice: Any) -> Tuple[str, ...]:
    """Trích tên tool được client chỉ định tường minh trong `tool_choice` để không bao giờ lọc nhầm."""
    if not isinstance(tool_choice, dict):
        return ()
    fn = tool_choice.get("function")
    if isinstance(fn, dict):
        name = str(fn.get("name") or "").strip().lower()
        return (name,) if name else ()
    name = str(tool_choice.get("name") or "").strip().lower()
    return (name,) if name else ()


def describe_profiles() -> List[Dict[str, str]]:
    return [
        {"id": pid, "description": PROFILE_DESCRIPTIONS.get(pid, "")}
        for pid in VALID_TOOL_PROFILES
    ]
