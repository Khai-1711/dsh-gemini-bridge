"""Cập nhật giá trị trong nội dung file .env. Hàm THUẦN: nhận chuỗi, trả chuỗi. KHÔNG đọc/ghi file, KHÔNG log giá trị."""
import re
from typing import Dict

_VALUE_RE = re.compile(r"[A-Za-z0-9._\-=+/%~]+")  # cookie chỉ gồm các ký tự này
_LABEL_RE = re.compile(r"[^\"\\\r\n]{1,40}")  # nhãn: không có ", \, xuống dòng; tối đa 40 ký tự


def is_valid_cookie_value(value: str) -> bool:
    return bool(value) and bool(_VALUE_RE.fullmatch(value))


def is_valid_label(value: str) -> bool:
    return bool(_LABEL_RE.fullmatch(value or ""))


def update_env_values(text: str, updates: Dict[str, str]) -> str:
    """Thay dòng KEY=... đã có (mọi lần xuất hiện), thêm KEY chưa có vào cuối; GIỮ nguyên comment và thứ tự."""
    out, done = [], set()
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f'{key}="{updates[key]}"')
            done.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in done:
            out.append(f'{key}="{val}"')
    return "\n".join(out) + "\n"
