"""Các route quản trị, Dashboard UI (1A), nạp cookie nóng, đổi model và điều phối đa lane."""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Any, Optional, Tuple
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from gemini_webapi.exceptions import AuthError

from app.accounts import save_state
from app.config import (
    BRIDGE_LOG_FILE,
    DISABLE_DRAIN_TIMEOUT,
    INSTANCE_ID,
    INSTANCE_LABEL,
    PORT,
    logger,
)
from app.control import is_origin_allowed
from app.env_update import (
    is_valid_cookie_value,
    is_valid_label,
    update_env_values,
)
from app.errors import BridgeError
from app.gemini_session import clear_response_cache
from app.lane_security import is_loopback_url
from app.lanes import build_local_lane_status, spawn_local_lane

admin_router = APIRouter()


def _m():
    return sys.modules.get("main") or sys.modules["__main__"]


def _origin_error(request: Request) -> Optional[JSONResponse]:
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
    return None


def _missing_lane_token_error() -> Optional[JSONResponse]:
    if not _m().LANE_ADMIN_TOKEN:
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "lane_not_controllable",
                    "message": "Thiếu LANE_ADMIN_TOKEN để điều khiển luồng khác.",
                }
            },
        )
    return None


def _bridge_err_response(exc: BridgeError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.message, "type": exc.err_type, "code": exc.code}},
    )


@admin_router.get("/")
@admin_router.get("/health")
async def health_check():
    """Thông tin sức khỏe tối thiểu công khai (không lộ tài khoản/model/hash)."""
    return {
        "status": "ok",
        "control": _m().session_manager.state,
        "instance_id": INSTANCE_ID or "default",
        "instance_label": INSTANCE_LABEL,
    }


@admin_router.get("/v1/status")
async def full_status():
    return _m().session_manager.get_status()


@admin_router.get("/dashboard")
async def dashboard_ui():
    index_file = _m().FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse(status_code=404, content={"error": "Không tìm thấy frontend/index.html"})
    return FileResponse(index_file)


@admin_router.post("/v1/reset")
async def reset_chat_session():
    mgr = _m().session_manager
    try:
        mgr.require_on()
    except BridgeError as e:
        return _bridge_err_response(e)
    await mgr.reset_session()
    return {"status": "ok", "message": "Chat session reset successfully"}


def _save_env_cookies(slot: int, psid: str, psidts: str, label: str = "") -> Tuple[bool, Optional[str]]:
    env_file = Path(_m().INSTANCE_PATHS["env_file"])
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
        return 400, {"error": {"code": "invalid_cookie_format", "message": "Giá trị cookie chứa ký tự không hợp lệ hoặc để trống."}}
    if label and not is_valid_label(label):
        return 400, {"error": {"code": "invalid_label", "message": "Nhãn tài khoản chứa ký tự không hợp lệ hoặc quá dài."}}

    try:
        res = await _m().session_manager.update_slot_cookie(slot, psid, psidts, label)
    except BridgeError as e:
        return e.status_code, {"error": {"message": e.message, "type": e.err_type, "code": e.code}}
    except AuthError as e:
        return 400, {"error": {"code": "auth_failed", "message": f"Cookie không hợp lệ: {e}"}}
    except ValueError as e:
        return 400, {"error": {"code": "invalid_cookie_request", "message": str(e)}}
    except Exception as e:
        return 400, {"error": {"code": "update_cookie_error", "message": f"Lỗi cập nhật cookie: {e}"}}

    persisted, warning_msg = _save_env_cookies(slot, psid, psidts, label)
    payload = {"ok": True, "persisted": persisted, "auth_ok": res.get("auth_ok", False), "account_key": res.get("account_key", "")}
    if warning_msg:
        payload["warning"] = warning_msg
    return 200, payload


