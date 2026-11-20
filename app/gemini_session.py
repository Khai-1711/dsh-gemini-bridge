"""Facade tương thích ngược cho SingleChatManager và các hằng số cấu hình kiểm thử."""
from gemini_webapi import GeminiClient  # noqa: F401

from app.config import (
    DISABLE_DRAIN_TIMEOUT,  # noqa: F401
    GEMINI_HARD_TIMEOUT,  # noqa: F401
    IMAGE_REQUEST_TIMEOUT,  # noqa: F401
    INSTANCE_PATHS,
    MIN_REQUEST_INTERVAL,  # noqa: F401
)

SESSION_STATE_FILE = INSTANCE_PATHS["session_file"]
MODEL_FIRST_BYTE_FALLBACK_TIMEOUT = 35.0


def clear_response_cache() -> int:
    """No-op compatibility endpoint after prompt cache removal."""
    return 0


from app.gemini_runtime import SingleChatManager  # noqa: E402
from app.session_pool import (  # noqa: E402
    SessionSlot,
    clear_saved_session,
    compute_session_key,
    is_subagent_messages,
    load_saved_session,
    save_session_state,
)

session_manager = SingleChatManager()

__all__ = [
    "DISABLE_DRAIN_TIMEOUT",
    "GEMINI_HARD_TIMEOUT",
    "GeminiClient",
    "IMAGE_REQUEST_TIMEOUT",
    "INSTANCE_PATHS",
    "MIN_REQUEST_INTERVAL",
    "MODEL_FIRST_BYTE_FALLBACK_TIMEOUT",
    "SESSION_STATE_FILE",
    "SessionSlot",
    "SingleChatManager",
    "clear_response_cache",
    "clear_saved_session",
    "compute_session_key",
    "is_subagent_messages",
    "load_saved_session",
    "save_session_state",
    "session_manager",
]
