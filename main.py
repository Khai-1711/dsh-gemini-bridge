import asyncio
import hmac
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Tuple

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from gemini_webapi.exceptions import AuthError

from datetime import datetime

from app.accounts import save_state
from app.activity import ActivityJournal, summarize_tool_calls
from app.config import (
    ALLOW_INSECURE_LAN,
    ALLOWED_HOSTS,
    API_KEY,
    API_START_ON,
    BASE_DIR,
    BRIDGE_LOG_FILE,
    DISABLE_DRAIN_TIMEOUT,
    ENABLE_RESPONSE_CACHE,
    FORBIDDEN_TOOLS,
    HOST,
    INSTANCE_ID,
    INSTANCE_LABEL,
    INSTANCE_PATHS,
    LANE_ADMIN_TOKEN,
    LOG_DIR,
    PEER_LANES,
    PORT,
    SSE_HEARTBEAT_INTERVAL,
    logger,
)
from app.control import is_origin_allowed
from app.env_update import (
    is_valid_cookie_value,
    is_valid_label,
    update_env_values,
)
from app.errors import BridgeError
from app.gemini_session import clear_response_cache, session_manager
from app.lane_security import (
    check_bind_safety,
    is_host_header_allowed,
    is_loopback_url,
    lane_token_ok,
)
from app.lanes import LaneRegistry, build_local_lane_status
from app.prompt_builder import build_incremental_prompt_and_files, compile_tools_prompt
from app.streaming import ToolCallAwareStreamer
from app.tool_parser import parse_tool_calls_from_reply

FRONTEND_DIR = BASE_DIR / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    ok, msg = check_bind_safety(HOST, API_KEY, ALLOW_INSECURE_LAN)
    if not ok:
        print("[LỖI CẤU HÌNH] " + msg)
        logger.critical(msg)
        sys.exit(1)
    if msg:
        logger.critical(msg)

    if API_START_ON:
        try:
            await session_manager.enable()
        except Exception as exc:
            logger.error(f"[CONTROL] Tự bật lúc khởi động thất bại: {exc}")
    else:
        logger.info("[CONTROL] Bridge khởi động ở trạng thái TẮT. Hãy bật ở dashboard.")
    yield
    await session_manager.close()


app = FastAPI(
    title="DSH Gemini Agent Bridge (Enterprise Edition)",
    version="3.0.0",
    lifespan=lifespan,
)

activity = ActivityJournal(INSTANCE_PATHS["activity_file"], INSTANCE_ID)
session_manager.on_event = lambda **kw: activity.write(**kw)

# Gắn thư mục frontend tĩnh
if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


# =====================================================================
# HOST VALIDATION & AUTHENTICATION MIDDLEWARE
# =====================================================================
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # 1. Kiểm tra Host header (chống DNS rebinding)
    if not is_host_header_allowed(request.headers.get("host", ""), ALLOWED_HOSTS):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Host không được phép", "code": "host_not_allowed"}},
        )

    # 2. Xác thực API Key
    path = request.url.path
    method = request.method.upper()
    is_whitelisted = (
        (method == "GET" and path in ("/", "/health", "/dashboard", "/favicon.ico"))
        or (method == "GET" and path.startswith("/frontend"))
    )

    if API_KEY and not is_whitelisted:
        auth_header = request.headers.get("Authorization", "").strip()
        token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else auth_header
        key_ok = bool(token) and hmac.compare_digest(token.encode("utf-8"), API_KEY.encode("utf-8"))

        is_lane_route = path.startswith("/v1/lanes")
        lane_header = request.headers.get("X-Lane-Token", "").strip()
        lane_ok = is_lane_route and bool(lane_header) and lane_token_ok(lane_header, LANE_ADMIN_TOKEN)

        if not (key_ok or lane_ok):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized: Invalid or missing API Key",
                    "message": "Unauthorized: Invalid or missing API Key",
                    "code": "unauthorized",
                },
            )

    return await call_next(request)


# =====================================================================
# SYSTEM & HEALTH ENDPOINTS
# =====================================================================
@app.get("/")
@app.get("/health")
async def health_check():
    """Thông tin sức khỏe tối thiểu công khai (không lộ tài khoản/model/hash)."""
    return {
        "status": "ok",
        "control": session_manager.state,
        "instance_id": INSTANCE_ID or "default",
        "instance_label": INSTANCE_LABEL,
    }


