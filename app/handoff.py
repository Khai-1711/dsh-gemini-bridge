"""Sổ sự thật (ledger) + bản bàn giao có giới hạn cứng khi chuyển tài khoản.

Module THUẦN TÚY: không import app.config. Ledger chỉ lưu "con trỏ" (tên file, 80 ký tự đầu
của lệnh, thành công/lỗi), tuyệt đối không lưu nội dung file và không lưu cookie.
"""
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List

SECTION_ORDER = ["GOAL", "DONE", "DOING", "NEXT", "DECISIONS", "FILES", "LAST_ERROR"]
# (số dòng tối đa, số ký tự tối đa mỗi dòng)
SECTION_LIMITS = {
    "GOAL": (3, 300),
    "DONE": (8, 160),
    "DOING": (3, 200),
    "NEXT": (5, 160),
    "DECISIONS": (5, 200),
    "FILES": (15, 200),
    "LAST_ERROR": (3, 300),
}
_HEADER_RE = re.compile(
    r"^\s*\[(GOAL|DONE|DOING|NEXT|DECISIONS|FILES|LAST_ERROR)\]\s*$"
)


def parse_sections(text: str) -> Dict[str, List[str]]:
    """Tách văn bản Flash thành các mục. Chỉ nhận tiêu đề đứng một mình trên một dòng."""
    out: Dict[str, List[str]] = {}
    cur = None
    for raw in (text or "").splitlines():
        m = _HEADER_RE.match(raw)
        if m:
            cur = m.group(1)
            out.setdefault(cur, [])
            continue
        if cur is None:
            continue
        line = raw.strip().lstrip("-*• ").strip()
        if line:
            out[cur].append(line)
    return out


def _clip(line: str, n: int) -> str:
    line = " ".join(line.split())
    return line if len(line) <= n else line[: n - 1] + "…"


def _render(sections: Dict[str, List[str]]) -> str:
    parts = []
    for name in SECTION_ORDER:
        lines = sections.get(name) or []
        parts.append(f"[{name}]")
        if name != "GOAL":
            parts.extend(f"- {l}" for l in lines)
        else:
            parts.extend(lines)
    return "\n".join(parts)


def bound_sections(sections: Dict[str, List[str]], max_chars: int) -> str:
    """Cắt theo giới hạn dòng/ký tự từng mục, rồi nếu vẫn dài thì cắt tiếp theo thứ tự cố định:
    bỏ [DONE] cũ nhất (thay bằng 1 dòng đếm) -> bỏ [DECISIONS] cũ nhất -> bớt [FILES] cuối
    -> rút [NEXT]. Luôn giữ GOAL, NEXT, LAST_ERROR, FILES. Kết quả LUÔN <= max_chars."""
    sec: Dict[str, List[str]] = {}
    for name in SECTION_ORDER:
        max_lines, max_len = SECTION_LIMITS[name]
        lines = [
            _clip(l, max_len)
            for l in (sections.get(name) or [])
            if l and l.strip()
        ]
        if name in ("DONE", "DECISIONS", "FILES"):
            lines = lines[-max_lines:] if name != "FILES" else lines[:max_lines]
        else:
            lines = lines[:max_lines]
        sec[name] = lines
    dropped = 0
    # DONE đầu vào có thể đã dài hơn 8: ghi số bị bỏ do giới hạn dòng
    extra = len([l for l in (sections.get("DONE") or []) if l and l.strip()]) - len(
        sec["DONE"]
    )
    dropped += max(extra, 0)

    def render() -> str:
        s = dict(sec)
        if dropped:
            s["DONE"] = [f"(+{dropped} việc cũ hơn, xem archive)"] + sec["DONE"]
        return _render(s)

    text = render()
    for name in ("DONE", "DECISIONS"):
        while len(text) > max_chars and sec[name]:
            sec[name].pop(0)
            if name == "DONE":
                dropped += 1
            text = render()
    while len(text) > max_chars and len(sec["FILES"]) > 3:
        sec["FILES"].pop()
        text = render()
    while len(text) > max_chars and len(sec["NEXT"]) > 1:
        sec["NEXT"].pop()
        text = render()
    if len(text) > max_chars:  # cắt ngặt cuối cùng: rút ngắn từng dòng còn lại
        for name in SECTION_ORDER:
            sec[name] = [_clip(l, 60) for l in sec[name]]
        text = render()
    if len(text) > max_chars:
        text = text[: max(max_chars - 1, 0)] + "…"
    return text