@admin_router.post("/v1/accounts/{slot}/cookies")
async def update_account_cookies(slot: int, request: Request):
    if err := _origin_error(request):
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    psid = str(body.get("psid") or body.get("SECURE_1PSID") or "").strip()
    psidts = str(body.get("psidts") or body.get("SECURE_1PSIDTS") or "").strip()
    label = str(body.get("label") or body.get("LABEL") or "").strip()
    status_code, data = await _m()._local_update_cookies(slot, psid, psidts, label)
    return JSONResponse(status_code=status_code, content=data)


@admin_router.post("/v1/update-cookies")
async def update_cookies(request: Request):
    return await update_account_cookies(0, request)


async def _local_switch_model(model_name: Any, keep_chat: bool = True) -> Tuple[int, dict]:
    mgr = _m().session_manager
    if not (mgr.state == "on" and (mgr.auth_ok or (mgr.allow_guest and mgr.is_guest))):
        return 409, {"error": {"code": "lane_not_ready", "message": "Luồng chưa BẬT hoặc chưa sẵn sàng."}}

    raw_str = str(model_name or "").strip()
    if not raw_str:
        return 400, {"error": {"code": "invalid_request", "message": "Tên model không được để trống."}}

    target = raw_str
    is_valid = False
    if target.lower() in ("unspecified", "", "default", "auto", "none", "gemini"):
        is_valid = True
        target = mgr.default_model or "unspecified"
    elif mgr.available_models and target in mgr.available_models:
        is_valid = True
    elif mgr.available_models and any(m.lower() == target.lower() for m in mgr.available_models):
        target = next(m for m in mgr.available_models if m.lower() == target.lower())
        is_valid = True
    else:
        t_low = target.lower()
        matched = None
        for detail in (getattr(mgr, "model_details", None) or []):
            d_name = str(detail.get("name", "")).strip()
            disp = str(detail.get("display_name", "")).strip().lower()
            desc = str(detail.get("description", "")).strip().lower()
            if d_name and t_low in (disp, desc):
                matched = d_name
                break
        if not matched:
            for keyword in ("neon", "hydrogen", "carbon", "flash", "pro"):
                if keyword in t_low:
                    for m in mgr.available_models:
                        if keyword in m.lower():
                            matched = m
                            break
                    if not matched:
                        for detail in (getattr(mgr, "model_details", None) or []):
                            d_name = str(detail.get("name", "")).strip()
                            meta_txt = f"{detail.get('display_name', '')} {detail.get('description', '')}".lower()
                            if d_name and keyword in meta_txt:
                                matched = d_name
                                break
                if matched:
                    break
        if matched:
            target = matched
            is_valid = True
        else:
            try:
                resolved = mgr.client.resolve_model(target)
                resolved_name = str(getattr(resolved, "model_name", None) or target)
                if resolved_name in mgr.available_models:
                    target = resolved_name
                    is_valid = True
            except Exception:
                is_valid = False

    if not is_valid:
        return 400, {"error": {"code": "model_not_available", "message": f"Model '{model_name}' không có trong danh sách khả dụng: {mgr.available_models}"}}

    try:
        new_model = await mgr.switch_model(target, keep_chat=keep_chat)
    except Exception as e:
        return 500, {"error": {"code": "switch_model_failed", "message": str(e)}}

    return 200, {"status": "ok", "active_model": new_model, "available_models": mgr.available_models}


def _local_lane_models() -> dict:
    mgr = _m().session_manager
    details = {d["name"]: d for d in (mgr.model_details or [])}
    available = []
    for m in mgr.available_models:
        if m == "unspecified":
            label = "Mặc định (unspecified)"
        else:
            meta = details.get(m, {})
            desc = meta.get("description", "").strip()
            disp = meta.get("display_name", "").strip()
            if desc and desc != m and desc.lower() not in m.lower():
                label = f"{m} ({desc})"
            elif disp and disp != m and not any(k in disp.lower() for k in ["trả lời", "trợ giúp", "nghiên cứu", "phức tạp"]):
                label = disp
            else:
                label = m
        available.append({"id": m, "label": label})
    return {"current": mgr.default_model, "available": available}