@app.get("/v1/status")
async def full_status():
    """Thông tin trạng thái chi tiết của instance (bắt buộc API_KEY nếu đã cấu hình)."""
    return session_manager.get_status()


@app.get("/dashboard")
async def dashboard_ui():
    """Phục vụ giao diện Dashboard từ thư mục frontend/."""
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "Không tìm thấy frontend/index.html"},
        )
    return FileResponse(index_file)


@app.post("/v1/reset")
async def reset_chat_session():
    """Chủ động mở một đoạn chat mới tinh trên Gemini Web."""
    try:
        session_manager.require_on()
    except BridgeError as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"error": {"message": e.message, "type": e.err_type, "code": e.code}},
        )
    await session_manager.reset_session()
    return {"status": "ok", "message": "Chat session reset successfully"}


def _save_env_cookies(slot: int, psid: str, psidts: str, label: str = "") -> Tuple[bool, Optional[str]]:
    """Ghi bền cookie vào .env.<ID> an toàn bằng file tạm + os.replace."""
    env_file = Path(INSTANCE_PATHS["env_file"])
    if slot == 0:
        updates = {"SECURE_1PSID": psid, "SECURE_1PSIDTS": psidts}
    else:
        updates = {f"BACKUP_{slot}PSID": psid, f"BACKUP_{slot}PSIDTS": psidts}
        if label:
            updates[f"BACKUP_{slot}_LABEL"] = label

    try:
        content = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        new_content = update_env_values(content, updates)
        tmp_file = env_file.parent / f".tmp_{env_file.name}_{uuid.uuid4().hex}"
        tmp_file.write_text(new_content, encoding="utf-8")
        os.replace(tmp_file, env_file)
        return True, None
    except Exception as exc:
        logger.error(f"[ACCOUNTS] Không ghi được file .env: {exc}")
        return False, f"Không ghi được file .env: {exc}. Cookie mới chỉ có hiệu lực cho tới khi restart server."


async def _local_update_cookies(slot: int, psid: str, psidts: str, label: str = "") -> Tuple[int, dict]:
    if not is_valid_cookie_value(psid) or not is_valid_cookie_value(psidts):
        return 400, {
            "error": {
                "code": "invalid_cookie_format",
                "message": "Giá trị cookie chứa ký tự không hợp lệ hoặc để trống.",
            }
        }

    if label and not is_valid_label(label):
        return 400, {
            "error": {
                "code": "invalid_label",
                "message": "Nhãn tài khoản chứa ký tự không hợp lệ hoặc quá dài.",
            }
        }

    try:
        res = await session_manager.update_slot_cookie(slot, psid, psidts, label)
    except BridgeError as e:
        return e.status_code, {"error": {"message": e.message, "type": e.err_type, "code": e.code}}
    except AuthError as e:
        return 400, {"error": {"code": "auth_failed", "message": f"Cookie không hợp lệ: {e}"}}
    except ValueError as e:
        return 400, {"error": {"code": "invalid_cookie_request", "message": str(e)}}
    except Exception as e:
        return 400, {"error": {"code": "update_cookie_error", "message": f"Lỗi cập nhật cookie: {e}"}}

    # Lưu bền vào .env
    persisted, warning_msg = _save_env_cookies(slot, psid, psidts, label)

    payload = {
        "ok": True,
        "persisted": persisted,
        "auth_ok": res.get("auth_ok", False),
        "account_key": res.get("account_key", ""),
    }
    if warning_msg:
        payload["warning"] = warning_msg
    return 200, payload


@app.post("/v1/accounts/{slot}/cookies")
async def update_account_cookies(slot: int, request: Request):
    if not is_origin_allowed(request.headers.get("origin"), PORT):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Origin không được phép.",
                    "type": "forbidden",
                    "code": "origin_not_allowed",
                }
            },
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    psid = str(body.get("psid") or body.get("SECURE_1PSID") or "").strip()
    psidts = str(body.get("psidts") or body.get("SECURE_1PSIDTS") or "").strip()
    label = str(body.get("label") or body.get("LABEL") or "").strip()

    status_code, data = await _local_update_cookies(slot, psid, psidts, label)
    return JSONResponse(status_code=status_code, content=data)