def build_handoff_message(
    bounded: str, recent_turns: List[str], archive_path: str, max_chars: int
) -> str:
    """Tin mở đầu cho chat mới. LUÔN <= max_chars (chưa tính tool header)."""
    head = "=== HANDOFF (tiếp tục công việc từ tài khoản trước) ===\n"
    rules = (
        "\n[RULES]\n- Đọc lại file trước khi sửa, đừng giả định nội dung.\n"
        f"- Chi tiết đầy đủ nằm ở: {archive_path} (chỉ dùng tool Read khi thật sự cần).\n"
    )
    tail = "=== END HANDOFF ===\n"
    fixed = len(head) + len(rules) + len(tail) + len("\n[RECENT]\n")
    budget = max(max_chars - fixed, 0)
    body = bounded if len(bounded) <= budget else bounded[: max(budget - 1, 0)] + "…"
    left = budget - len(body)
    recent = ""
    if recent_turns and left > 80:
        lines, used = [], 0
        for t in reversed(recent_turns):  # ưu tiên lượt mới nhất
            t = " ".join(t.split())[:600]
            if used + len(t) + 1 > left:
                break
            lines.insert(0, t)
            used += len(t) + 1
        recent = "\n[RECENT]\n" + "\n".join(lines) if lines else ""
    return (head + body + recent + rules + tail)[:max_chars] if max_chars else ""


class FactLedger:
    """Sổ sự thật tự dựng từ các tool call; ghi ledger.json trong thư mục handoff của instance."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.path = self.dir / "ledger.json"
        self.data = {
            "goal": "",
            "files": [],
            "commands": [],
            "last_error": "",
            "last_reply": "",
            "pending": {},
        }
        try:
            old = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                self.data.update(old)
        except Exception:
            pass

    def _save(self) -> None:
        tmp_name = None
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.dir,
                prefix=".ledger_",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_name = tmp.name
                tmp.write(json.dumps(self.data, ensure_ascii=False, indent=2))
                tmp.flush()
            os.replace(tmp_name, self.path)
            tmp_name = None
        except Exception as exc:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            logging.getLogger("dsh_bridge").warning(
                "Khong luu duoc FactLedger (%s): %s", type(exc).__name__, exc
            )

    def note_user_goal(self, text) -> None:
        try:
            self.data["goal"] = _clip(str(text or ""), 300)
            self._save()
        except Exception:
            pass

    def note_assistant(self, text) -> None:
        try:
            self.data["last_reply"] = _clip(str(text or ""), 200)
            self._save()
        except Exception:
            pass

    def note_tool_calls(self, tool_calls) -> None:
        """Chỉ lưu tên tool + file_path/path + 80 ký tự đầu của lệnh. KHÔNG lưu 'content'."""
        try:
            for tc in tool_calls or []:
                fn = tc.get("function", tc) if isinstance(tc, dict) else {}
                name = fn.get("name", "?")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                path = args.get("file_path") or args.get("path")
                if path:
                    p = str(path)
                    if p in self.data["files"]:
                        self.data["files"].remove(p)
                    self.data["files"].append(p)
                    self.data["files"] = self.data["files"][-15:]
                cmd = args.get("command")
                call_id = tc.get("id") if isinstance(tc, dict) else None
                entry = {
                    "tool": name,
                    "cmd": _clip(str(cmd), 80) if cmd else "",
                    "ok": None,
                }
                if cmd or name:
                    self.data["commands"].append(entry)
                    self.data["commands"] = self.data["commands"][-30:]
                if call_id:
                    self.data["pending"][str(call_id)] = len(self.data["commands"]) - 1
            self._save()
        except Exception:
            pass

    def note_tool_result(self, tool_call_id, content) -> None:
        """Đánh dấu thành công/lỗi theo từ khóa; chỉ giữ 300 ký tự đầu của lỗi."""
        try:
            text = str(content or "")
            bad = bool(
                re.search(
                    r"\b(error|exception|traceback|failed|lỗi)\b",
                    text[:2000],
                    re.I,
                )
            )
            idx = self.data["pending"].pop(str(tool_call_id), None)
            if idx is not None and 0 <= idx < len(self.data["commands"]):
                self.data["commands"][idx]["ok"] = not bad
            if bad:
                self.data["last_error"] = _clip(text, 300)
            self._save()
        except Exception:
            pass

    def to_sections(self) -> Dict[str, List[str]]:
        d = self.data
        done = [
            f"{c['tool']}: {c['cmd']}".rstrip(": ")
            for c in d["commands"]
            if c.get("ok")
        ]
        todo = [
            f"{c['tool']}: {c['cmd']}".rstrip(": ")
            for c in d["commands"]
            if c.get("ok") is False
        ]
        return {
            "GOAL": [d["goal"]] if d["goal"] else [],
            "DONE": done,
            "DOING": [d["last_reply"]] if d["last_reply"] else [],
            "NEXT": (
                ["Tiếp tục mục tiêu trên; kiểm tra lại các việc lỗi: " + todo[-1]]
                if todo
                else ["Tiếp tục mục tiêu trên từ lượt đang dở."]
            ),
            "DECISIONS": [],
            "FILES": list(d["files"]),
            "LAST_ERROR": [d["last_error"]] if d["last_error"] else [],
        }
