import logging
import logging.handlers
import math
import os
import re
import sys
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# =====================================================================
# 1. NẠP BIẾN MÔI TRƯỜNG TỪ FILE .env
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent.parent


def sanitize_instance_id(raw: str) -> str:
    """ID instance: rỗng (kiểu cũ) hoặc 1-16 ký tự chữ/số/_/-. Chặn ký tự đường dẫn."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", raw):
        raise ValueError(f"BRIDGE_INSTANCE không hợp lệ: {raw!r}")
    return raw


def build_instance_paths(base_dir: Path, instance_id: str) -> dict:
    """DUY NHẤT một nơi quyết định tên file theo instance."""
    sfx = f"_{instance_id}" if instance_id else ""
    return {
        "env_file": base_dir / (f".env.{instance_id}" if instance_id else ".env"),
        "session_file": base_dir / f".bridge_session{sfx}.json",
        "log_file": base_dir / "logs" / f"bridge{sfx}.log",
        "activity_file": base_dir / "logs" / f"activity{sfx}.jsonl",
        "cookie_dir": base_dir / f".gemini_cookies{sfx}",
        "accounts_state_file": base_dir / f".bridge_accounts{sfx}.json",
        "chain_file": base_dir / f".bridge_chain{sfx}.json",
        "handoff_dir": base_dir / "handoff" / (instance_id or "default"),
    }


# BRIDGE_INSTANCE phải đọc từ MÔI TRƯỜNG TRƯỚC khi nạp file .env (vì load_dotenv
# dùng override=True). Đây là biến DUY NHẤT đặt ngoài file .env.
try:
    INSTANCE_ID = sanitize_instance_id(os.getenv("BRIDGE_INSTANCE", ""))
except ValueError as _exc:
    print(f"[LỖI CẤU HÌNH] {_exc}. Chỉ dùng chữ/số/_/-, tối đa 16 ký tự (ví dụ: acc1).")
    sys.exit(1)
INSTANCE_PATHS = build_instance_paths(BASE_DIR, INSTANCE_ID)
ENV_FILE = INSTANCE_PATHS["env_file"]
if INSTANCE_ID and not ENV_FILE.exists():
    print(f"[LỖI CẤU HÌNH] Instance '{INSTANCE_ID}' cần file {ENV_FILE.name} nhưng không thấy. "
          f"Hãy copy .env.example thành {ENV_FILE.name} rồi điền cookie.")
    sys.exit(1)
INSTANCE_TAG = f":{INSTANCE_ID}" if INSTANCE_ID else ""

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=ENV_FILE, override=True)
except ImportError:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

# Thư viện gemini-webapi lưu cookie đã xoay (rotated) vào thư mục này và ưu
# tiên dùng chúng. Đặt cố định trong dự án để không bị Windows dọn thư mục temp.
_COOKIE_CACHE_DIR = INSTANCE_PATHS["cookie_dir"]
_COOKIE_CACHE_DIR.mkdir(exist_ok=True)
os.environ.setdefault("GEMINI_COOKIE_PATH", str(_COOKIE_CACHE_DIR))

# =====================================================================
# 2. THÔNG TIN XÁC THỰC GEMINI WEB (COOKIES)
# =====================================================================
SECURE_1PSID = os.getenv("SECURE_1PSID", "").strip()
SECURE_1PSIDTS = os.getenv("SECURE_1PSIDTS", "").strip()

# Cookie dự phòng (Failover Pool)
BACKUP_1PSID = os.getenv("BACKUP_1PSID", "").strip()
BACKUP_1PSIDTS = os.getenv("BACKUP_1PSIDTS", "").strip()

# API Key xác thực (rỗng = tắt xác thực)
API_KEY = os.getenv("API_KEY", "").strip()
ALLOW_INSECURE_LAN = os.getenv("ALLOW_INSECURE_LAN", "false").strip().lower() in ("1", "true", "yes")
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
LANE_ADMIN_TOKEN = os.getenv("LANE_ADMIN_TOKEN", "").strip()

# =====================================================================
# 3. CẤU HÌNH SERVER & HIỆU NĂNG
# =====================================================================
from app.control import parse_start_state

HOST = os.getenv("HOST", "127.0.0.1").strip()
PORT = int(os.getenv("PORT", "10000").strip())
INSTANCE_LABEL = os.getenv("INSTANCE_LABEL", "").strip()   # nhãn dễ đọc, vd "Frontend"
BRIDGE_LOG_FILE = INSTANCE_PATHS["log_file"]

API_START_ON = parse_start_state(os.getenv("API_START_STATE", "on"))
DISABLE_DRAIN_TIMEOUT = float(os.getenv("DISABLE_DRAIN_TIMEOUT", "120"))

# Giới hạn cứng 90s (< 120s của gemini_webapi)
GEMINI_HARD_TIMEOUT = int(os.getenv("GEMINI_HARD_TIMEOUT", "90"))

# Total deadline for image upload, OCR fallback, and the Gemini response.
IMAGE_REQUEST_TIMEOUT = float(os.getenv("IMAGE_REQUEST_TIMEOUT", "90"))
if not math.isfinite(IMAGE_REQUEST_TIMEOUT) or not 0 < IMAGE_REQUEST_TIMEOUT <= 120:
    raise ValueError("IMAGE_REQUEST_TIMEOUT must be greater than 0 and at most 120 seconds.")

# Giãn cách tối thiểu giữa 2 request liên tiếp (giây)
MIN_REQUEST_INTERVAL = float(os.getenv("MIN_REQUEST_INTERVAL", "0.5"))

# Chu kỳ keep-alive SSE (giây)
SSE_HEARTBEAT_INTERVAL = float(os.getenv("SSE_HEARTBEAT_INTERVAL", "2.5"))

# Giới hạn ký tự tool result (mặc định 12000)
MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "12000"))

# Giới hạn ảnh mỗi lượt
MAX_IMAGES_PER_TURN = int(os.getenv("MAX_IMAGES_PER_TURN", "4"))

# Timeout chờ Lock (giây) — trả lỗi nếu chờ quá lâu
LOCK_ACQUIRE_TIMEOUT = float(os.getenv("LOCK_ACQUIRE_TIMEOUT", "30"))

# Local tool-call repair chỉ đánh giá shadow; kết quả không được gửi cho DSH.
TOOL_REPAIR_ENABLED = os.getenv("TOOL_REPAIR_ENABLED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)
TOOL_REPAIR_ENDPOINT = os.getenv(
    "TOOL_REPAIR_ENDPOINT",
    "http://127.0.0.1:8000/v1/chat/completions",
).strip()
TOOL_REPAIR_TRUSTED_IPS = tuple(
    ip.strip()
    for ip in os.getenv("TOOL_REPAIR_TRUSTED_IPS", "").split(",")
    if ip.strip()
)
TOOL_REPAIR_MODEL = os.getenv("TOOL_REPAIR_MODEL", "").strip()
TOOL_REPAIR_TIMEOUT_SECONDS = float(os.getenv("TOOL_REPAIR_TIMEOUT_SECONDS", "2"))
if (
    not math.isfinite(TOOL_REPAIR_TIMEOUT_SECONDS)
    or not 0 < TOOL_REPAIR_TIMEOUT_SECONDS <= 10
):
    raise ValueError("TOOL_REPAIR_TIMEOUT_SECONDS must be greater than 0 and at most 10.")

# Bật bộ nhớ đệm câu trả lời (mặc định tắt; chỉ bật khi hỏi đáp tĩnh, không dùng cho code agent)
ENABLE_RESPONSE_CACHE = os.getenv("ENABLE_RESPONSE_CACHE", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)

# =====================================================================
# 3b. CẤU HÌNH MULTI-ACCOUNT CHAIN & QUOTA HANDOFF
# =====================================================================
from app.accounts import build_account_chain

ACCOUNT_CHAIN = build_account_chain(os.environ)
ALLOW_GUEST_MODE = os.getenv("ALLOW_GUEST_MODE", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
QUOTA_COOLDOWN_HOURS = float(os.getenv("QUOTA_COOLDOWN_HOURS", "24"))
AUTH_COOLDOWN_MINUTES = float(os.getenv("AUTH_COOLDOWN_MINUTES", "60"))
HANDOFF_ENABLE = os.getenv("HANDOFF_ENABLE", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
HANDOFF_MAX_CHARS = int(os.getenv("HANDOFF_MAX_CHARS", "2500"))
HANDOFF_SOURCE_MAX_CHARS = int(os.getenv("HANDOFF_SOURCE_MAX_CHARS", "12000"))
HANDOFF_RECENT_TURNS = int(os.getenv("HANDOFF_RECENT_TURNS", "2"))
REPLAY_MAX_CHARS = int(os.getenv("REPLAY_MAX_CHARS", "8000"))
HANDOFF_FLASH_TIMEOUT = float(os.getenv("HANDOFF_FLASH_TIMEOUT", "60"))
BRIDGE_DEBUG_FAKE_QUOTA = os.getenv("BRIDGE_DEBUG_FAKE_QUOTA", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)

# =====================================================================
# 4. CẤU HÌNH BẢO MẬT Ổ ĐĨA (SANDBOX JAIL)
# =====================================================================
DEFAULT_PROJECT_DIR = os.getenv("DEFAULT_PROJECT_DIR", r"D:\ABC").strip()
FORBIDDEN_TOOLS = {"read_image", "generate_video", "generate_image"}

# =====================================================================
# 5. LOGGING (Console + File Rotation)
# =====================================================================
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FORMAT = f"%(asctime)s | %(levelname)-7s | [DSH-Bridge{INSTANCE_TAG}] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
logger = logging.getLogger("DSH-Bridge")

# File handler: 5MB × 3 backup files
_file_handler = logging.handlers.RotatingFileHandler(
    BRIDGE_LOG_FILE,
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
logger.addHandler(_file_handler)

# =====================================================================
# 5.1. CẤU HÌNH LIÊN KẾT NHIỀU LUỒNG (PEER LANES)
# =====================================================================
from app.lane_security import parse_peer_lanes

PEER_LANES = parse_peer_lanes(os.getenv("PEER_LANES", ""), INSTANCE_ID)
if PEER_LANES and not INSTANCE_ID:
    if PORT == 10000:
        logger.info("[GATEWAY] Chạy chế độ Frontend Gateway trên port 10000. Các luồng worker: %s", list(PEER_LANES.keys()))
    else:
        logger.warning(
            "[LANE] Đang chạy KHÔNG có BRIDGE_INSTANCE (ID 'default') nhưng có PEER_LANES. "
            "Hai luồng cần ID acc1/acc2 (dùng run_acc1.bat / run_acc2.bat)."
        )
if PEER_LANES and not LANE_ADMIN_TOKEN:
    logger.warning(
        "[LANE] Có PEER_LANES nhưng thiếu LANE_ADMIN_TOKEN: chỉ XEM được luồng kia, KHÔNG điều khiển được."
    )

# =====================================================================
# 6. STARTUP VALIDATION (Fail-fast)
# =====================================================================
def validate_config():
    """Kiểm tra cấu hình bắt buộc ngay khi khởi động. Dừng sớm nếu thiếu (trừ khi bật ALLOW_GUEST_MODE)."""
    errors = []
    if not ACCOUNT_CHAIN:
        errors.append("SECURE_1PSID is empty!")
        if not SECURE_1PSIDTS:
            errors.append("SECURE_1PSIDTS is empty!")
    elif not SECURE_1PSIDTS:
        errors.append("SECURE_1PSIDTS is empty!")
    if errors:
        if ALLOW_GUEST_MODE:
            logger.info(
                "[CONFIG] Không có cookie SECURE_1PSID/SECURE_1PSIDTS. "
                "Cấu hình ALLOW_GUEST_MODE=true -> Bridge sẽ chạy ở chế độ KHÁCH (Guest Mode) mặc định."
            )
            return
        for e in errors:
            logger.error(f"[CONFIG] {e}")
        logger.error(
            "Hướng dẫn: Copy .env.example → .env và điền cookie Gemini Web.\n"
            "Lấy cookie tại: F12 → Application → Cookies → gemini.google.com"
        )
        sys.exit(1)

validate_config()
