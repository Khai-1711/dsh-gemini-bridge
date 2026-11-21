"""Điểm vào FastAPI mỏng cho DSH Gemini Bridge (khởi tạo app, middleware và gắn router)."""
from contextlib import asynccontextmanager
import hmac
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.activity import ActivityJournal
from app.config import (
    ALLOW_INSECURE_LAN,
    ALLOWED_HOSTS,
    API_KEY,
    API_START_ON,
    BASE_DIR,
    HOST,
    INSTANCE_ID,
    INSTANCE_PATHS,
    IS_GATEWAY_INSTANCE,
    LANE_ADMIN_TOKEN,
    LOG_DIR,
    PEER_LANES,
    PORT,
    SSE_HEARTBEAT_INTERVAL,  # noqa: F401
    logger,
)
from app.gemini_session import session_manager
from app.gateway_router import GatewayRouter
from app.lane_security import check_bind_safety, is_host_header_allowed, lane_token_ok
from app.lanes import LaneRegistry, spawn_local_lane
from app.prompt_builder import build_incremental_prompt_and_files  # noqa: F401

FRONTEND_DIR = BASE_DIR / "frontend"
activity = ActivityJournal(INSTANCE_PATHS["activity_file"], INSTANCE_ID)
FAILURE_TRACES_FILE = LOG_DIR / (
    f"failure_traces_{INSTANCE_ID}.jsonl" if INSTANCE_ID else "failure_traces.jsonl"
)
session_manager.on_event = lambda **kw: activity.write(**kw)

is_frontend_gateway = IS_GATEWAY_INSTANCE
lane_registry = LaneRegistry(
    self_id=INSTANCE_ID or ("frontend" if is_frontend_gateway else "default"),
    peers=PEER_LANES,
    token=LANE_ADMIN_TOKEN,
    spawn_fn=spawn_local_lane,
    is_frontend_gateway=is_frontend_gateway,
)
gateway_router = GatewayRouter(
    lane_registry=lane_registry,
    api_key=API_KEY,
    activity_cb=lambda **kw: activity.write(**kw),
)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    ok, msg = check_bind_safety(HOST, API_KEY, ALLOW_INSECURE_LAN)
    if not ok:
        print("[LỖI CẤU HÌNH] " + msg)
        logger.critical(msg)
        sys.exit(1)
    if msg:
        logger.critical(msg)

    if is_frontend_gateway:
        gateway_router.active = True
        logger.info(
            f"[GATEWAY] Khởi động Cổng Frontend Gateway trên port {PORT}. Sẵn sàng điều phối các luồng: {list(PEER_LANES.keys())}."
        )
    elif API_START_ON:
        try:
            await session_manager.enable()
        except Exception as exc:
            logger.error(f"[CONTROL] Tự bật lúc khởi động thất bại: {exc}")
    else:
        logger.info("[CONTROL] Bridge khởi động ở trạng thái TẮT. Hãy bật ở dashboard.")
    yield
    gateway_router.active = False
    await gateway_router.close()
    await session_manager.close()


app = FastAPI(
    title="DSH Gemini Agent Bridge (Enterprise Edition)",
    version="3.0.0",
    lifespan=lifespan,
)
app.state.session_manager = session_manager
app.state.lane_registry = lane_registry
app.state.gateway_router = gateway_router
app.state.activity = activity

if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if not is_host_header_allowed(request.headers.get("host", ""), ALLOWED_HOSTS):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Host không được phép", "code": "host_not_allowed"}},
        )

    path = request.url.path
    method = request.method.upper()
    is_whitelisted = (method == "GET" and path in ("/", "/health", "/dashboard", "/favicon.ico")) or (
        method == "GET" and path.startswith("/frontend")
    )

    if API_KEY and not is_whitelisted:
        auth_header = request.headers.get("Authorization", "").strip()
        token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else auth_header
        key_ok = bool(token) and hmac.compare_digest(token.encode("utf-8"), API_KEY.encode("utf-8"))
        lane_header = request.headers.get("X-Lane-Token", "").strip()
        lane_ok = path.startswith("/v1/lanes") and bool(lane_header) and lane_token_ok(lane_header, LANE_ADMIN_TOKEN)
        if not (key_ok or lane_ok):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized: Invalid or missing API Key",
                    "message": "Unauthorized: Invalid or missing API Key",
                    "code": "unauthorized",
                },
            )

    response = await call_next(request)
    if path.startswith("/frontend") or path in ("/", "/dashboard"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


from app.openai_adapter import chat_completions, list_models, openai_router  # noqa: E402, F401
from app.routes_admin import (  # noqa: E402, F401
    _local_control,
    _local_lane_models,
    _local_switch_model,
    _local_update_cookies,
    _resolve_chat_model,
    _save_env_cookies,
    admin_router,
    clear_cache,
    clear_exhausted,
    control_disable,
    control_enable,
    control_status,
    dashboard_ui,
    full_status,
    gateway_shutdown,
    gateway_spawn,
    get_activity,
    get_lane_status,
    get_recent_logs,
    health_check,
    lane_account_cookies,
    lane_activity,
    lane_cache_clear,
    lane_disable,
    lane_enable,
    lane_logs,
    lane_models,
    lane_reauth,
    lane_reset,
    lane_switch_model,
    list_lanes,
    local_reauth,
    reset_chat_session,
    switch_model,
    update_account_cookies,
    update_cookies,
)

app.include_router(admin_router)
app.include_router(openai_router)

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