@app.post("/v1/update-cookies")
async def update_cookies(request: Request):
    """Hot-reload cookie trực tiếp từ API (tương thích ngược: nạp vào slot 0)."""
    return await update_account_cookies(0, request)


async def _local_switch_model(model_name: str, keep_chat: bool = True) -> Tuple[int, dict]:
    if session_manager.state != "on" or not session_manager.auth_ok:
        return 409, {
            "error": {
                "code": "lane_not_ready",
                "message": "Luồng chưa BẬT hoặc chưa đăng nhập.",
            }
        }

    if not model_name:
        return 400, {
            "error": {
                "code": "invalid_request",
                "message": "Tên model không được để trống.",
            }
        }

    target = model_name
    is_valid = False
    if target in ("unspecified", ""):
        is_valid = True
        target = "unspecified"
    elif session_manager.available_models and target in session_manager.available_models:
        is_valid = True
    else:
        try:
            resolved = session_manager.client.resolve_model(target)
            resolved_name = str(getattr(resolved, "model_name", None) or target)
            if resolved_name in session_manager.available_models:
                target = resolved_name
                is_valid = True
        except Exception:
            is_valid = False

    if not is_valid:
        return 400, {
            "error": {
                "code": "model_not_available",
                "message": f"Model '{model_name}' không có trong danh sách khả dụng: {session_manager.available_models}",
            }
        }

    try:
        new_model = await session_manager.switch_model(target, keep_chat=keep_chat)
    except Exception as e:
        return 500, {"error": {"code": "switch_model_failed", "message": str(e)}}

    return 200, {
        "status": "ok",
        "active_model": new_model,
        "available_models": session_manager.available_models,
    }


def _local_lane_models() -> dict:
    details = {d["name"]: d for d in (session_manager.model_details or [])}
    available = []
    for m in session_manager.available_models:
        if m == "unspecified":
            label = "Mặc định (unspecified)"
        else:
            meta = details.get(m, {})
            label = meta.get("display_name", m) or m
        available.append({"id": m, "label": label})
    return {
        "current": session_manager.default_model,
        "available": available,
    }


@app.post("/v1/switch-model")
async def switch_model(request: Request):
    """Chuyển đổi model Gemini đang sử dụng."""
    try:
        session_manager.require_on()
    except BridgeError as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"error": {"message": e.message, "type": e.err_type, "code": e.code}},
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    model_name = body.get("model", "").strip()
    keep_chat = body.get("keep_chat", True)
    code, data = await _local_switch_model(model_name, keep_chat=keep_chat)
    return JSONResponse(status_code=code, content=data)


@app.post("/v1/cache/clear")
async def clear_cache():
    """Xóa toàn bộ bộ nhớ đệm cache."""
    cleared = clear_response_cache()
    return {"status": "ok", "cleared_entries": cleared}


@app.get("/v1/logs")
async def get_recent_logs(lines: int = 50):
    """Lấy danh sách các dòng log gần nhất để hiển thị lên Dashboard."""
    log_file = BRIDGE_LOG_FILE
    if not log_file.exists():
        return {"logs": [f"Chưa có tệp nhật ký {BRIDGE_LOG_FILE.name}"]}
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return {"logs": [line.strip() for line in all_lines[-lines:]]}
    except Exception as e:
        return {"logs": [f"Lỗi đọc nhật ký: {e}"]}


@app.get("/v1/activity")
async def get_activity(limit: int = 50):
    """Nhật ký công việc của CHÍNH instance này (không gồm instance khác)."""
    limit = max(1, min(limit, 500))
    return {"instance": INSTANCE_ID or "default", "entries": activity.tail(limit)}


@app.post("/v1/accounts/clear-exhausted")
async def clear_exhausted():
    """Xóa trạng thái 'nghỉ' của mọi tài khoản (dùng khi chủ dự án biết quota đã làm mới)."""
    session_manager.account_state.clear()
    save_state(INSTANCE_PATHS["accounts_state_file"], session_manager.account_state)
    return {"status": "ok"}