@admin_router.post("/v1/switch-model")
async def switch_model(request: Request):
    try:
        _m().session_manager.require_on()
    except BridgeError as e:
        return _bridge_err_response(e)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})
    code, data = await _m()._local_switch_model(body.get("model", "").strip(), keep_chat=body.get("keep_chat", True))
    return JSONResponse(status_code=code, content=data)


@admin_router.post("/v1/cache/clear")
async def clear_cache():
    return {"status": "ok", "cleared_entries": clear_response_cache()}


@admin_router.get("/v1/logs")
async def get_recent_logs(lines: int = 50):
    if not BRIDGE_LOG_FILE.exists():
        return {"logs": [f"Chưa có tệp nhật ký {BRIDGE_LOG_FILE.name}"]}
    try:
        with open(BRIDGE_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            return {"logs": [line.strip() for line in f.readlines()[-lines:]]}
    except Exception as e:
        return {"logs": [f"Lỗi đọc nhật ký: {e}"]}


@admin_router.get("/v1/activity")
async def get_activity(limit: int = 50):
    limit = max(1, min(limit, 500))
    return {"instance": INSTANCE_ID or "default", "entries": _m().activity.tail(limit)}


@admin_router.post("/v1/accounts/clear-exhausted")
async def clear_exhausted():
    mgr = _m().session_manager
    mgr.account_state.clear()
    save_state(_m().INSTANCE_PATHS["accounts_state_file"], mgr.account_state)
    return {"status": "ok"}


def _control_payload() -> dict:
    s = _m().session_manager
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
    mgr = _m().session_manager
    if action == "enable":
        was_on = mgr.state == "on"
        try:
            await mgr.enable()
        except BridgeError as e:
            return e.status_code, {"error": {"message": e.message, "type": e.err_type, "code": e.code}}
        res = _control_payload()
        res["changed"] = not was_on
        return 200, res
    if action == "disable":
        was_off = mgr.state == "off"
        try:
            await mgr.disable()
        except BridgeError as e:
            return e.status_code, {"error": {"message": e.message, "type": e.err_type, "code": e.code}}
        res = _control_payload()
        res["changed"] = not was_off
        return 200, res
    if action == "reauth":
        if mgr.state != "on":
            return 409, {"error": {"code": "lane_not_ready", "message": "Bridge chưa BẬT"}}
        ok = await mgr.ensure_authenticated()
        return 200, {"status": "ok" if ok else "failed", "auth_ok": ok}
    return 400, {"error": {"code": "invalid_action", "message": f"Hành động '{action}' không hợp lệ."}}


@admin_router.get("/v1/control")
async def control_status():
    return _control_payload()


@admin_router.post("/v1/control/enable")
async def control_enable(request: Request):
    if err := _origin_error(request):
        return err
    code, res = await _local_control("enable")
    return res if code == 200 else JSONResponse(status_code=code, content=res)


@admin_router.post("/v1/control/disable")
async def control_disable(request: Request):
    if err := _origin_error(request):
        return err
    code, res = await _local_control("disable")
    return res if code == 200 else JSONResponse(status_code=code, content=res)


async def _delayed_gateway_shutdown():
    await asyncio.sleep(0.5)
    logger.warning(f"[GATEWAY] Đang dừng tiến trình Port {PORT}...")
    os._exit(0)


@admin_router.post("/v1/gateway/shutdown")
@admin_router.post("/v1/gateway/stop")
async def gateway_shutdown(request: Request):
    if err := _origin_error(request):
        return err
    logger.warning(f"[GATEWAY] Nhận lệnh TẮT Cổng Frontend {PORT} từ Dashboard.")
    asyncio.create_task(_delayed_gateway_shutdown())
    return {"status": "ok", "message": f"Cổng {PORT} đang dừng tiến trình..."}


@admin_router.post("/v1/gateway/spawn")
async def gateway_spawn(request: Request):
    if err := _origin_error(request):
        return err
    if spawn_local_lane("frontend"):
        logger.info(f"[{INSTANCE_ID or 'worker'}] Đã spawn Cổng Frontend 10000 thành công.")
        return {"status": "ok", "message": "Đã gửi lệnh bật lại Cổng 10000."}
    return JSONResponse(status_code=500, content={"error": {"message": "Không thể spawn Cổng 10000.", "code": "spawn_failed"}})


@admin_router.post("/v1/reauth")
async def local_reauth(request: Request):
    if err := _origin_error(request):
        return err
    code, res = await _local_control("reauth")
    return res if code == 200 else JSONResponse(status_code=code, content=res)


@admin_router.get("/v1/lanes")
async def list_lanes(refresh: bool = False):
    reg = _m().lane_registry
    local_st = build_local_lane_status(
        _m().session_manager,
        port=PORT,
        instance_id=INSTANCE_ID or "default",
        instance_label=INSTANCE_LABEL or (INSTANCE_ID or "default"),
        cache_enabled=False,
    )
    lanes = await reg.list_lanes(local_st, force_refresh=bool(refresh))
    self_val = (lanes[0]["id"] if lanes else "acc1") if reg.is_frontend_gateway else (INSTANCE_ID or "default")
    return {
        "self": self_val,
        "lanes": lanes,
        "is_gateway": reg.is_frontend_gateway,
        "gateway_port": PORT if reg.is_frontend_gateway else None,
    }


@admin_router.get("/v1/lanes/{lane_id}/status")
async def get_lane_status(lane_id: str):
    return _m().session_manager.get_status()


async def _handle_lane_control(lane_id: str, action: str, request: Request):
    if err := _origin_error(request):
        return err
    status_code, data = await _m().lane_registry.control(lane_id, action, _local_control)
    _m().activity.write(
        event="lane_control",
        lane_id=lane_id,
        action=action,
        status=status_code,
        outcome="ok" if status_code == 200 else f"error:{status_code}",
    )
    return JSONResponse(status_code=status_code, content=data)


@admin_router.post("/v1/lanes/{lane_id}/enable")
async def lane_enable(lane_id: str, request: Request):
    return await _handle_lane_control(lane_id, "enable", request)


@admin_router.post("/v1/lanes/{lane_id}/disable")
async def lane_disable(lane_id: str, request: Request):
    return await _handle_lane_control(lane_id, "disable", request)


@admin_router.post("/v1/lanes/{lane_id}/reauth")
async def lane_reauth(lane_id: str, request: Request):
    return await _handle_lane_control(lane_id, "reauth", request)


@admin_router.get("/v1/lanes/{lane_id}/logs")
async def lane_logs(lane_id: str, limit: int = 50):
    limit = max(1, min(limit, 500))
    if lane_id in (INSTANCE_ID or "default", "self"):
        return await get_recent_logs(lines=limit)
    status_code, data = await _m().lane_registry.proxy_request(lane_id, "GET", "logs", query=f"limit={limit}", timeout=5.0)
    return JSONResponse(status_code=status_code, content=data)


@admin_router.get("/v1/lanes/{lane_id}/activity")
async def lane_activity(lane_id: str, limit: int = 50):
    limit = max(1, min(limit, 500))
    if lane_id in (INSTANCE_ID or "default", "self"):
        return await get_activity(limit=limit)
    status_code, data = await _m().lane_registry.proxy_request(lane_id, "GET", "activity", query=f"limit={limit}", timeout=5.0)
    return JSONResponse(status_code=status_code, content=data)


@admin_router.get("/v1/lanes/{lane_id}/models")
async def lane_models(lane_id: str):
    if lane_id in (INSTANCE_ID or "default", "self"):
        return _local_lane_models()
    status_code, data = await _m().lane_registry.proxy_request(lane_id, "GET", "models", timeout=5.0)
    return JSONResponse(status_code=status_code, content=data)


@admin_router.post("/v1/lanes/{lane_id}/switch-model")
async def lane_switch_model(lane_id: str, request: Request):
    if err := _origin_error(request):
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})
    model_name = str(body.get("model", "")).strip()
    keep_chat = bool(body.get("keep_chat", True))
    if lane_id in (INSTANCE_ID or "default", "self"):
        code, data = await _m()._local_switch_model(model_name, keep_chat=keep_chat)
        return JSONResponse(status_code=code, content=data)
    if err := _missing_lane_token_error():
        return err
    status_code, data = await _m().lane_registry.proxy_request(
        lane_id, "POST", "switch-model", body=json.dumps({"model": model_name, "keep_chat": keep_chat}).encode("utf-8"), timeout=10.0
    )
    return JSONResponse(status_code=status_code, content=data)


