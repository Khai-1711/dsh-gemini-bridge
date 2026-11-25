"""Dựng payload JSON trạng thái chẩn đoán cho SingleChatManager."""
from datetime import datetime
import time
from typing import Any, Dict

from app.config import INSTANCE_ID, INSTANCE_LABEL, INSTANCE_PATHS, PORT, TOOL_PROFILE


def build_manager_status(manager) -> Dict[str, Any]:
    """Trả về thông tin trạng thái chi tiết của instance."""
    uptime = time.time() - manager.start_time
    hours, remainder = divmod(int(uptime), 3600)
    minutes, seconds = divmod(remainder, 60)

    now = time.time()
    accounts_info = []
    for i, acc in enumerate(manager.chain or []):
        entry = manager.account_state.get(acc["key"])
        is_resting = False
        resting_until = None
        reason = None
        if entry:
            until = float(entry.get("until", 0))
            if now < until:
                is_resting = True
                try:
                    resting_until = datetime.fromtimestamp(until).astimezone().isoformat()
                except Exception:
                    resting_until = str(until)
                reason = entry.get("reason")

        st = "active" if i == manager.active_index else ("resting" if is_resting else "ready")
        accounts_info.append(
            {
                "label": acc["label"],
                "key": acc["key"],
                "status": st,
                "resting_until": resting_until,
                "reason": reason,
            }
        )

    active_label = (
        "Guest"
        if (getattr(manager, "is_guest", False) and not manager.chain)
        else (
            manager.chain[manager.active_index]["label"]
            if (manager.chain and 0 <= manager.active_index < len(manager.chain))
            else "unknown"
        )
    )

    is_on = manager.state == "on"
    auth_ok = manager.auth_ok if is_on else None
    account_status = (
        ("GUEST" if getattr(manager, "is_guest", False) else manager.account_status_name)
        if is_on
        else None
    )
    cookie_source = manager.cookie_source if is_on else None

    enabled_since_iso = None
    if manager.enabled_since:
        try:
            enabled_since_iso = (
                datetime.fromtimestamp(manager.enabled_since).astimezone().isoformat()
            )
        except Exception:
            enabled_since_iso = str(manager.enabled_since)

    status_str = "off" if manager.state == "off" else ("ok" if manager.auth_ok else "degraded")

    env_path = INSTANCE_PATHS.get("env_file") if isinstance(INSTANCE_PATHS, dict) else None
    env_file_name = (
        env_path.name if env_path else (".env." + INSTANCE_ID if INSTANCE_ID else ".env")
    )
    has_env_id = bool(
        manager._active_psid or (manager.chain and manager.chain[0].get("psid"))
    )
    if not has_env_id and env_path and env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if (
                    line.startswith("SECURE_1PSID=")
                    and len(line.split("=", 1)[1].strip()) >= 10
                ):
                    has_env_id = True
                    break
        except Exception:
            pass

    if not has_env_id:
        id_status = "missing"
    elif is_on and not manager.auth_ok:
        id_status = "expired"
    elif is_on and manager.auth_ok:
        id_status = "valid"
    else:
        id_status = "ready"

    last_act = None
    if manager.last_request_ts > 0:
        try:
            last_act = (
                datetime.fromtimestamp(manager.last_request_ts).astimezone().isoformat()
            )
        except Exception:
            last_act = str(manager.last_request_ts)

    return {
        "status": status_str,
        "state": manager.state,
        "enabled": is_on,
        "enabled_since": enabled_since_iso,
        "auth_ok": auth_ok,
        "is_guest": getattr(manager, "is_guest", False),
        "account_status": account_status,
        "cookie_source": cookie_source,
        "instance_id": INSTANCE_ID or "default",
        "instance_label": INSTANCE_LABEL,
        "port": PORT,
        "account_key": manager.account_key,
        "accounts": accounts_info,
        "active_account": active_label,
        "chain_overlap": getattr(manager, "chain_overlap", []),
        "model": manager.default_model,
        "available_models": manager.available_models,
        "model_details": manager.model_details,
        "session_initialized": manager.session_initialized,
        "turn_count": manager.turn_count,
        "total_requests": manager.total_requests,
        "total_errors": manager.total_errors,
        "error_rate": f"{(manager.total_errors / max(manager.total_requests, 1)) * 100:.1f}%",
        "uptime": f"{hours}h {minutes}m {seconds}s",
        "uptime_seconds": int(uptime),
        "using_backup_cookie": manager._using_backup,
        "vision_upload_available": manager.vision_upload_available,
        "cache_entries": 0,
        "cache_enabled": False,
        "inflight": manager._inflight,
        "chat_key": manager.current_chat_key(),
        "last_activity_ts": last_act,
        "has_env_id": has_env_id,
        "env_file": env_file_name,
        "id_status": id_status,
        "tool_profile": getattr(manager, "tool_profile", TOOL_PROFILE),
        "lane_resting_until": (
            datetime.fromtimestamp(manager.rest_until).astimezone().isoformat()
            if getattr(manager, "is_resting", None) and manager.is_resting()
            else None
        ),
    }