lane_registry = LaneRegistry(
    self_id=INSTANCE_ID or "default",
    peers=PEER_LANES,
    token=LANE_ADMIN_TOKEN,
)


def _control_payload() -> dict:
    s = session_manager
    enabled_since_iso = None
    if s.enabled_since:
        try:
            enabled_since_iso = datetime.fromtimestamp(s.enabled_since).astimezone().isoformat()
        except Exception:
            enabled_since_iso = str(s.enabled_since)

    return {
        "instance_id": INSTANCE_ID or "default",
        "instance_label": INSTANCE_LABEL,
        "port": PORT,
        "state": s.state,
        "enabled": s.state == "on",
        "enabled_since": enabled_since_iso,
        "drain_timeout": DISABLE_DRAIN_TIMEOUT,
    }


async def _local_control(action: str):
    if action == "enable":
        was_on = (session_manager.state == "on")
        try:
            await session_manager.enable()
        except BridgeError as e:
            return e.status_code, {"error": {"message": e.message, "type": e.err_type, "code": e.code}}
        res = _control_payload()
        res["changed"] = not was_on
        return 200, res
    elif action == "disable":
        was_off = (session_manager.state == "off")
        try:
            await session_manager.disable()
        except BridgeError as e:
            return e.status_code, {"error": {"message": e.message, "type": e.err_type, "code": e.code}}
        res = _control_payload()
        res["changed"] = not was_off
        return 200, res
    elif action == "reauth":
        if session_manager.state != "on":
            return 409, {"error": {"code": "lane_not_ready", "message": "Bridge chưa BẬT"}}
        ok = await session_manager.ensure_authenticated()
        return 200, {"status": "ok" if ok else "failed", "auth_ok": ok}
    return 400, {"error": {"code": "invalid_action", "message": f"Hành động '{action}' không hợp lệ."}}


@app.get("/v1/control")
async def control_status():
    return _control_payload()


@app.post("/v1/control/enable")
async def control_enable(request: Request):
    if not is_origin_allowed(request.headers.get("origin"), PORT):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Origin không được phép.",
                    "type": "forbidden",
                    "code": "origin_not_allowed",
                }
            },
        )
    status_code, res = await _local_control("enable")
    if status_code != 200:
        return JSONResponse(status_code=status_code, content=res)
    return res


@app.post("/v1/control/disable")
async def control_disable(request: Request):
    if not is_origin_allowed(request.headers.get("origin"), PORT):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Origin không được phép.",
                    "type": "forbidden",
                    "code": "origin_not_allowed",
                }
            },
        )
    status_code, res = await _local_control("disable")
    if status_code != 200:
        return JSONResponse(status_code=status_code, content=res)
    return res


@app.post("/v1/reauth")
async def local_reauth(request: Request):
    if not is_origin_allowed(request.headers.get("origin"), PORT):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Origin không được phép.",
                    "type": "forbidden",
                    "code": "origin_not_allowed",
                }
            },
        )
    status_code, res = await _local_control("reauth")
    if status_code != 200:
        return JSONResponse(status_code=status_code, content=res)
    return res


@app.get("/v1/lanes")
async def list_lanes():
    local_st = build_local_lane_status(
        session_manager,
        port=PORT,
        instance_id=INSTANCE_ID or "default",
        instance_label=INSTANCE_LABEL or (INSTANCE_ID or "default"),
        cache_enabled=ENABLE_RESPONSE_CACHE,
    )
    lanes = await lane_registry.list_lanes(local_st)
    return {
        "self": INSTANCE_ID or "default",
        "lanes": lanes,
    }


@app.get("/v1/lanes/{lane_id}/status")
async def get_lane_status(lane_id: str):
    """Trạng thái chi tiết của luồng (dùng cho các luồng gọi sang nhau bằng X-Lane-Token)."""
    return session_manager.get_status()


@app.post("/v1/lanes/{lane_id}/enable")
async def lane_enable(lane_id: str, request: Request):
    if not is_origin_allowed(request.headers.get("origin"), PORT):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Origin không được phép.",
                    "type": "forbidden",
                    "code": "origin_not_allowed",
                }
            },
        )
    status_code, data = await lane_registry.control(lane_id, "enable", _local_control)
    activity.write(
        event="lane_control",
        lane_id=lane_id,
        action="enable",
        status=status_code,
        outcome="ok" if status_code == 200 else f"error:{status_code}",
    )
    return JSONResponse(status_code=status_code, content=data)


