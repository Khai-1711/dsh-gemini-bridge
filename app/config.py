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
    state_dir = base_dir / ".bridge_state"
    return {
        "env_file": base_dir / (f".env.{instance_id}" if instance_id else ".env"),
        "session_file": state_dir / f".bridge_session{sfx}.json",
        "log_file": base_dir / "logs" / f"bridge{sfx}.log",
        "activity_file": base_dir / "logs" / f"activity{sfx}.jsonl",
        "cookie_dir": base_dir / f".gemini_cookies{sfx}",
        "accounts_state_file": state_dir / f".bridge_accounts{sfx}.json",
        "chain_file": state_dir / f".bridge_chain{sfx}.json",
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
# tiên dùng chúng. Đặt cố định theo từng instance (tránh thừa hưởng từ tiến trình Gateway cha).
_COOKIE_CACHE_DIR = INSTANCE_PATHS["cookie_dir"]
_COOKIE_CACHE_DIR.mkdir(exist_ok=True)
_env_explicit_cookie_path = False
if ENV_FILE.exists():
    try:
        for _ln in ENV_FILE.read_text(encoding="utf-8").splitlines():
            _s = _ln.strip()
            if _s.startswith("GEMINI_COOKIE_PATH=") and _s.split("=", 1)[1].strip().strip('"').strip("'"):
                _env_explicit_cookie_path = True
                break
    except OSError:
        pass
if not _env_explicit_cookie_path:
    os.environ["GEMINI_COOKIE_PATH"] = str(_COOKIE_CACHE_DIR)
else:
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
GATEWAY_MODE = os.getenv("GATEWAY_MODE", "auto").strip().lower()
BRIDGE_LOG_FILE = INSTANCE_PATHS["log_file"]

# Model do người dùng chủ động chọn (rỗng = giữ lựa chọn Dashboard hoặc mặc định tài khoản)
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "").strip()
# Model dự phòng khi timeout/lỗi (rỗng = KHÔNG tự ý đổi sang model khác sau lưng người dùng)
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "").strip()