@admin_router.post("/v1/lanes/{lane_id}/reset")
async def lane_reset(lane_id: str, request: Request):
    if err := _origin_error(request):
        return err
    if lane_id in (INSTANCE_ID or "default", "self"):
        mgr = _m().session_manager
        try:
            mgr.require_on()
        except BridgeError as e:
            return _bridge_err_response(e)
        await mgr.reset_session()
        return {"status": "ok", "message": "Chat session reset successfully"}
    if err := _missing_lane_token_error():
        return err
    status_code, data = await _m().lane_registry.proxy_request(lane_id, "POST", "reset", body=b"{}", timeout=10.0)
    return JSONResponse(status_code=status_code, content=data)


@admin_router.post("/v1/lanes/{lane_id}/cache/clear")
async def lane_cache_clear(lane_id: str, request: Request):
    if err := _origin_error(request):
        return err
    if lane_id in (INSTANCE_ID or "default", "self"):
        return {"status": "ok", "cleared_entries": clear_response_cache()}
    if err := _missing_lane_token_error():
        return err
    status_code, data = await _m().lane_registry.proxy_request(lane_id, "POST", "cache/clear", body=b"{}", timeout=10.0)
    return JSONResponse(status_code=status_code, content=data)


@admin_router.post("/v1/lanes/{lane_id}/accounts/{slot}/cookies")
async def lane_account_cookies(lane_id: str, slot: int, request: Request):
    if err := _origin_error(request):
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    psid = str(body.get("psid") or body.get("SECURE_1PSID") or "").strip()
    psidts = str(body.get("psidts") or body.get("SECURE_1PSIDTS") or "").strip()
    label = str(body.get("label") or body.get("LABEL") or "").strip()

    if lane_id in (INSTANCE_ID or "default", "self"):
        code, data = await _m()._local_update_cookies(slot, psid, psidts, label)
        return JSONResponse(status_code=code, content=data)

    reg = _m().lane_registry
    if lane_id not in reg.peers:
        return JSONResponse(status_code=404, content={"error": {"code": "lane_not_found", "message": f"Lane '{lane_id}' không tồn tại."}})
    if not is_loopback_url(reg.peers[lane_id]):
        return JSONResponse(status_code=403, content={"error": {"code": "cookie_proxy_forbidden", "message": "Hãy mở dashboard của chính luồng đó để nạp cookie (không gửi cookie qua mạng)."}})
    if err := _missing_lane_token_error():
        return err

    req_body = json.dumps({"psid": psid, "psidts": psidts, "label": label}).encode("utf-8")
    status_code, data = await reg.proxy_request(lane_id, "POST", f"accounts/{slot}/cookies", body=req_body, timeout=30.0)
    return JSONResponse(status_code=status_code, content=data)


@admin_router.get("/v1/gateway/routes")
async def gateway_routes():
    gw = getattr(_m(), "gateway_router", None)
    if gw is None:
        return {"active": False}
    return gw.get_stats()