@app.post("/v1/lanes/{lane_id}/disable")
async def lane_disable(lane_id: str, request: Request):
    if not is_origin_allowed(request.headers.get("origin"), PORT):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Origin không được phép.",
                    "type": "forbidden",
                    "code": "origin_not_allowed",
                }
            },
        )
    status_code, data = await lane_registry.control(lane_id, "disable", _local_control)
    activity.write(
        event="lane_control",
        lane_id=lane_id,
        action="disable",
        status=status_code,
        outcome="ok" if status_code == 200 else f"error:{status_code}",
    )
    return JSONResponse(status_code=status_code, content=data)


@app.post("/v1/lanes/{lane_id}/reauth")
async def lane_reauth(lane_id: str, request: Request):
    if not is_origin_allowed(request.headers.get("origin"), PORT):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Origin không được phép.",
                    "type": "forbidden",
                    "code": "origin_not_allowed",
                }
            },
        )
    status_code, data = await lane_registry.control(lane_id, "reauth", _local_control)
    activity.write(
        event="lane_control",
        lane_id=lane_id,
        action="reauth",
        status=status_code,
        outcome="ok" if status_code == 200 else f"error:{status_code}",
    )
    return JSONResponse(status_code=status_code, content=data)


@app.get("/v1/lanes/{lane_id}/logs")
async def lane_logs(lane_id: str, limit: int = 50):
    limit = max(1, min(limit, 500))
    if lane_id in (INSTANCE_ID or "default", "self"):
        return await get_recent_logs(lines=limit)

    status_code, data = await lane_registry.proxy_request(
        lane_id, "GET", "logs", query=f"limit={limit}", timeout=5.0
    )
    return JSONResponse(status_code=status_code, content=data)


@app.get("/v1/lanes/{lane_id}/activity")
async def lane_activity(lane_id: str, limit: int = 50):
    limit = max(1, min(limit, 500))
    if lane_id in (INSTANCE_ID or "default", "self"):
        return await get_activity(limit=limit)

    status_code, data = await lane_registry.proxy_request(
        lane_id, "GET", "activity", query=f"limit={limit}", timeout=5.0
    )
    return JSONResponse(status_code=status_code, content=data)


@app.get("/v1/lanes/{lane_id}/models")
async def lane_models(lane_id: str):
    if lane_id in (INSTANCE_ID or "default", "self"):
        return _local_lane_models()

    status_code, data = await lane_registry.proxy_request(
        lane_id, "GET", "models", timeout=5.0
    )
    return JSONResponse(status_code=status_code, content=data)


@app.post("/v1/lanes/{lane_id}/switch-model")
async def lane_switch_model(lane_id: str, request: Request):
    if not is_origin_allowed(request.headers.get("origin"), PORT):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Origin không được phép.",
                    "type": "forbidden",
                    "code": "origin_not_allowed",
                }
            },
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    model_name = str(body.get("model", "")).strip()
    keep_chat = bool(body.get("keep_chat", True))

    if lane_id in (INSTANCE_ID or "default", "self"):
        code, data = await _local_switch_model(model_name, keep_chat=keep_chat)
        return JSONResponse(status_code=code, content=data)

    if not LANE_ADMIN_TOKEN:
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "lane_not_controllable",
                    "message": "Thiếu LANE_ADMIN_TOKEN để điều khiển luồng khác.",
                }
            },
        )

    status_code, data = await lane_registry.proxy_request(
        lane_id,
        "POST",
        "switch-model",
        body=json.dumps({"model": model_name, "keep_chat": keep_chat}).encode("utf-8"),
        timeout=10.0,
    )
    return JSONResponse(status_code=status_code, content=data)