# Tự sửa nội bộ tối đa 1 vòng khi Gemini trả tool call lỗi hoặc phản hồi trống
TOOL_PARSE_SELF_CORRECTION = os.getenv("TOOL_PARSE_SELF_CORRECTION", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
EMPTY_REPLY_RETRIES = max(0, int(os.getenv("EMPTY_REPLY_RETRIES", "1")))
# Stream bị Google cắt giữa chừng nhưng client CHƯA thấy chữ hiển thị nào (vd. đang sinh <tool_call>
# bị giữ lại) -> chờ một chút rồi thử lại trên metadata an toàn trước lượt.
# Thực tế (2026-11-25): Google trả mã 1213 khi một tài khoản mở chat mới ~1s sau lượt trước; thử lại sau 4s
# vẫn dính -> mặc định 2 lần, giãn dần (20s, 40s). Chế độ chạy nền bền: ưu tiên khỏe tài khoản hơn tốc độ.
MID_STREAM_RETRIES = max(0, int(os.getenv("MID_STREAM_RETRIES", "2")))
MID_STREAM_RETRY_DELAY = max(0.0, float(os.getenv("MID_STREAM_RETRY_DELAY", "20")))
# Việc workspace mà Gemini in code ra chat (khối code >= N dòng) và không gọi tool nào
# -> tự nhắc 1 lượt kín bắt phát <tool_call> (giống người dùng nhắc tay).
# Mở chat MỚI trên cùng một tài khoản phải cách request trước ít nhất N giây (lượt tiếp trong chat cũ vẫn
# dùng MIN_REQUEST_INTERVAL). Mở chat dồn dập ~1s là lúc Google trả 1213 / dễ bị coi là bot.
NEW_CHAT_MIN_INTERVAL = max(0.0, float(os.getenv("NEW_CHAT_MIN_INTERVAL", "25")))
# Gateway: hội thoại LEAD đang dính một luồng thì chờ luồng đó rảnh tối đa N giây thay vì chuyển luồng
# (chuyển luồng = mất ChatSession, phải dựng lại từ tóm tắt). Lead nằm ở luồng nào là động, không cố định acc1.
LEAD_STICKY_LOCK_WAIT = max(1.0, float(os.getenv("LEAD_STICKY_LOCK_WAIT", "240")))
# Google trả mã 1213 liên tiếp = tài khoản đang bị bóp (thực tế 25/11: acc1 bị 12 lần, acc2 0 lần).
# Sau N lần liên tiếp -> luồng tự NGHỈ M phút; gateway né luồng đó cho tới khi hết nghỉ.
RATE_LIMIT_REST_AFTER = max(1, int(os.getenv("RATE_LIMIT_REST_AFTER", "1")))
RATE_LIMIT_REST_MINUTES = max(0.0, float(os.getenv("RATE_LIMIT_REST_MINUTES", "45")))
# Chế độ chạy nền bền: mỗi lượt cách lượt trước thêm một khoảng ngẫu nhiên [MIN, MAX] giây
# (nhịp giống người, tránh đều tăm tắp như bot). Đặt MAX=0 để tắt.
TURN_JITTER_MIN = max(0.0, float(os.getenv("TURN_JITTER_MIN", "3")))
TURN_JITTER_MAX = max(TURN_JITTER_MIN, float(os.getenv("TURN_JITTER_MAX", "8")))
CODE_DUMP_NUDGE = os.getenv("CODE_DUMP_NUDGE", "true").strip().lower() in ("1", "true", "yes")
CODE_DUMP_MIN_LINES = max(1, int(os.getenv("CODE_DUMP_MIN_LINES", "20")))

API_START_ON = parse_start_state(os.getenv("API_START_STATE", "on"))
DISABLE_DRAIN_TIMEOUT = float(os.getenv("DISABLE_DRAIN_TIMEOUT", "120"))

# Giới hạn chờ phản hồi đầu tiên (mặc định 180s = 3 phút cho các model thinking nặng)
GEMINI_HARD_TIMEOUT = int(os.getenv("GEMINI_HARD_TIMEOUT", "180"))

# Timeout chờ chunk đầu tiên (thoughts hoặc text) trước khi soft-fallback sang model khác (giây)
MODEL_FIRST_BYTE_FALLBACK_TIMEOUT = float(
    os.getenv("MODEL_FIRST_BYTE_FALLBACK_TIMEOUT", "90")
)

# Idle timeout giữa các chunk sau khi đã nhận thoughts_delta hoặc text_delta (giây)
GEMINI_CHUNK_IDLE_TIMEOUT = float(
    os.getenv("GEMINI_CHUNK_IDLE_TIMEOUT", "120")
)

# Trần tổng thời gian cho 1 lượt stream sau khi model đã bắt đầu phản hồi (giây, mặc định 7 phút)
GEMINI_STREAM_OVERALL_TIMEOUT = float(
    os.getenv("GEMINI_STREAM_OVERALL_TIMEOUT", str(max(420, GEMINI_HARD_TIMEOUT)))
)

# Total deadline for direct image upload and the Gemini response.
IMAGE_REQUEST_TIMEOUT = float(os.getenv("IMAGE_REQUEST_TIMEOUT", "90"))
if not math.isfinite(IMAGE_REQUEST_TIMEOUT) or not 0 < IMAGE_REQUEST_TIMEOUT <= 120:
    raise ValueError("IMAGE_REQUEST_TIMEOUT must be greater than 0 and at most 120 seconds.")

# Giãn cách tối thiểu giữa 2 request liên tiếp (giây)
MIN_REQUEST_INTERVAL = float(os.getenv("MIN_REQUEST_INTERVAL", "0.5"))

# Chu kỳ keep-alive SSE (giây)
SSE_HEARTBEAT_INTERVAL = float(os.getenv("SSE_HEARTBEAT_INTERVAL", "2.5"))

# Giới hạn ký tự tool result (mỗi kết quả 12000; tổng 1 lượt nhiều tool song song tối đa 36000)
MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "12000"))
MAX_TOTAL_TOOL_RESULTS_CHARS = int(os.getenv("MAX_TOTAL_TOOL_RESULTS_CHARS", "36000"))

