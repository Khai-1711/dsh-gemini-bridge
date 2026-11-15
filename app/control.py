"""Hàm thuần túy cho công tắc bật/tắt (dễ test, không phụ thuộc cấu hình)."""
import ipaddress
from typing import Iterable, Optional
from urllib.parse import urlparse


def parse_start_state(raw: Optional[str]) -> bool:
    """True nếu bridge phải TỰ BẬT lúc khởi động. Mặc định (rỗng/lạ) là TẮT."""
    return (raw or "").strip().lower() in ("on", "true", "1", "yes")


def is_origin_allowed(
    origin: Optional[str], port: int, allowed_origins: Optional[Iterable[str]] = None
) -> bool:
    """Chặn trang web lạ trong trình duyệt gọi vào bridge.
    Request không có header Origin (DSH, curl, script) được cho qua.
    Dashboard cùng cổng (127.0.0.1, localhost hoặc IP mạng LAN riêng) được cho qua.
    """
    if not origin:
        return True

    if allowed_origins and origin in allowed_origins:
        return True

    try:
        parsed = urlparse(origin)
        if parsed.scheme != "http":
            return False
        if parsed.port != port:
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        if hostname in ("localhost", "127.0.0.1"):
            return True
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback:
                return True
        except ValueError:
            pass
        return False
    except Exception:
        return False