@app.post("/v1/lanes/{lane_id}/reset")
async def lane_reset(lane_id: str, request: Request):
    if not is_origin_allowed(request.headers.get("origin"), PORT):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Origin không được phép.",
                    "type": "forbidden",
                    "code": "origin_not_allowed",
                }
            },
        )
    if lane_id in (INSTANCE_ID or "default", "self"):
        try:
            session_manager.require_on()
        except BridgeError as e:
            return JSONResponse(
                status_code=e.status_code,
                content={"error": {"message": e.message, "type": e.err_type, "code": e.code}},
            )
        await session_manager.reset_session()
        return {"status": "ok", "message": "Chat session reset successfully"}

    if not LANE_ADMIN_TOKEN:
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "lane_not_controllable",
                    "message": "Thiếu LANE_ADMIN_TOKEN để điều khiển luồng khác.",
                }
            },
        )

    status_code, data = await lane_registry.proxy_request(
        lane_id, "POST", "reset", body=b"{}", timeout=10.0
    )
    return JSONResponse(status_code=status_code, content=data)


@app.post("/v1/lanes/{lane_id}/cache/clear")
async def lane_cache_clear(lane_id: str, request: Request):
    if not is_origin_allowed(request.headers.get("origin"), PORT):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Origin không được phép.",
                    "type": "forbidden",
                    "code": "origin_not_allowed",
                }
            },
        )
    if lane_id in (INSTANCE_ID or "default", "self"):
        cleared = clear_response_cache()
        return {"status": "ok", "cleared_entries": cleared}

    if not LANE_ADMIN_TOKEN:
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "lane_not_controllable",
                    "message": "Thiếu LANE_ADMIN_TOKEN để điều khiển luồng khác.",
                }
            },
        )

    status_code, data = await lane_registry.proxy_request(
        lane_id, "POST", "cache/clear", body=b"{}", timeout=10.0
    )
    return JSONResponse(status_code=status_code, content=data)


@app.post("/v1/lanes/{lane_id}/accounts/{slot}/cookies")
async def lane_account_cookies(lane_id: str, slot: int, request: Request):
    if not is_origin_allowed(request.headers.get("origin"), PORT):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Origin không được phép.",
                    "type": "forbidden",
                    "code": "origin_not_allowed",
                }
            },
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    psid = str(body.get("psid") or body.get("SECURE_1PSID") or "").strip()
    psidts = str(body.get("psidts") or body.get("SECURE_1PSIDTS") or "").strip()
    label = str(body.get("label") or body.get("LABEL") or "").strip()

    # Nếu đích là chính mình -> gọi local handler
    if lane_id in (INSTANCE_ID or "default", "self"):
        status_code, data = await _local_update_cookies(slot, psid, psidts, label)
        return JSONResponse(status_code=status_code, content=data)

    # Nếu đích là luồng khác -> chỉ cho phép khi URL của luồng đó là loopback (is_loopback_url)
    if lane_id not in lane_registry.peers:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "lane_not_found", "message": f"Lane '{lane_id}' không tồn tại."}},
        )

    peer_url = lane_registry.peers[lane_id]
    if not is_loopback_url(peer_url):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "cookie_proxy_forbidden",
                    "message": "Hãy mở dashboard của chính luồng đó để nạp cookie (không gửi cookie qua mạng).",
                }
            },
        )

    if not LANE_ADMIN_TOKEN:
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "lane_not_controllable",
                    "message": "Thiếu LANE_ADMIN_TOKEN để điều khiển luồng khác.",
                }
            },
        )

    req_body = json.dumps({"psid": psid, "psidts": psidts, "label": label}).encode("utf-8")
    status_code, data = await lane_registry.proxy_request(
        lane_id,
        "POST",
        f"accounts/{slot}/cookies",
        body=req_body,
        timeout=30.0,
    )
    return JSONResponse(status_code=status_code, content=data)


@app.get("/v1/models")
async def list_models():
    """Trả về danh sách model thực tế được phát hiện trên tài khoản."""
    try:
        session_manager.require_on()
    except BridgeError as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"error": {"message": e.message, "type": e.err_type, "code": e.code}},
        )
    details = {
        d["name"]: d for d in (session_manager.model_details or [])
    }
    models_data = []
    for m in session_manager.available_models:
        meta = details.get(m, {})
        models_data.append(
            {
                "id": m,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google",
                "display_name": meta.get("display_name", m),
                "description": meta.get("description", ""),
            }
        )
    return {"object": "list", "data": models_data}


