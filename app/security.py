import os
import re
from app.config import DEFAULT_PROJECT_DIR, WORKSPACE_SANDBOX_MODE, logger


_WINDOWS_SYSTEM_DIR_RE = re.compile(
    r"^[A-Z]:\\(WINDOWS|PROGRAMDATA|SYSTEM VOLUME INFORMATION)(\\|$)",
    re.IGNORECASE,
)

_POSIX_SYSTEM_DIR_RE = re.compile(
    r"^/(etc/shadow|etc/passwd|boot|proc|sys)(/|$)",
    re.IGNORECASE,
)


def is_safe_path(path_str: str, workspace_root: str = "") -> bool:
    """Kiểm tra đường dẫn tệp theo hướng mở theo workspace của người dùng.

    - Không còn giới hạn cứng vào ổ D: hay D:\\ABC.
    - Chặn thư mục lõi hệ điều hành (C:\\Windows, System32, đường dẫn mạng UNC)
      và path traversal đi vào thư mục hệ thống.
    - Khi WORKSPACE_SANDBOX_MODE=workspace và xác định được thư mục dự án,
      kiểm tra thêm đường dẫn nằm trong thư mục dự án đó.
    """
    if not path_str:
        return True

    clean_path = str(path_str).strip()
    if not clean_path:
        return True

    if clean_path.startswith("\\\\") or clean_path.startswith("//"):
        return False

    base_root = (workspace_root or DEFAULT_PROJECT_DIR or "").strip()
    try:
        is_win_abs = bool(re.match(r"^[A-Za-z]:", clean_path))
        if not is_win_abs and not os.path.isabs(clean_path):
            if base_root:
                target = os.path.join(base_root, clean_path)
            else:
                # Relative path without known workspace root: reject explicit traversal into OS dirs
                norm_rel = os.path.normpath(clean_path).replace("/", "\\")
                if re.search(r"(?:^|\\)(Windows|System32)(\\|$)", norm_rel, re.IGNORECASE):
                    return False
                return True
        else:
            target = clean_path

        resolved = os.path.realpath(os.path.abspath(target))
    except Exception:
        return False

    resolved_upper = resolved.upper()
    if resolved_upper.startswith("\\\\"):
        return False
    if _WINDOWS_SYSTEM_DIR_RE.search(resolved_upper):
        return False
    if _POSIX_SYSTEM_DIR_RE.search(resolved.replace("\\", "/")):
        return False

    if WORKSPACE_SANDBOX_MODE == "workspace" and base_root:
        return is_path_within_project(resolved, project_dir=base_root)

    return True


def is_safe_path_no_c_drive(path_str: str, workspace_root: str = "") -> bool:
    """Alias tương thích ngược: cho phép mọi ổ dự án (kể cả C:\\Users\\...), chỉ chặn thư mục lõi OS."""
    return is_safe_path(path_str, workspace_root=workspace_root)


def is_path_within_project(path_str: str, project_dir: str = None) -> bool:
    """Return whether a path resolves inside the specified or configured project directory."""
    if not path_str:
        return False
    root_raw = (project_dir if project_dir is not None else DEFAULT_PROJECT_DIR) or ""
    if not root_raw.strip():
        return True
    try:
        root = os.path.realpath(os.path.abspath(root_raw))
        candidate = path_str.strip()
        if not os.path.isabs(candidate):
            candidate = os.path.join(root, candidate)
        resolved = os.path.realpath(os.path.abspath(candidate))
        return os.path.commonpath(
            [os.path.normcase(root), os.path.normcase(resolved)]
        ) == os.path.normcase(root)
    except (OSError, ValueError):
        return False


def sanitize_shell_command_no_c_drive(cmd: str) -> str:
    """Kiểm duyệt lệnh PowerShell/Bash: cho phép thao tác dev trên mọi ổ đĩa,
    chỉ chặn các lệnh phá hủy hệ điều hành hoặc thực thi mã tải từ mạng nguy hiểm.
    """
    if not cmd or not cmd.strip():
        return "Write-Output 'Empty command skipped.'"

    forbidden_patterns = [
        # === Lệnh phá hủy phân vùng / boot / OS ===
        r"\\Windows\\System32\b",
        r"/c/windows/system32\b",
        r"\bformat(\.com)?\s+[a-z]:",
        r"\bdiskpart\b",
        r"\bbcdedit\b",
        r"\breg\s+(add|delete|import)\s+.*\b(HKLM|HKEY_LOCAL_MACHINE)\b",
        r"\bsc(\.exe)?\s+(stop|delete|config|create)\b",
        r"\bnet\s+(user|localgroup|share)\b",
        r"\btakeown\b.*\\Windows\b",
        r"\bicacls\b.*\\Windows\b",
        # === Xóa đệ quy nhắm vào gốc ổ đĩa hoặc thư mục Windows ===
        r"\bRemove-Item\b.*-Recurse.*(?:[A-Za-z]:\\(?:Windows|ProgramData)\b|['\"]?[A-Za-z]:\\['\"]?(?:\s|$))",
        r"\b(?:rd|rmdir)\b.*(?:/s|/q).*(?:[A-Za-z]:\\(?:Windows|ProgramData)\b|['\"]?[A-Za-z]:\\['\"]?(?:\s|$))",
        r"\brm\b\s+-[a-z]*r[a-z]*f[a-z]*\s+(?:/(?:\s|$|\*)|[A-Za-z]:[/\\](?:Windows)?(?:\s|$))",
        r"\bdel\b\s+.*\\Windows\\",
        # === Tải mã từ Internet rồi chạy thẳng (download & execute) ===
        r"Invoke-WebRequest.*\|\s*(?:Invoke-Expression|iex)\b",
        r"\bIWR\b.*\|\s*(?:Invoke-Expression|iex)\b",
        r"DownloadString.*\|\s*(?:Invoke-Expression|iex)\b",
        r"\b(?:powershell|pwsh)(?:\.exe)?\s+.*-(?:e|enc|encodedcommand)\b",
        r"\bcertutil(?:\.exe)?\s+.*-(?:urlcache|decode)\b",
    ]

    for pat in forbidden_patterns:
        if re.search(pat, cmd, flags=re.IGNORECASE):
            logger.warning("[SECURITY] Blocked destructive OS-level command.")
            return (
                "Write-Output 'SECURITY BLOCK: Destructive OS-level or encoded "
                "remote execution command is prohibited.'"
            )

    return cmd


sanitize_shell_command = sanitize_shell_command_no_c_drive


def sanitize_no_media_triggers(text: str) -> str:
    """Làm sạch từ khóa kích hoạt nhầm Veo/Imagen trên Gemini Web."""
    if not text:
        return ""
    return re.sub(
        r"\b(generate|create|make)\s+(a\s+)?(video|movie|animation|clip)\b",
        r"write code for \3",
        text,
        flags=re.IGNORECASE,
    )
