"""Hàm THUẦN cho an toàn mạng LAN và cấu hình 2 luồng. KHÔNG import app.config, KHÔNG đọc biến môi trường."""
import hmac
import ipaddress
import re
from typing import Dict, Iterable, Tuple
from urllib.parse import urlsplit

_RFC1918 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")


def _parse_ip(host: str):
    try:
        return ipaddress.ip_address((host or "").strip().strip("[]"))
    except ValueError:
        return None


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if h == "localhost":
        return True
    ip = _parse_ip(h)
    return bool(ip and ip.is_loopback)


def is_rfc1918_host(host: str) -> bool:
    ip = _parse_ip(host)
    return bool(ip and ip.version == 4 and any(ip in net for net in _RFC1918))


def check_bind_safety(host: str, api_key: str, allow_insecure: bool) -> Tuple[bool, str]:
    """Trả (được_phép_khởi_động, thông_báo). HOST không phải loopback mà không có API_KEY -> TỪ CHỐI."""
    if is_loopback_host(host):
        return True, ""
    if (api_key or "").strip():
        return True, ""
    if allow_insecure:
        return True, ("CẢNH BÁO: đang mở ra mạng (HOST=%s) mà KHÔNG có API_KEY (ALLOW_INSECURE_LAN=true). "
                      "Bất kỳ máy nào trong mạng cũng dùng được tài khoản của bạn." % host)
    return False, ("TỪ CHỐI KHỞI ĐỘNG: HOST=%s mở ra mạng nhưng API_KEY đang rỗng. "
                   "Hãy đặt API_KEY trong file .env, hoặc dùng HOST=127.0.0.1." % host)


def split_host_header(value: str) -> str:
    v = (value or "").strip()
    if v.startswith("["):
        end = v.find("]")
        return v[1:end] if end != -1 else ""
    if v.count(":") == 1:
        return v.rsplit(":", 1)[0]
    return v


def is_host_header_allowed(host_header: str, extra_allowed: Iterable[str] = ()) -> bool:
    """Chống DNS rebinding: chỉ nhận Host là loopback, IP LAN (RFC1918) hoặc nằm trong danh sách cho phép."""
    host = split_host_header(host_header).lower()
    if not host:
        return False
    if is_loopback_host(host) or is_rfc1918_host(host):
        return True
    return host in {h.strip().lower() for h in extra_allowed if h and h.strip()}


def is_cross_site_browser_request(
    method: str,
    origin: str,
    sec_fetch_site: str,
    extra_allowed: Iterable[str] = (),
) -> bool:
    """Chặn CSRF từ trang web lạ trong trình duyệt gọi vào bridge."""
    if (method or "").strip().upper() in ("GET", "HEAD", "OPTIONS"):
        return False
    if (sec_fetch_site or "").strip().lower() == "cross-site":
        return True
    orig = (origin or "").strip()
    if not orig:
        return False
    if orig.lower() == "null":
        return True
    if any(ch.isspace() or ch == "\\" for ch in orig):
        return True
    try:
        parsed = urlsplit(orig)
        port = parsed.port
    except ValueError:
        return True
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.netloc.endswith(":")
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port <= 0)
    ):
        return True
    hostname = parsed.hostname or ""
    if not hostname:
        return True
    if ":" in hostname and not parsed.netloc.startswith("["):
        return True
    host_for_check = f"[{hostname}]" if ":" in hostname else hostname
    return not is_host_header_allowed(host_for_check, extra_allowed)


def parse_peer_lanes(raw: str, self_id: str) -> Dict[str, str]:
    """'acc2=http://127.0.0.1:10001,acc3=http://192.168.1.5:10002' -> {id: url}.
    Bỏ mục sai định dạng, trùng ID của chính mình, hoặc host không phải loopback/LAN."""
    peers: Dict[str, str] = {}
    for item in re.split(r"[,\s;]+", (raw or "").strip()):
        if "=" not in item:
            continue
        lane_id, url = item.split("=", 1)
        lane_id, url = lane_id.strip(), url.strip()
        if not _ID_RE.fullmatch(lane_id) or lane_id == (self_id or ""):
            continue
        m = re.fullmatch(r"https?://(\[[0-9a-fA-F:]+\]|[^/:\s]+)(:\d{1,5})?/?", url)
        if not m:
            continue
        host = m.group(1).strip("[]")
        if not (is_loopback_host(host) or is_rfc1918_host(host)):
            continue
        peers[lane_id] = url.rstrip("/")
    return peers


def is_loopback_url(url: str) -> bool:
    m = re.fullmatch(r"https?://(\[[0-9a-fA-F:]+\]|[^/:\s]+)(:\d{1,5})?/?", (url or "").strip())
    return bool(m and is_loopback_host(m.group(1).strip("[]")))


def lane_token_ok(provided: str, expected: str) -> bool:
    """So khớp token giữa 2 luồng bằng hmac.compare_digest. Thiếu token = KHÔNG BAO GIỜ khớp."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