# =====================================================================
# OPENAI CHAT COMPLETIONS API (Core Agent Pipeline)
# =====================================================================
@app.post("/v1/chat/completions")
@app.post("/v1/responses")
async def chat_completions(request: Request):
    try:
        session_manager.require_on()
    except BridgeError as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"error": {"message": e.message, "type": e.err_type, "code": e.code}},
        )
    t0 = time.time()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON payload"})

    messages = body.get("messages") or body.get("input") or []
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]

    tools = body.get("tools") or []
    tools_map = {
        t.get("function", t).get("name"): t.get("function", t)
        for t in tools
        if t.get("function", t).get("name")
        and t.get("function", t).get("name").lower() not in FORBIDDEN_TOOLS
    }

    last = messages[-1] if messages else {}
    task_hint = ""
    if last.get("role") == "user":
        c = last.get("content")
        task_hint = (c if isinstance(c, str) else str(c))[:100]

    model_id = body.get("model", session_manager.default_model)
    is_stream = body.get("stream", False)
    chat_id = f"chatcmpl-{uuid.uuid4().hex}"
    created_ts = int(t0)

    if not await session_manager.ensure_authenticated():
        activity.write(
            request_id=chat_id,
            model=model_id,
            stream=is_stream,
            chat_key=session_manager.current_chat_key(),
            auth_ok=False,
            task_hint=task_hint,
            latency_ms=int((time.time() - t0) * 1000),
            outcome="error:gemini_not_authenticated",
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": (
                        "Gemini chưa đăng nhập (guest mode hoặc cookie hết hạn). "
                        "Hãy cập nhật cookie ở /dashboard hoặc POST /v1/update-cookies."
                    ),
                    "type": "auth_error",
                    "code": "gemini_not_authenticated",
                }
            },
        )

    # Ghi nhận hội thoại và sổ sự thật (Ledger)
    session_manager.remember_transcript(messages)
    if messages and messages[-1].get("role") == "user":
        session_manager.ledger.note_user_goal(messages[-1].get("content"))
    for m in reversed(messages):
        if m.get("role") == "assistant":
            break
        if m.get("role") == "tool":
            session_manager.ledger.note_tool_result(
                m.get("tool_call_id"), m.get("content")
            )

    # Rút trích prompt gia tăng
    prompt_to_send, image_files = build_incremental_prompt_and_files(
        messages, tools, is_first_turn=session_manager.needs_tool_header
    )
    session_manager.remember_tool_header(compile_tools_prompt(tools))
    del body, messages, tools

    # Tính toán ước tính số token (OpenAI Usage Stats)
    prompt_tokens = max(1, len(prompt_to_send) // 4)

    def _content_chunk(piece: str) -> dict:
        return {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": model_id,
            "choices": [
                {"index": 0, "delta": {"content": piece}, "finish_reason": None}
            ],
        }

    # ─────────────────────────────────────────────────────────────────
    # LUỒNG STREAMING (SSE + Keep-Alive Heartbeat + real text deltas)
    # ─────────────────────────────────────────────────────────────────
    if is_stream:
        async def sse_generator():
            first_chunk = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

            queue: asyncio.Queue = asyncio.Queue()
            producer_done = asyncio.Event()

            async def producer():
                try:
                    async for item in session_manager.send_prepared_turn_stream(
                        prompt_to_send, image_files
                    ):
                        await queue.put(item)
                except Exception as exc:  # defensive
                    logger.error(f"[SSE] producer error: {exc}")
                finally:
                    producer_done.set()

            prod_task = asyncio.create_task(producer())
            splitter = ToolCallAwareStreamer()
            done_text = None

            try:
                while True:
                    if producer_done.is_set() and queue.empty():
                        break
                    try:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=SSE_HEARTBEAT_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        if producer_done.is_set() and queue.empty():
                            break
                        yield ": keep-alive\n\n"
                        continue

                    kind, payload = item
                    if kind == "error":
                        err_code = (
                            payload.get("code", "error")
                            if isinstance(payload, dict)
                            else "error"
                        )
                        activity.write(
                            request_id=chat_id,
                            model=model_id,
                            stream=True,
                            chat_key=session_manager.current_chat_key(),
                            auth_ok=session_manager.auth_ok,
                            task_hint=task_hint,
                            latency_ms=int((time.time() - t0) * 1000),
                            outcome=f"error:{err_code}",
                        )
                        yield f"data: {json.dumps({'error': payload}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    elif kind == "delta":
                        for piece in splitter.feed(payload):
                            yield f"data: {json.dumps(_content_chunk(piece), ensure_ascii=False)}\n\n"
                    elif kind == "text":
                        yield f"data: {json.dumps(_content_chunk(payload), ensure_ascii=False)}\n\n"
                    elif kind == "done":
                        done_text = payload

                for piece in splitter.flush():
                    yield f"data: {json.dumps(_content_chunk(piece), ensure_ascii=False)}\n\n"

                reply_text = done_text if done_text is not None else splitter.full_text
                tool_calls = parse_tool_calls_from_reply(reply_text, tools_map)
                session_manager.ledger.note_tool_calls(tool_calls)
                session_manager.ledger.note_assistant(reply_text)

                if tool_calls:
                    tc_deltas = [
                        {
                            "index": idx,
                            "id": tc["id"],
                            "type": "function",
                            "function": tc["function"],
                        }
                        for idx, tc in enumerate(tool_calls)
                    ]
                    tool_chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model_id,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"tool_calls": tc_deltas},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(tool_chunk, ensure_ascii=False)}\n\n"
                    finish_reason = "tool_calls"
                else:
                    finish_reason = "stop"

                completion_tokens = max(1, len(reply_text) // 4)
                end_chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }

                activity.write(
                    request_id=chat_id,
                    model=model_id,
                    stream=True,
                    chat_key=session_manager.current_chat_key(),
                    auth_ok=session_manager.auth_ok,
                    task_hint=task_hint,
                    prompt_chars=len(prompt_to_send),
                    reply_chars=len(reply_text),
                    finish=finish_reason,
                    tools=summarize_tool_calls(tool_calls),
                    latency_ms=int((time.time() - t0) * 1000),
                    outcome="ok",
                )

                yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except asyncio.CancelledError:
                prod_task.cancel()
                try:
                    await prod_task
                except (asyncio.CancelledError, Exception):
                    pass
                raise

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ─────────────────────────────────────────────────────────────────
    # LUỒNG NON-STREAMING
    # ─────────────────────────────────────────────────────────────────
    try:
        reply_text = await session_manager.send_prepared_turn(prompt_to_send, image_files)
    except BridgeError as e:
        activity.write(
            request_id=chat_id,
            model=model_id,
            stream=False,
            chat_key=session_manager.current_chat_key(),
            auth_ok=session_manager.auth_ok,
            task_hint=task_hint,
            latency_ms=int((time.time() - t0) * 1000),
            outcome=f"error:{e.code}",
        )
        return JSONResponse(
            status_code=e.status_code,
            content={"error": {"message": e.message, "type": e.err_type, "code": e.code}},
        )
    tool_calls = parse_tool_calls_from_reply(reply_text, tools_map)
    session_manager.ledger.note_tool_calls(tool_calls)
    session_manager.ledger.note_assistant(reply_text)

    if tool_calls:
        message_obj = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        finish_reason = "tool_calls"
    else:
        message_obj = {"role": "assistant", "content": reply_text}
        finish_reason = "stop"

    activity.write(
        request_id=chat_id,
        model=model_id,
        stream=False,
        chat_key=session_manager.current_chat_key(),
        auth_ok=session_manager.auth_ok,
        task_hint=task_hint,
        prompt_chars=len(prompt_to_send),
        reply_chars=len(reply_text),
        finish=finish_reason,
        tools=summarize_tool_calls(tool_calls),
        latency_ms=int((time.time() - t0) * 1000),
        outcome="ok",
    )

    completion_tokens = max(1, len(reply_text) // 4)
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created_ts,
        "model": model_id,
        "choices": [
            {"index": 0, "message": message_obj, "finish_reason": finish_reason}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


if __name__ == "__main__":
    import uvicorn

    ok, msg = check_bind_safety(HOST, API_KEY, ALLOW_INSECURE_LAN)
    if not ok:
        print("[LỖI CẤU HÌNH] " + msg)
        sys.exit(1)
    if msg:
        logger.critical(msg)

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
        limit_concurrency=200,
        timeout_keep_alive=15,
    )