# Giới hạn ảnh mỗi lượt
MAX_IMAGES_PER_TURN = int(os.getenv("MAX_IMAGES_PER_TURN", "4"))

# Timeout chờ Lock (giây) — trả lỗi nếu chờ quá lâu
LOCK_ACQUIRE_TIMEOUT = float(os.getenv("LOCK_ACQUIRE_TIMEOUT", "30"))

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
# 3c. CẤU HÌNH PROFILE VAULT & AUTO COOKIE SYNC (KHÔNG CẦN MỞ TAB)
# =====================================================================
_RAW_PROFILES_DIR = os.getenv("COOKIE_PROFILES_DIR", "").strip()
COOKIE_PROFILES_DIR = (
    Path(_RAW_PROFILES_DIR) if _RAW_PROFILES_DIR else (BASE_DIR / ".cookie_profiles")
)
AUTO_COOKIE_SYNC = os.getenv("AUTO_COOKIE_SYNC", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
COOKIE_SYNC_BROWSER = os.getenv("COOKIE_SYNC_BROWSER", "auto").strip()
COOKIE_SYNC_TIMEOUT = max(10.0, float(os.getenv("COOKIE_SYNC_TIMEOUT", "25")))
COOKIE_SYNC_COOLDOWN = max(15.0, float(os.getenv("COOKIE_SYNC_COOLDOWN", "120")))

# =====================================================================
# 4. CẤU HÌNH WORKSPACE & AN TOÀN HỆ THỐNG
# =====================================================================
# Rỗng = mở tự do theo thư mục người dùng đang mở trên IDE (không khóa cứng ổ nào)
DEFAULT_PROJECT_DIR = os.getenv("DEFAULT_PROJECT_DIR", "").strip()
# client = tin tưởng phạm vi thư mục của IDE/đoạn chat (chỉ chặn lệnh phá OS cực đoan)
# workspace = khóa mềm đường dẫn file vào workspace nhận diện được từ đoạn chat / DEFAULT_PROJECT_DIR
from app.tool_profiles import normalize_tool_profile, parse_tool_list

WORKSPACE_SANDBOX_MODE = os.getenv("WORKSPACE_SANDBOX_MODE", "client").strip().lower()
FORBIDDEN_TOOLS = {"read_image", "generate_video", "generate_image"}
# Profile lọc tool để giảm phình prompt Turn 1 & ngăn tự ý đẻ subagent mở chat web mới:
# no_subagents (mặc định an toàn) | coding | coding_parallel | research | none | all
TOOL_PROFILE = normalize_tool_profile(os.getenv("TOOL_PROFILE", "no_subagents"))
ALLOWED_TOOLS = parse_tool_list(os.getenv("ALLOWED_TOOLS", ""))
BLOCKED_TOOLS = parse_tool_list(os.getenv("BLOCKED_TOOLS", ""))

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
IS_GATEWAY_INSTANCE = bool(PEER_LANES) and (
    GATEWAY_MODE in ("1", "true", "yes", "on")
    or (
        GATEWAY_MODE not in ("0", "false", "no", "off")
        and (not INSTANCE_ID or INSTANCE_ID in ("default", "frontend", "gateway"))
    )
)
if IS_GATEWAY_INSTANCE:
    logger.info(
        "[GATEWAY] Chạy chế độ Frontend Gateway trên port %s. Các luồng worker: %s",
        PORT,
        list(PEER_LANES.keys()),
    )
elif PEER_LANES and not INSTANCE_ID:
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
    elif not any(acc.get("psidts") for acc in ACCOUNT_CHAIN):
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
