"""CLI quản lý Profile Vault & Auto Cookie Sync cho nhiều tài khoản Google (0 tab mở nền).

Ví dụ sử dụng:
    python scripts/cookie_sync.py login acc1 --lane acc1 --slot 0 --label "Work 1"
    python scripts/cookie_sync.py login acc2 --lane acc2 --slot 0 --label "Work 2"
    python scripts/cookie_sync.py login acc1_b1 --lane acc1 --slot 1 --label "Backup 1"
    python scripts/cookie_sync.py sync acc1
    python scripts/cookie_sync.py sync-all
    python scripts/cookie_sync.py list
    python scripts/cookie_sync.py bind work4 --lane acc2 --slot 2
    python scripts/cookie_sync.py remove work4
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from app.config import API_KEY, BASE_DIR, LANE_ADMIN_TOKEN, PORT
from app.cookie_sync import (
    CookieSyncManager,
    CookieSyncResult,
    ProfileVault,
    find_browser_executable,
    persist_cookie_jar_to_lane_cache,
    persist_cookies_to_lane_env,
)


def _fmt_ts(ts: int) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


async def _try_hot_reload_gateway(
    lane_id: str,
    slot: int,
    psid: str,
    psidts: str,
    label: str = "",
    cookies: Optional[list] = None,
    gateway_url: str = "",
) -> Tuple[bool, str]:
    """Nếu Bridge/Gateway đang chạy trên localhost -> nạp nóng luôn không cần restart."""
    base = (gateway_url or f"http://127.0.0.1:{PORT}").rstrip("/")
    url = f"{base}/v1/lanes/{lane_id}/accounts/{slot}/cookies"
    headers = {
        "Content-Type": "application/json",
        "Origin": base,
    }
    if LANE_ADMIN_TOKEN:
        headers["X-Lane-Token"] = LANE_ADMIN_TOKEN
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload: dict = {"psid": psid, "psidts": psidts, "label": label}
    if cookies:
        payload["cookies"] = cookies
    try:
        async with httpx.AsyncClient(timeout=38.0) as client:
            resp = await client.post(url, headers=headers, content=json.dumps(payload))
        if resp.status_code == 200:
            data = resp.json()
            return True, f"hot-reload OK (auth_ok={data.get('auth_ok')})"
        try:
            err = resp.json()
        except Exception:
            err = resp.text[:160]
        return False, f"HTTP {resp.status_code}: {err}"
    except Exception:
        return False, "bridge offline"


async def _apply_result(
    res: CookieSyncResult,
    lane_id: str,
    slot: Optional[int],
    label: str,
    gateway_url: str = "",
) -> bool:
    if not res.ok:
        print(f"  [LỖI] {res.profile_id}: {res.message} ({res.code})")
        return False

    print(
        f"  [OK]  {res.profile_id}: đã lấy cookie tươi (account={res.account_key}, rotated={res.rotated}, jar={len(res.cookies)})"
    )
    target_lane = (lane_id or "").strip()
    if not target_lane:
        print("        Profile chưa gắn vào luồng nào (chỉ lưu trong Profile Vault).")
        return True

    target_slot = int(slot) if slot is not None else 0
    if res.cookies:
        persist_cookie_jar_to_lane_cache(BASE_DIR, target_lane, res.psid, res.cookies)

    hot_ok, hot_msg = await _try_hot_reload_gateway(
        target_lane, target_slot, res.psid, res.psidts, label, cookies=res.cookies, gateway_url=gateway_url
    )
    if hot_ok:
        print(f"        -> Đã nạp nóng vào luồng '{target_lane}' slot {target_slot}: {hot_msg}")
        return True

    # Fallback ghi trực tiếp vào .env.<lane> nếu bridge đang tắt (hoặc chưa nạp nóng được)
    env_ok, env_name, env_err = persist_cookies_to_lane_env(
        BASE_DIR, target_lane, target_slot, res.psid, res.psidts, label
    )
    if env_ok:
        print(
            f"        -> Đã ghi vào {env_name} (slot {target_slot}; hot-reload bỏ qua: {hot_msg})"
        )
        return True
    print(f"        [LỖI] Không ghi được {env_name}: {env_err}")
    return False


async def cmd_list(mgr: CookieSyncManager) -> int:
    exe, name = find_browser_executable(mgr.browser_preference)
    print(f"Trình duyệt dùng để sync: {name or 'KHÔNG TÌM THẤY'} ({exe or '-'})")
    print(f"Thư mục Profile Vault   : {mgr.vault.profiles_dir}")
    profiles = mgr.vault.list_profiles()
    if not profiles:
        print("\nChưa có profile nào trong kho. Tạo mới bằng:")
        print('  python scripts/cookie_sync.py login acc1 --lane acc1 --slot 0 --label "Work 1"')
        return 0
    print(
        f"\n{'PROFILE':<18} {'NHÃN':<18} {'LUỒNG':<10} {'SLOT':<5} {'TRẠNG THÁI':<15} {'SYNC GẦN NHẤT'}"
    )
    print("-" * 90)
    for p in profiles:
        slot_s = "-" if p.get("slot") is None else str(p["slot"])
        st = p.get("last_status") or ("ready" if p.get("has_profile_data") else "new")
        print(
            f"{p['profile_id']:<18} {(p.get('label') or ''):<18} {(p.get('lane_id') or '-'):<10} "
            f"{slot_s:<5} {st:<15} {_fmt_ts(int(p.get('last_synced_at') or 0))}"
        )
    return 0


async def cmd_login(mgr: CookieSyncManager, args: argparse.Namespace) -> int:
    print(
        f"[1/2] Đang mở trình duyệt thật cho profile '{args.profile_id}'..."
    )
    print("      Hãy đăng nhập tài khoản Google + 2FA, chờ vào màn hình Gemini, rồi ĐÓNG cửa sổ trình duyệt.")
    done_fut: asyncio.Future[CookieSyncResult] = asyncio.get_running_loop().create_future()

    async def _on_done(res: CookieSyncResult, _meta: dict) -> None:
        if not done_fut.done():
            done_fut.set_result(res)

    meta, _proc = await mgr.start_interactive_login(
        args.profile_id,
        label=args.label,
        lane_id=args.lane,
        slot=args.slot,
        on_complete=_on_done,
    )
    res = await done_fut
    print(f"[2/2] Đã đóng cửa sổ và đồng bộ ngầm xong cho '{args.profile_id}'.")
    lane = args.lane if args.lane is not None else (meta.get("lane_id") or "")
    slot = args.slot if args.slot is not None else meta.get("slot")
    label = args.label or meta.get("label") or ""
    ok = await _apply_result(res, lane, slot, label, gateway_url=args.gateway)
    return 0 if ok else 1


async def cmd_sync(mgr: CookieSyncManager, args: argparse.Namespace) -> int:
    meta = mgr.vault.get_profile(args.profile_id)
    if not meta:
        print(f"[LỖI] Không thấy profile '{args.profile_id}'. Hãy chạy `login {args.profile_id}` trước.")
        return 1
    res = await mgr.sync_profile(args.profile_id)
    lane = args.lane if args.lane is not None else (meta.get("lane_id") or "")
    slot = args.slot if args.slot is not None else meta.get("slot")
    label = meta.get("label") or ""
    ok = await _apply_result(res, lane, slot, label, gateway_url=args.gateway)
    return 0 if ok else 1


async def cmd_sync_all(mgr: CookieSyncManager, args: argparse.Namespace) -> int:
    profiles = [
        p
        for p in mgr.vault.list_profiles()
        if p.get("has_profile_data")
        and (not args.lane or (p.get("lane_id") or "") == args.lane)
    ]
    if not profiles:
        print("Không có profile nào đã đăng nhập để sync.")
        return 0
    print(f"Đang sync ngầm tuần tự {len(profiles)} profile (0 cửa sổ mở)...")
    failed = 0
    for meta in profiles:
        res = await mgr.sync_profile(meta["profile_id"])
        ok = await _apply_result(
            res,
            meta.get("lane_id") or "",
            meta.get("slot"),
            meta.get("label") or "",
            gateway_url=args.gateway,
        )
        if not ok:
            failed += 1
    print(f"\nHoàn tất: {len(profiles) - failed}/{len(profiles)} thành công.")
    return 0 if failed == 0 else 1


async def cmd_bind(mgr: CookieSyncManager, args: argparse.Namespace) -> int:
    meta = mgr.vault.upsert_profile(
        args.profile_id,
        label=args.label,
        lane_id=args.lane,
        slot=args.slot,
        clear_binding=args.unbind,
    )
    print(
        f"Đã cập nhật profile '{meta['profile_id']}': lane={meta.get('lane_id') or '-'}, slot={meta.get('slot')}"
    )
    return 0


async def cmd_remove(mgr: CookieSyncManager, args: argparse.Namespace) -> int:
    deleted = mgr.vault.delete_profile(args.profile_id, remove_dir=not args.keep_dir)
    if deleted:
        print(f"Đã xóa profile '{args.profile_id}' khỏi Profile Vault.")
        return 0
    print(f"Không tìm thấy profile '{args.profile_id}'.")
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="DSH Gemini Bridge — Profile Vault & Auto Cookie Sync CLI"
    )
    p.add_argument(
        "--gateway",
        default=os.getenv("BRIDGE_GATEWAY_URL", ""),
        help="URL Gateway/Worker để nạp nóng (mặc định http://127.0.0.1:<PORT>)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Liệt kê toàn bộ profile trong kho")

    p_login = sub.add_parser("login", help="Mở trình duyệt thật đăng nhập 1 lần rồi tự đồng bộ")
    p_login.add_argument("profile_id", help="Tên định danh profile (vd: acc1, work4, backup2)")
    p_login.add_argument("--lane", default=None, help="Gắn vào luồng (vd: acc1, acc2, acc3)")
    p_login.add_argument("--slot", type=int, default=None, help="Ô tài khoản trong luồng (0..4)")
    p_login.add_argument("--label", default=None, help="Nhãn gợi nhớ")

    p_sync = sub.add_parser("sync", help="Sync ngầm 1 profile (không mở cửa sổ)")
    p_sync.add_argument("profile_id")
    p_sync.add_argument("--lane", default=None)
    p_sync.add_argument("--slot", type=int, default=None)

    p_all = sub.add_parser("sync-all", help="Sync ngầm tuần tự toàn bộ profile đã đăng nhập")
    p_all.add_argument("--lane", default="", help="Chỉ sync các profile thuộc 1 luồng cụ thể")

    p_bind = sub.add_parser("bind", help="Gắn hoặc đổi luồng/slot cho profile có sẵn")
    p_bind.add_argument("profile_id")
    p_bind.add_argument("--lane", default=None)
    p_bind.add_argument("--slot", type=int, default=None)
    p_bind.add_argument("--label", default=None)
    p_bind.add_argument("--unbind", action="store_true", help="Gỡ gắn khỏi luồng")

    p_rm = sub.add_parser("remove", help="Xóa profile khỏi kho")
    p_rm.add_argument("profile_id")
    p_rm.add_argument("--keep-dir", action="store_true", help="Giữ lại thư mục profile trên đĩa")

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    mgr = CookieSyncManager(vault=ProfileVault())

    if args.cmd == "list":
        return asyncio.run(cmd_list(mgr))
    if args.cmd == "login":
        return asyncio.run(cmd_login(mgr, args))
    if args.cmd == "sync":
        return asyncio.run(cmd_sync(mgr, args))
    if args.cmd == "sync-all":
        return asyncio.run(cmd_sync_all(mgr, args))
    if args.cmd == "bind":
        return asyncio.run(cmd_bind(mgr, args))
    if args.cmd == "remove":
        return asyncio.run(cmd_remove(mgr, args))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
