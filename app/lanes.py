"""Tổng hợp trạng thái các luồng (lanes) và điều khiển proxy server-to-server.
Không import app.config ở phạm vi toàn cục để dễ test và tránh phụ thuộc vòng.
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit
from app.lane_security import is_loopback_url

logger = logging.getLogger("dsh.lanes")


def check_env_file_id(lane_id: str, base_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Kiểm tra xem file .env tương ứng với lane_id có chứa SECURE_1PSID hợp lệ không (Zero-Leak).
    Trả về (has_env_id, env_filename).
    """
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent
    is_gw = lane_id in ("frontend", "gateway", "10000", "default", "")
    env_name = ".env" if is_gw else f".env.{lane_id}"
    env_path = base_dir / env_name
    if not env_path.exists():
        if not is_gw and lane_id == "default":
            env_path = base_dir / ".env"
            env_name = ".env"
        if not env_path.exists():
            return False, env_name

    try:
        content = env_path.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == "SECURE_1PSID" and len(v) >= 10:
                    return True, env_name
    except Exception:
        pass
    return False, env_name


def _port_from_url(url: str) -> Optional[int]:
    try:
        parsed = urlsplit(url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _parse_uptime(uptime_val: Any) -> int:
    if isinstance(uptime_val, (int, float)):
        return int(uptime_val)
    if isinstance(uptime_val, str) and "h" in uptime_val:
        # e.g. "1h 2m 3s"
        try:
            parts = uptime_val.replace("s", "").split()
            h = int(parts[0].replace("h", ""))
            m = int(parts[1].replace("m", ""))
            s = int(parts[2])
            return h * 3600 + m * 60 + s
        except Exception:
            return 0
    return 0


def spawn_local_lane(lane_id: str) -> bool:
    """Tự động khởi chạy tiến trình luồng con hoặc gateway trên máy cục bộ (Windows / Linux)."""
    try:
        base_dir = Path(__file__).resolve().parent.parent
        is_gw = lane_id in ("frontend", "gateway", "10000")
        env_file = base_dir / ".env" if is_gw else base_dir / f".env.{lane_id}"
        if not env_file.exists():
            return False

        env = os.environ.copy()
        # Không truyền rò rỉ biến cấu hình/cookie của tiến trình Gateway cha sang worker con
        for leak_key in (
            "GEMINI_COOKIE_PATH",
            "SECURE_1PSID",
            "SECURE_1PSIDTS",
            "BACKUP_1PSID",
            "BACKUP_1PSIDTS",
            "BACKUP_1_LABEL",
            "BACKUP_2PSID",
            "BACKUP_2PSIDTS",
            "BACKUP_2_LABEL",
            "BACKUP_3PSID",
            "BACKUP_3PSIDTS",
            "BACKUP_3_LABEL",
            "BACKUP_4PSID",
            "BACKUP_4PSIDTS",
            "BACKUP_4_LABEL",
            "INSTANCE_LABEL",
            "PORT",
            "PEER_LANES",
            "GATEWAY_MODE",
        ):
            env.pop(leak_key, None)
        if not is_gw:
            env["BRIDGE_INSTANCE"] = lane_id
        else:
            env.pop("BRIDGE_INSTANCE", None)

        log_dir = base_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        spawn_log = open(log_dir / f"spawn_{lane_id}.log", "a", encoding="utf-8")
        try:
            if sys.platform == "win32":
                CREATE_NO_WINDOW = 0x08000000
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                subprocess.Popen(
                    [sys.executable, str(base_dir / "main.py")],
                    cwd=str(base_dir),
                    env=env,
                    creationflags=flags,
                    stdout=spawn_log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
            else:
                subprocess.Popen(
                    [sys.executable, str(base_dir / "main.py")],
                    cwd=str(base_dir),
                    env=env,
                    stdout=spawn_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        finally:
            spawn_log.close()
        return True
    except Exception as exc:
        logger.error(f"[LANE] Lỗi spawn tiến trình '{lane_id}': {exc}")
        return False


def build_local_lane_status(manager, port: int = 10000, instance_id: str = "", instance_label: str = "", cache_enabled: bool = False) -> Dict[str, Any]:
    """Dựng trạng thái của chính luồng này theo đúng lược đồ JSON chuẩn."""
    status = manager.get_status() if hasattr(manager, "get_status") else {}
    accounts_in = status.get("accounts", [])
    accs = []
    for a in accounts_in:
        accs.append({
            "label": a.get("label", ""),
            "status": a.get("status", "ready"),
            "resting_until": a.get("resting_until"),
            "reason": a.get("reason"),
        })

    active_acc_label = status.get("active_account", "")
    total_accs = len(accounts_in)
    active_idx = getattr(manager, "active_index", 0)
    uptime_s = int(time.time() - getattr(manager, "start_time", time.time()))
    is_on = getattr(manager, "state", "off") == "on"
    is_guest = getattr(manager, "is_guest", False) is True

    has_env_id = status.get("has_env_id") if isinstance(status, dict) else None
    env_file = status.get("env_file") if isinstance(status, dict) else None
    id_status = status.get("id_status") if isinstance(status, dict) else None

    if has_env_id is None or env_file is None or id_status is None:
        chk_has, chk_file = check_env_file_id(instance_id)
        if has_env_id is None:
            has_env_id = chk_has
        if env_file is None:
            env_file = chk_file
        if id_status is None:
            if not has_env_id:
                id_status = "missing"
            elif is_on and not getattr(manager, "auth_ok", False):
                id_status = "expired"
            elif is_on and getattr(manager, "auth_ok", False):
                id_status = "valid"
            else:
                id_status = "ready"

    return {
        "id": instance_id or "default",
        "label": instance_label or (instance_id or "default"),
        "url": f"http://127.0.0.1:{port}",
        "port": port,
        "is_self": True,
        "reachable": True,
        "control": getattr(manager, "state", "off"),
        "inflight": getattr(manager, "_inflight", 0),
        "auth_ok": bool(manager.auth_ok) if is_on else False,
        "account_status": (
            "GUEST" if is_guest
            else (getattr(manager, "account_status_name", "UNKNOWN") or "UNKNOWN")
        ) if is_on else "UNKNOWN",
        "active_account": {
            "label": str(active_acc_label),
            "index": active_idx,
            "total": total_accs,
        },
        "accounts": accs,
        "model": getattr(manager, "default_model", "unspecified") or "unspecified",
        "available_models": list(getattr(manager, "available_models", []) or []),
        "turn_count": int(getattr(manager, "turn_count", 0) or 0),
        "requests": getattr(manager, "total_requests", 0),
        "errors": getattr(manager, "total_errors", 0),
        "uptime_s": max(0, uptime_s),
        "chat_key": manager.current_chat_key(),
        "last_activity_ts": status.get("last_activity_ts"),
        "cache_enabled": bool(cache_enabled),
        "controllable": True,
        "has_env_id": bool(has_env_id),
        "env_file": str(env_file),
        "id_status": str(id_status),
        "lane_resting_until": status.get("lane_resting_until"),
    }


def _format_remote_status(lane_id: str, base_url: str, data: dict, controllable: bool) -> Dict[str, Any]:
    """Định dạng dữ liệu trả về từ /v1/status của luồng khác về lược đồ chuẩn."""
    ctrl_state = data.get("state", "off")
    accounts_in = data.get("accounts", [])
    accs = []
    for a in accounts_in:
        accs.append({
            "label": a.get("label", ""),
            "status": a.get("status", "ready"),
            "resting_until": a.get("resting_until"),
            "reason": a.get("reason"),
        })

    active_acc_label = data.get("active_account", "")
    total_accs = len(accounts_in)
    active_idx = 0
    for idx, a in enumerate(accounts_in):
        if a.get("label") == active_acc_label or a.get("status") == "active":
            active_idx = idx
            break

    uptime_s = data.get("uptime_seconds", 0) or _parse_uptime(data.get("uptime"))

    has_env_id = data.get("has_env_id") if isinstance(data, dict) else None
    env_file = data.get("env_file") if isinstance(data, dict) else None
    id_status = data.get("id_status") if isinstance(data, dict) else None

    if has_env_id is None or env_file is None or id_status is None:
        chk_has, chk_file = check_env_file_id(lane_id)
        if has_env_id is None:
            has_env_id = chk_has
        if env_file is None:
            env_file = chk_file
        if id_status is None:
            if not has_env_id:
                id_status = "missing"
            elif ctrl_state == "on" and not bool(data.get("auth_ok", False)):
                id_status = "expired"
            elif ctrl_state == "on" and bool(data.get("auth_ok", False)):
                id_status = "valid"
            else:
                id_status = "ready"

    return {
        "id": lane_id,
        "label": data.get("instance_label") or lane_id,
        "url": base_url,
        "port": _port_from_url(base_url),
        "is_self": False,
        "reachable": True,
        "control": ctrl_state,
        "inflight": data.get("inflight", 0),
        "auth_ok": bool(data.get("auth_ok", False)),
        "account_status": (
            "GUEST" if bool(data.get("is_guest"))
            else (data.get("account_status", "UNKNOWN") or "UNKNOWN")
        ),
        "active_account": {
            "label": str(active_acc_label),
            "index": active_idx,
            "total": total_accs,
        },
        "accounts": accs,
        "model": data.get("model", "unspecified") or "unspecified",
        "available_models": list(data.get("available_models") or []),
        "turn_count": int(data.get("turn_count", 0) or 0),
        "requests": data.get("total_requests", 0),
        "errors": data.get("total_errors", 0),
        "uptime_s": int(uptime_s),
        "chat_key": data.get("chat_key", "") or "",
        "last_activity_ts": data.get("last_activity_ts"),
        "cache_enabled": bool(data.get("cache_enabled", False)),
        "controllable": controllable,
        "has_env_id": bool(has_env_id),
        "env_file": str(env_file),
        "id_status": str(id_status),
        "lane_resting_until": data.get("lane_resting_until"),
    }


def _urllib_sync(method: str, url: str, headers: dict, body: Optional[bytes], timeout: float) -> Tuple[int, Optional[dict]]:
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            try:
                parsed = json.loads(data.decode("utf-8"))
            except Exception:
                parsed = {"raw": data.decode("utf-8", errors="replace")}
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        try:
            parsed = json.loads(exc.read().decode("utf-8"))
        except Exception:
            parsed = {"error": exc.reason}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc.reason)}
    except TimeoutError:
        return 0, {"error": "timeout"}
    except Exception as exc:
        return 0, {"error": str(exc)}


async def urllib_call(method: str, url: str, headers: dict = None, body: bytes = None, timeout: float = 2.0) -> Tuple[int, Optional[dict]]:
    """Gọi HTTP an toàn bằng urllib trong worker thread, bắt mọi ngoại lệ."""
    return await asyncio.to_thread(_urllib_sync, method, url, headers or {}, body, timeout)


class LaneRegistry:
    """Bộ điều phối danh bạ và gọi chéo giữa các luồng agent."""

    def __init__(
        self,
        self_id: str,
        peers: Dict[str, str],
        token: str,
        http_call: Callable = urllib_call,
        cache_ttl: float = 4.0,
        spawn_fn: Optional[Callable[[str], bool]] = None,
        is_frontend_gateway: bool = False,
    ):
        self.self_id = self_id or "default"
        self.peers = peers or {}
        self.token = token or ""
        self.http_call = http_call
        self.cache_ttl = cache_ttl
        self.spawn_fn = spawn_fn
        self.is_frontend_gateway = is_frontend_gateway
        self._peer_status_cache: Dict[str, Dict[str, Any]] = {}
        self._peer_cache_ts: Dict[str, float] = {}
        self._lane_locks: Dict[str, asyncio.Lock] = {}
        self._cache_lock = asyncio.Lock()

    def _get_lane_lock(self, lane_id: str) -> asyncio.Lock:
        lock = self._lane_locks.get(lane_id)
        if lock is None:
            lock = asyncio.Lock()
            self._lane_locks[lane_id] = lock
        return lock

    async def list_lanes(self, local_status: dict, force_refresh: bool = False) -> List[Dict[str, Any]]:
        lanes = [] if self.is_frontend_gateway else [local_status]
        tasks = []
        peer_ids = []
        peer_results = {}
        now = time.time()

        for peer_id, base_url in self.peers.items():
            async with self._cache_lock:
                cached = self._peer_status_cache.get(peer_id)
                cached_ts = self._peer_cache_ts.get(peer_id, 0.0)
            cached_age = now - cached_ts
            is_down_or_off = bool(
                cached and (cached.get("reachable") is False or cached.get("control") == "off")
            )
            online_ttl = min(1.5, self.cache_ttl) if self.cache_ttl > 0 else 0.0
            cache_valid = bool(
                not force_refresh
                and cached
                and (
                    (is_down_or_off and cached_age < self.cache_ttl)
                    or (not is_down_or_off and online_ttl > 0 and cached_age < online_ttl)
                )
            )
            if cache_valid:
                cached_lane = dict(cached)
                chk_has, chk_file = check_env_file_id(peer_id)
                cached_lane.update({
                    "url": base_url,
                    "port": _port_from_url(base_url),
                    "controllable": bool(self.token),
                    "has_env_id": cached.get("has_env_id", chk_has),
                    "env_file": cached.get("env_file", chk_file),
                    "id_status": cached.get("id_status", "ready" if chk_has else "missing"),
                })
                peer_results[peer_id] = cached_lane
                continue

            peer_ids.append((peer_id, base_url))
            headers = {}
            if self.token:
                headers["X-Lane-Token"] = self.token
            url = f"{base_url}/v1/lanes/{peer_id}/status"
            probe_timeout = 0.8 if is_loopback_url(base_url) else 2.0
            tasks.append(self.http_call("GET", url, headers=headers, body=None, timeout=probe_timeout))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for (p_id, p_url), res in zip(peer_ids, results, strict=False):
                chk_has, chk_file = check_env_file_id(p_id)
                if isinstance(res, Exception) or not isinstance(res, tuple) or res[0] == 0:
                    err_msg = res[1].get("error", "unreachable") if (isinstance(res, tuple) and len(res) > 1 and isinstance(res[1], dict)) else str(res)
                    lane = {
                        "id": p_id,
                        "label": p_id,
                        "url": p_url,
                        "port": _port_from_url(p_url),
                        "is_self": False,
                        "reachable": False,
                        "error": err_msg,
                        "control": "off",
                        "controllable": bool(self.token),
                        "has_env_id": chk_has,
                        "env_file": chk_file,
                        "id_status": "ready" if chk_has else "missing",
                    }
                else:
                    status_code, data = res
                    if status_code == 200 and isinstance(data, dict):
                        lane = _format_remote_status(p_id, p_url, data, controllable=bool(self.token))
                    else:
                        lane = {
                            "id": p_id,
                            "label": p_id,
                            "url": p_url,
                            "port": _port_from_url(p_url),
                            "is_self": False,
                            "reachable": False,
                            "error": f"HTTP {status_code}",
                            "control": "off",
                            "controllable": bool(self.token),
                            "has_env_id": chk_has,
                            "env_file": chk_file,
                            "id_status": "ready" if chk_has else "missing",
                        }
                async with self._cache_lock:
                    self._peer_status_cache[p_id] = dict(lane)
                    self._peer_cache_ts[p_id] = time.time()
                peer_results[p_id] = lane

        lanes.extend(peer_results[peer_id] for peer_id in self.peers if peer_id in peer_results)
        return lanes

    async def control(self, lane_id: str, action: str, local_fn: Callable) -> Tuple[int, dict]:
        if lane_id == self.self_id:
            return await local_fn(action)

        if lane_id not in self.peers:
            return 404, {"error": {"code": "lane_not_found", "message": f"Lane '{lane_id}' không tồn tại."}}

        if not self.token:
            return 403, {"error": {"code": "lane_not_controllable", "message": "Thiếu LANE_ADMIN_TOKEN để điều khiển luồng khác."}}

        peer_url = self.peers[lane_id]
        if action == "enable":
            target_url = f"{peer_url}/v1/lanes/{lane_id}/enable"
            timeout = 30.0
        elif action == "disable":
            target_url = f"{peer_url}/v1/lanes/{lane_id}/disable"
            timeout = 130.0
        elif action == "reauth":
            target_url = f"{peer_url}/v1/lanes/{lane_id}/reauth"
            timeout = 30.0
        else:
            return 400, {"error": {"code": "invalid_action", "message": f"Hành động '{action}' không hợp lệ."}}

        headers = {
            "X-Lane-Token": self.token,
            "Content-Type": "application/json",
        }
        async with self._get_lane_lock(lane_id):
            async with self._cache_lock:
                cached = self._peer_status_cache.get(lane_id)
            is_known_down = bool(cached and cached.get("reachable") is False)
            call_timeout = 2.0 if (action == "enable" and (is_known_down or self.spawn_fn)) else timeout
            status_code, body = await self.http_call("POST", target_url, headers=headers, body=b"{}", timeout=call_timeout)
            if status_code == 0 and action == "enable" and self.spawn_fn and is_loopback_url(peer_url):
                if self.spawn_fn(lane_id):
                    for _ in range(12):
                        await asyncio.sleep(0.8)
                        st_code, st_body = await self.http_call("GET", f"{peer_url}/v1/lanes/{lane_id}/status", headers=headers, body=None, timeout=2.0)
                        if st_code == 200 and isinstance(st_body, dict):
                            if st_body.get("state") != "on":
                                status_code, body = await self.http_call("POST", target_url, headers=headers, body=b"{}", timeout=timeout)
                            else:
                                status_code, body = 200, st_body
                            break

            if status_code == 0:
                err = body.get("error") if isinstance(body, dict) else "unreachable"
                async with self._cache_lock:
                    self._peer_status_cache[lane_id] = {
                        "id": lane_id,
                        "label": lane_id,
                        "url": peer_url,
                        "port": _port_from_url(peer_url),
                        "is_self": False,
                        "reachable": False,
                        "error": err,
                        "control": "off",
                        "controllable": bool(self.token),
                    }
                    self._peer_cache_ts[lane_id] = time.time()
                return 502, {"error": {"code": "peer_unreachable", "message": f"Không kết nối được tới luồng {lane_id}: {err}"}}

            if 200 <= status_code < 300:
                async with self._cache_lock:
                    cached_entry = dict(self._peer_status_cache.get(lane_id, {}))
                    cached_entry.update({
                        "id": lane_id,
                        "label": (body.get("instance_label") or lane_id) if isinstance(body, dict) else lane_id,
                        "url": peer_url,
                        "port": _port_from_url(peer_url),
                        "is_self": False,
                        "reachable": True,
                        "controllable": bool(self.token),
                    })
                    if isinstance(body, dict):
                        cached_entry.update({key: body[key] for key in ("control", "state", "auth_ok") if key in body})
                        if "state" in body:
                            cached_entry["control"] = body["state"]
                        elif action == "enable":
                            cached_entry["control"] = "starting"
                        elif action == "disable":
                            cached_entry["control"] = "stopping"
                    elif action == "enable":
                        cached_entry["control"] = "starting"
                    elif action == "disable":
                        cached_entry["control"] = "stopping"
                    self._peer_status_cache[lane_id] = cached_entry
                    self._peer_cache_ts[lane_id] = 0.0
            return status_code, body or {}

    async def proxy_request(
        self,
        lane_id: str,
        method: str,
        subpath: str,
        query: str = "",
        body: Optional[bytes] = None,
        timeout: float = 5.0,
    ) -> Tuple[int, dict]:
        """Gọi proxy sang một luồng peer với header X-Lane-Token."""
        if lane_id == self.self_id:
            return 400, {"error": {"code": "cannot_proxy_self", "message": "Không proxy tới chính mình."}}

        if lane_id not in self.peers:
            return 404, {"error": {"code": "lane_not_found", "message": f"Lane '{lane_id}' không tồn tại."}}

        peer_url = self.peers[lane_id]
        url = f"{peer_url}/v1/lanes/{lane_id}/{subpath.lstrip('/')}"
        if query:
            url += f"?{query}"

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Lane-Token"] = self.token

        status_code, resp_body = await self.http_call(
            method, url, headers=headers, body=body, timeout=timeout
        )
        if status_code == 0:
            err = resp_body.get("error") if isinstance(resp_body, dict) else "unreachable"
            async with self._cache_lock:
                cached = dict(self._peer_status_cache.get(lane_id, {}))
                cached.update({
                    "id": lane_id,
                    "label": cached.get("label") or lane_id,
                    "url": peer_url,
                    "port": _port_from_url(peer_url),
                    "is_self": False,
                    "reachable": False,
                    "error": err,
                    "controllable": bool(self.token),
                })
                self._peer_status_cache[lane_id] = cached
                self._peer_cache_ts[lane_id] = time.time()
            return 502, {
                "error": {
                    "code": "peer_unreachable",
                    "message": f"Không kết nối được tới luồng {lane_id}: {err}",
                }
            }
        return status_code, resp_body or {}
