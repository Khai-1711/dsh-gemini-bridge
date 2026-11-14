import logging
import logging.handlers
import os
import sys
from pathlib import Path

# =====================================================================
# 1. NẠP BIẾN MÔI TRƯỜNG TỪ FILE .env
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

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

# =====================================================================
# 3. CẤU HÌNH SERVER & HIỆU NĂNG
# =====================================================================
HOST = os.getenv("HOST", "127.0.0.1").strip()
PORT = int(os.getenv("PORT", "10000").strip())

# Giới hạn cứng 90s (< 120s của gemini_webapi)
GEMINI_HARD_TIMEOUT = int(os.getenv("GEMINI_HARD_TIMEOUT", "90"))

# Giãn cách tối thiểu giữa 2 request liên tiếp (giây)
MIN_REQUEST_INTERVAL = float(os.getenv("MIN_REQUEST_INTERVAL", "0.5"))

# Chu kỳ keep-alive SSE (giây)
SSE_HEARTBEAT_INTERVAL = float(os.getenv("SSE_HEARTBEAT_INTERVAL", "2.5"))

# Giới hạn ký tự tool result
MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "4000"))

# Giới hạn ảnh mỗi lượt
MAX_IMAGES_PER_TURN = int(os.getenv("MAX_IMAGES_PER_TURN", "4"))

# Timeout chờ Lock (giây) — trả lỗi nếu chờ quá lâu
LOCK_ACQUIRE_TIMEOUT = float(os.getenv("LOCK_ACQUIRE_TIMEOUT", "30"))

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

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | [DSH-Bridge] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
logger = logging.getLogger("DSH-Bridge")

# File handler: 5MB × 3 backup files
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "bridge.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
logger.addHandler(_file_handler)

# =====================================================================
# 6. STARTUP VALIDATION (Fail-fast)
# =====================================================================
def validate_config():
    """Kiểm tra cấu hình bắt buộc ngay khi khởi động. Dừng sớm nếu thiếu."""
    errors = []
    if not SECURE_1PSID:
        errors.append("SECURE_1PSID is empty!")
    if not SECURE_1PSIDTS:
        errors.append("SECURE_1PSIDTS is empty!")
    if errors:
        for e in errors:
            logger.error(f"[CONFIG] {e}")
        logger.error(
            "Hướng dẫn: Copy .env.example → .env và điền cookie Gemini Web.\n"
            "Lấy cookie tại: F12 → Application → Cookies → gemini.google.com"
        )
        sys.exit(1)

validate_config()
