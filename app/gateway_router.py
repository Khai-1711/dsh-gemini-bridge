"""Bộ định tuyến Gateway thông minh (Sticky Session + Least-Inflight + Failover)."""
import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from app.config import GEMINI_STREAM_OVERALL_TIMEOUT
from app.gemini_session import compute_session_key, is_subagent_messages

logger = logging.getLogger("DSH-Bridge")

STICKY_TTL = 3600.0
MAX_STICKY_SESSIONS = 128
LANE_COOLDOWN_SECONDS = 15.0
FAILOVER_STATUS_CODES = {429, 503}


@dataclass
class StickyBinding:
    lane_id: str
    last_used_ts: float
    is_subagent: bool = False


def _default_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )


class GatewayRouter:
    """Điều phối /v1/models và /v1/chat/completions từ Gateway (:10000) sang các worker."""

    def __init__(
        self,
        lane_registry,
        api_key: str = "",
        activity_cb: Optional[Callable[..., Any]] = None,
        client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
        cooldown_seconds: float = LANE_COOLDOWN_SECONDS,
    ) -> None:
        self.lane_registry = lane_registry
        self.api_key = str(api_key or "").strip()
        self.activity_cb = activity_cb
        self._client_factory = client_factory or _default_http_client
        self.cooldown_seconds = float(cooldown_seconds)
        self.active: bool = False
        self._route_lock = asyncio.Lock()
        self._sticky: Dict[str, StickyBinding] = {}
        self._gateway_inflight: Dict[str, int] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    def _record_activity(self, **fields: Any) -> None:
        if self.activity_cb is None:
            return
        try:
            self.activity_cb(**fields)
        except Exception:
            pass

    def _is_lane_healthy(self, lane: dict, now: float) -> bool:
        lane_id = str(lane.get("id") or "")
        if lane.get("reachable") is not True or lane.get("control") != "on":
            return False
        if lane.get("auth_ok") is not True:
            return False
        if self._cooldown_until.get(lane_id, 0.0) > now:
            return False
        if str(lane.get("account_status") or "").upper() == "EXHAUSTED":
            return False
        accounts = lane.get("accounts") or []
        if accounts and all(a.get("status") == "resting" for a in accounts):
            return False
        return True

    def _is_lane_on(self, lane: dict, now: float) -> bool:
        lane_id = str(lane.get("id") or "")
        return (
            lane.get("reachable") is True
            and lane.get("control") == "on"
            and self._cooldown_until.get(lane_id, 0.0) <= now
        )

    def _evict_sticky_locked(self, now: float) -> None:
        expired = [k for k, v in self._sticky.items() if (now - v.last_used_ts) > STICKY_TTL]
        for key in expired:
            self._sticky.pop(key, None)
        while len(self._sticky) > MAX_STICKY_SESSIONS:
            oldest_key = min(self._sticky, key=lambda k: self._sticky[k].last_used_ts)
            self._sticky.pop(oldest_key, None)

    @staticmethod
    def _lane_supports_model(lane: dict, requested_model: str) -> bool:
        avail = lane.get("available_models") or []
        if not avail:
            return False
        req_low = requested_model.strip().lower()
        return any(str(m).strip().lower() == req_low for m in avail)

    async def reserve_lane(
        self,
        conv_key: str,
        is_new_conv: bool,
        is_subagent: bool,
        exclude: Optional[set] = None,
        requested_model: str = "",
    ) -> Tuple[Optional[dict], Optional[str]]:
        excluded = exclude or set()
        lanes = await self.lane_registry.list_lanes({}, force_refresh=False)
        now = time.time()

        async with self._route_lock:
            self._evict_sticky_locked(now)
            peer_lanes = [
                lane for lane in lanes if not lane.get("is_self") and lane.get("id") not in excluded
            ]
            healthy = [lane for lane in peer_lanes if self._is_lane_healthy(lane, now)]
            candidates = healthy or [lane for lane in peer_lanes if self._is_lane_on(lane, now)]
            if not candidates:
                return None, "Không có luồng worker nào đang BẬT và sẵn sàng (acc1/acc2)."

            req_clean = str(requested_model or "").strip()
            if req_clean and req_clean.lower() not in ("default", "auto", "none", "unspecified", "gemini"):
                matching_model_lanes = [
                    lane for lane in candidates if self._lane_supports_model(lane, req_clean)
                ]
                if matching_model_lanes:
                    candidates = matching_model_lanes

            by_id = {str(lane["id"]): lane for lane in candidates}
            primary_lead_id = next(iter(self.lane_registry.peers.keys()), str(candidates[0]["id"]))
            chosen: Optional[dict] = None

            if not is_new_conv and conv_key in self._sticky:
                pinned_id = self._sticky[conv_key].lane_id
                if pinned_id in by_id:
                    chosen = by_id[pinned_id]

            if chosen is None:
                sticky_counts: Dict[str, int] = {}
                for binding in self._sticky.values():
                    sticky_counts[binding.lane_id] = sticky_counts.get(binding.lane_id, 0) + 1

                def _rank(item: Tuple[int, dict]) -> Tuple[int, int, int, int]:
                    idx, lane = item
                    lid = str(lane["id"])
                    effective_load = max(
                        int(lane.get("inflight") or 0),
                        self._gateway_inflight.get(lid, 0),
                    )
                    if is_subagent and len(candidates) > 1:
                        role_penalty = 0 if lid != primary_lead_id else 1
                    else:
                        role_penalty = 0 if lid == primary_lead_id else 1
                    return (effective_load, role_penalty, sticky_counts.get(lid, 0), idx)

                _, chosen = min(enumerate(candidates), key=_rank)

            chosen_id = str(chosen["id"])
            self._sticky[conv_key] = StickyBinding(
                lane_id=chosen_id, last_used_ts=now, is_subagent=is_subagent
            )
            self._gateway_inflight[chosen_id] = self._gateway_inflight.get(chosen_id, 0) + 1
            return dict(chosen), None

    async def release_lane(self, lane_id: str) -> None:
        async with self._route_lock:
            self._gateway_inflight[lane_id] = max(0, self._gateway_inflight.get(lane_id, 0) - 1)

    async def mark_cooldown(self, lane_id: str, seconds: Optional[float] = None) -> None:
        ttl = self.cooldown_seconds if seconds is None else float(seconds)
        async with self._route_lock:
            self._cooldown_until[lane_id] = time.time() + max(0.0, ttl)

    def _build_forward_headers(self, request_headers: Any, failover_ready: bool = False) -> Dict[str, str]:
        out: Dict[str, str] = {"Content-Type": "application/json"}
        auth_val = request_headers.get("Authorization") or request_headers.get("authorization")
        if auth_val:
            out["Authorization"] = str(auth_val)
        elif self.api_key:
            out["Authorization"] = f"Bearer {self.api_key}"
        for h_name in ("X-Session-Id", "X-DSH-Session-Id", "X-Workspace-Dir", "X-Tool-Profile"):
            val = request_headers.get(h_name) or request_headers.get(h_name.lower())
            if val:
                out[h_name] = str(val)
        if failover_ready:
            out["X-Gateway-Failover-Ready"] = "1"
        return out

    async def proxy_models(self):
        lanes = await self.lane_registry.list_lanes({}, force_refresh=False)
        now = time.time()
        peers = [lane for lane in lanes if not lane.get("is_self")]
        ordered = [lane for lane in peers if self._is_lane_healthy(lane, now)] or [
            lane for lane in peers if self._is_lane_on(lane, now)
        ]
        if not ordered:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "Không có luồng worker nào đang BẬT để đọc danh sách model.",
                        "type": "service_unavailable",
                        "code": "no_healthy_lane",
                    }
                },
            )

        client = self._get_client()
        merged: list = []
        seen_ids: set = set()
        any_ok = False
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

        for lane in ordered:
            base_url = str(lane.get("url") or "").rstrip("/")
            if not base_url:
                continue
            try:
                resp = await client.get(f"{base_url}/v1/models", headers=headers, timeout=5.0)
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                items = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(items, list):
                    continue
                any_ok = True
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    mid = item.get("id")
                    if mid and mid not in seen_ids:
                        seen_ids.add(mid)
                        merged.append(item)
            except Exception:
                continue

        if not any_ok:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "Không lấy được danh sách model từ các luồng worker.",
                        "type": "service_unavailable",
                        "code": "no_healthy_lane",
                    }
                },
            )
        return {"object": "list", "data": merged}

    async def proxy_chat_completions(
        self,
        request: Request,
        body: dict,
        messages: list,
        chat_id: str,
        model_id: str,
        is_stream: bool,
        t0: float,
    ):
        explicit_sid = (
            request.headers.get("X-Session-Id") or request.headers.get("X-DSH-Session-Id") or ""
        ).strip()
        conv_key, is_new_conv = compute_session_key(messages, explicit_sid)
        is_subagent = is_subagent_messages(messages)
        client = self._get_client()

        excluded: set = set()
        last_error_resp: Optional[Tuple[int, bytes, str, str]] = None
        max_attempts = max(2, len(self.lane_registry.peers))

        for attempt in range(max_attempts):
            lane, err_msg = await self.reserve_lane(
                conv_key,
                is_new_conv,
                is_subagent,
                exclude=excluded,
                requested_model=model_id,
            )
            if lane is None:
                if last_error_resp is not None:
                    code, content, media_type, prev_lane = last_error_resp
                    return Response(
                        content=content,
                        status_code=code,
                        media_type=media_type,
                        headers={"X-Gateway-Lane": prev_lane},
                    )
                self._record_activity(
                    request_id=chat_id,
                    model=model_id,
                    stream=is_stream,
                    stage="gateway_route",
                    session_key=conv_key,
                    is_subagent=is_subagent,
                    latency_ms=int((time.time() - t0) * 1000),
                    outcome="error:no_healthy_lane",
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "message": err_msg or "Không có luồng worker sẵn sàng.",
                            "type": "service_unavailable",
                            "code": "no_healthy_lane",
                        }
                    },
                )

            lane_id = str(lane["id"])
            lane_url = str(lane.get("url") or "").rstrip("/")
            target_url = f"{lane_url}{request.url.path}"
            role_tag = "subagent" if is_subagent else "lead"
            has_alternative = any(
                pid not in excluded and pid != lane_id for pid in self.lane_registry.peers
            )
            fwd_headers = self._build_forward_headers(
                request.headers, failover_ready=(attempt + 1 < max_attempts and has_alternative)
            )
            logger.info(
                f"[GATEWAY] Route {conv_key[:8]} ({role_tag}) -> {lane_id} ({lane_url}) "
                f"| inflight={self._gateway_inflight.get(lane_id, 0)}"
            )

            if is_stream:
                upstream_resp: Optional[httpx.Response] = None
                try:
                    req = client.build_request("POST", target_url, json=body, headers=fwd_headers)
                    upstream_resp = await client.send(req, stream=True)
                except (
                    httpx.ConnectError,
                    httpx.ConnectTimeout,
                    httpx.ReadError,
                    httpx.RemoteProtocolError,
                ) as exc:
                    await self.release_lane(lane_id)
                    await self.mark_cooldown(lane_id)
                    excluded.add(lane_id)
                    has_other = any(pid not in excluded for pid in self.lane_registry.peers)
                    if attempt + 1 < max_attempts and has_other:
                        logger.warning(f"[GATEWAY] {lane_id} lỗi kết nối ({exc}); thử chuyển luồng khác.")
                        continue
                    self._record_activity(
                        request_id=chat_id,
                        model=model_id,
                        stream=True,
                        stage="gateway_route",
                        lane_id=lane_id,
                        session_key=conv_key,
                        is_subagent=is_subagent,
                        latency_ms=int((time.time() - t0) * 1000),
                        outcome="error:upstream_connect_error",
                    )
                    return JSONResponse(
                        status_code=503,
                        content={
                            "error": {
                                "message": f"Không kết nối được tới luồng worker '{lane_id}'.",
                                "type": "service_unavailable",
                                "code": "upstream_connect_error",
                            }
                        },
                        headers={"X-Gateway-Lane": lane_id},
                    )
                except Exception:
                    await self.release_lane(lane_id)
                    raise

                if upstream_resp.status_code != 200:
                    try:
                        raw_err = await upstream_resp.aread()
                    finally:
                        await upstream_resp.aclose()
                        await self.release_lane(lane_id)
                    media = upstream_resp.headers.get("content-type", "application/json")
                    status_code = upstream_resp.status_code
                    if status_code in FAILOVER_STATUS_CODES:
                        await self.mark_cooldown(lane_id)
                        excluded.add(lane_id)
                        last_error_resp = (status_code, raw_err, media, lane_id)
                        has_other = any(pid not in excluded for pid in self.lane_registry.peers)
                        if attempt + 1 < max_attempts and has_other:
                            logger.warning(f"[GATEWAY] {lane_id} trả HTTP {status_code}; thử chuyển luồng khác.")
                            continue
                    self._record_activity(
                        request_id=chat_id,
                        model=model_id,
                        stream=True,
                        stage="gateway_route",
                        lane_id=lane_id,
                        session_key=conv_key,
                        is_subagent=is_subagent,
                        latency_ms=int((time.time() - t0) * 1000),
                        outcome=f"http_{status_code}",
                    )
                    return Response(
                        content=raw_err,
                        status_code=status_code,
                        media_type=media,
                        headers={"X-Gateway-Lane": lane_id},
                    )

                async def _stream_passthrough(resp: httpx.Response = upstream_resp, lid: str = lane_id):
                    outcome = "ok"
                    try:
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                yield chunk
                    except (asyncio.CancelledError, GeneratorExit):
                        outcome = "aborted"
                        raise
                    except Exception as exc:
                        outcome = f"error:{type(exc).__name__}"
                        raise
                    finally:
                        try:
                            await resp.aclose()
                        except Exception:
                            pass
                        await self.release_lane(lid)
                        self._record_activity(
                            request_id=chat_id,
                            model=model_id,
                            stream=True,
                            stage="gateway_route",
                            lane_id=lid,
                            session_key=conv_key,
                            is_subagent=is_subagent,
                            latency_ms=int((time.time() - t0) * 1000),
                            outcome=outcome,
                        )

                return StreamingResponse(
                    _stream_passthrough(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                        "X-Gateway-Lane": lane_id,
                    },
                )

            # Non-streaming proxy pass-through
            released = False
            non_stream_read_timeout = max(240.0, float(GEMINI_STREAM_OVERALL_TIMEOUT) + 30.0)
            try:
                resp = await client.post(
                    target_url,
                    json=body,
                    headers=fwd_headers,
                    timeout=httpx.Timeout(
                        connect=10.0, read=non_stream_read_timeout, write=30.0, pool=10.0
                    ),
                )
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadError,
                httpx.RemoteProtocolError,
            ) as exc:
                await self.release_lane(lane_id)
                released = True
                await self.mark_cooldown(lane_id)
                excluded.add(lane_id)
                has_other = any(pid not in excluded for pid in self.lane_registry.peers)
                if attempt + 1 < max_attempts and has_other:
                    logger.warning(f"[GATEWAY] {lane_id} lỗi kết nối ({exc}); thử chuyển luồng khác.")
                    continue
                self._record_activity(
                    request_id=chat_id,
                    model=model_id,
                    stream=False,
                    stage="gateway_route",
                    lane_id=lane_id,
                    session_key=conv_key,
                    is_subagent=is_subagent,
                    latency_ms=int((time.time() - t0) * 1000),
                    outcome="error:upstream_connect_error",
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "message": f"Không kết nối được tới luồng worker '{lane_id}'.",
                            "type": "service_unavailable",
                            "code": "upstream_connect_error",
                        }
                    },
                    headers={"X-Gateway-Lane": lane_id},
                )
            finally:
                if not released:
                    await self.release_lane(lane_id)

            media = resp.headers.get("content-type", "application/json")
            if resp.status_code in FAILOVER_STATUS_CODES:
                await self.mark_cooldown(lane_id)
                excluded.add(lane_id)
                last_error_resp = (resp.status_code, resp.content, media, lane_id)
                has_other = any(pid not in excluded for pid in self.lane_registry.peers)
                if attempt + 1 < max_attempts and has_other:
                    logger.warning(f"[GATEWAY] {lane_id} trả HTTP {resp.status_code}; thử chuyển luồng khác.")
                    continue

            self._record_activity(
                request_id=chat_id,
                model=model_id,
                stream=False,
                stage="gateway_route",
                lane_id=lane_id,
                session_key=conv_key,
                is_subagent=is_subagent,
                latency_ms=int((time.time() - t0) * 1000),
                outcome="ok" if resp.status_code == 200 else f"http_{resp.status_code}",
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=media,
                headers={"X-Gateway-Lane": lane_id},
            )

        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "Không có luồng worker sẵn sàng.",
                    "type": "service_unavailable",
                    "code": "no_healthy_lane",
                }
            },
        )

    def get_stats(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "active": self.active,
            "inflight": dict(self._gateway_inflight),
            "cooldowns": {
                lid: round(until - now, 1)
                for lid, until in self._cooldown_until.items()
                if until > now
            },
            "sticky_count": len(self._sticky),
            "sticky": {
                k: {
                    "lane_id": b.lane_id,
                    "is_subagent": b.is_subagent,
                    "age_s": round(now - b.last_used_ts, 1),
                }
                for k, b in self._sticky.items()
            },
        }
