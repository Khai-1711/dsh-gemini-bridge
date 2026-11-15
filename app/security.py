import os
import re
from app.config import DEFAULT_PROJECT_DIR, logger


_SYSTEM_DIR_RE = re.compile(
    r"^[A-Z]:\\(WINDOWS|PROGRAM FILES( \(X86\))?|PROGRAMDATA)(\\|$)",
    re.IGNORECASE,
)


def is_safe_path_no_c_drive(path_str: str) -> bool:
    """
    Kiểm tra đường dẫn: Tuyệt đối cấm trỏ vào ổ C:\\ hoặc thư mục hệ thống.
    Dùng realpath() để chặn path traversal (../../Windows/System32).
    """
    if not path_str:
        return True

    try:
        clean_path = path_str.strip()
        if not re.match(r"^[A-Za-z]:", clean_path) and not clean_path.startswith(("\\", "/")):
            target = os.path.join(DEFAULT_PROJECT_DIR, clean_path)
        else:
            target = clean_path
        resolved = os.path.realpath(os.path.abspath(target)).upper()
    except Exception:
        return False

    if resolved.startswith("C:") or resolved.startswith("\\\\"):
        return False

    if _SYSTEM_DIR_RE.search(resolved):
        return False

    return True


def sanitize_shell_command_no_c_drive(cmd: str) -> str:
    """
    Kiểm duyệt lệnh PowerShell/Bash: Chặn mọi truy cập ổ C:\\, Registry, System.
    Bao gồm chặn bypass qua biến môi trường, backtick, .NET API, eval.
    """
    if not cmd or not cmd.strip():
        return "Write-Output 'Empty command skipped.'"

    forbidden_patterns = [
        # === Truy cập trực tiếp ổ C ===
        r"\bC:\\",
        r"\bc:/",
        # === Biến môi trường trỏ tới ổ hệ thống ===
        r"\$env:SystemDrive",
        r"\$env:SystemRoot",
        r"\$env:windir",
        r"\$env:ProgramFiles",
        r"\$env:ProgramData",
        r"\$env:USERPROFILE",
        r"\$env:APPDATA",
        r"\$env:LOCALAPPDATA",
        # === Lệnh di chuyển vào ổ C ===
        r"Set-Location\s+.*C:",
        r"Push-Location\s+.*C:",
        r"\bcd\s+.*C:",
        # === Backtick escape bypass ===
        r"C``:\\",
        # === Lệnh nguy hiểm cấp hệ thống ===
        r"\bformat(\.com)?\s+[a-z]:",
        r"\bdiskpart\b",
        r"\bbcdedit\b",
        r"\breg\s+(add|delete|import|save|export|query)",
        r"\bsc\s+(stop|delete|config|create)",
        r"\bnet\s+user\b",
        r"\bnet\s+localgroup\b",
        r"\bnet\s+share\b",
        r"\btakeown\b",
        r"\bicacls\b.*C:",
        # === Xóa đệ quy nguy hiểm ===
        r"\bRemove-Item\b.*-Recurse.*C:",
        r"\bdel\b.*C:",
        r"\brm\b.*-rf.*C:",
        r"\brd\s+/s",
        # === Code execution / eval nguy hiểm ===
        r"\bInvoke-Expression\b",
        r"\biex\s",
        r"\bStart-Process\b.*C:",
        # === .NET direct file access trên ổ C ===
        r"\[System\.IO\.(File|Directory)\].*C:",
        r"\[System\.Diagnostics\.Process\]",
        # === PowerShell download & execute ===
        r"Invoke-WebRequest.*\|\s*Invoke-Expression",
        r"IWR.*\|\s*IEX",
        r"DownloadString.*\|\s*IEX",
    ]

    for pat in forbidden_patterns:
        if re.search(pat, cmd, flags=re.IGNORECASE):
            logger.warning(f"[SECURITY] Chặn lệnh nguy hiểm: {cmd[:200]}")
            return (
                "Write-Output 'SECURITY BLOCK: Access to C: drive or system "
                "configuration is strictly prohibited. Operate ONLY inside D: drive.'"
            )

    return cmd


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
