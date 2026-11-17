"""Chuỗi tài khoản + trạng thái "nghỉ" (hết quota / lỗi đăng nhập) cho từng instance.

Module THUẦN TÚY: không import app.config, không gọi mạng, không ghi cookie ra đĩa.
Chỉ ghi hash 12 ký tự của PSID để nhận diện tài khoản.
"""
import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional


def account_key(psid: str) -> str:
    """Hash rút gọn 12 ký tự của PSID (không phải cookie, không đảo ngược được)."""
    return hashlib.sha256((psid or "").encode("utf-8")).hexdigest()[:12]


def build_account_chain(env) -> List[Dict[str, str]]:
    """Dựng chuỗi tài khoản từ biến môi trường: chính + BACKUP_1..BACKUP_4.

    Bỏ qua tài khoản thiếu PSID hoặc trùng PSID với tài khoản đứng trước.
    Mỗi phần tử: {"label", "psid", "psidts", "key"}.
    """
    chain: List[Dict[str, str]] = []
    seen = set()

    def add(label: str, psid: str, psidts: str) -> None:
        psid = (psid or "").strip()
        if not psid or psid in seen:
            return
        seen.add(psid)
        chain.append(
            {
                "label": label,
                "psid": psid,
                "psidts": (psidts or "").strip(),
                "key": account_key(psid),
            }
        )

    add(
        (env.get("INSTANCE_LABEL") or "chinh").strip() or "chinh",
        env.get("SECURE_1PSID", ""),
        env.get("SECURE_1PSIDTS", ""),
    )
    for n in range(1, 5):
        label = (env.get(f"BACKUP_{n}_LABEL") or f"backup{n}").strip()
        add(
            label,
            env.get(f"BACKUP_{n}PSID", ""),
            env.get(f"BACKUP_{n}PSIDTS", ""),
        )
    return chain


def load_state(path) -> Dict[str, dict]:
    """Đọc file trạng thái; lỗi/không có file -> {}."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(path, state: Dict[str, dict]) -> None:
    """Ghi atomic (ghi file tạm rồi thay thế). Lỗi ghi bị nuốt."""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for attempt in range(3):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                if attempt < 2:
                    import time
                    time.sleep(0.02)
                else:
                    raise
    except Exception:
        pass
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def mark_exhausted(
    state: Dict[str, dict], key: str, until_ts: float, reason: str, now: float
) -> None:
    """Đánh dấu tài khoản `key` nghỉ tới `until_ts`. reason: 'quota' hoặc 'auth'."""
    state[key] = {
        "until": float(until_ts),
        "reason": reason,
        "marked_at": float(now),
    }


def is_available(state: Dict[str, dict], key: str, now: float) -> bool:
    entry = state.get(key)
    if not entry:
        return True
    try:
        return now >= float(entry.get("until", 0))
    except Exception:
        return True


def first_available_index(
    chain: List[dict], state: Dict[str, dict], now: float
) -> Optional[int]:
    """Chỉ số tài khoản đầu tiên dùng được, hoặc None nếu tất cả đang nghỉ."""
    for i, acc in enumerate(chain):
        if is_available(state, acc["key"], now):
            return i
    return None


def next_available_index(
    chain: List[dict], state: Dict[str, dict], current_index: int, now: float
) -> Optional[int]:
    """Tài khoản kế tiếp (đi vòng tròn) dùng được. KHÔNG bao giờ trả về current_index."""
    n = len(chain)
    for step in range(1, n):
        i = (current_index + step) % n
        if is_available(state, chain[i]["key"], now):
            return i
    return None


def write_chain_file(
    path, instance: str, port, keys: List[str], now: float
) -> None:
    """Ghi danh sách hash tài khoản của instance này (chỉ hash 12 ký tự, KHÔNG cookie)."""
    save_state(
        path,
        {
            "instance": instance or "default",
            "port": port,
            "keys": list(keys),
            "written": float(now),
        },
    )


def find_chain_overlap(
    base_dir, own_chain_file, own_keys: List[str]
) -> List[dict]:
    """Tìm tài khoản của mình trùng với chuỗi của instance KHÁC.
    Trả về [{"key": ..., "other_instance": ...}]. Chỉ bắt được trường hợp dán CÙNG một cookie."""
    out: List[dict] = []
    own = Path(own_chain_file).resolve()
    mine = set(own_keys)
    try:
        for f in Path(base_dir).glob(".bridge_chain*.json"):
            if f.resolve() == own:
                continue
            data = load_state(f)
            for k in data.get("keys", []):
                if k in mine:
                    out.append(
                        {"key": k, "other_instance": data.get("instance", "?")}
                    )
    except Exception:
        pass
    return out
