"""Nhật ký công việc RIÊNG cho từng instance (mỗi dòng 1 JSON)."""
import json
import logging
import logging.handlers
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


_SENSITIVE_FIELDS = {
    "task_hint",
    "prompt",
    "message",
    "content",
    "arguments",
    "argument",
    "cookie",
    "cookies",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "image",
    "image_data",
    "hint",
    "command",
    "path",
    "file_path",
    "url",
    "secret",
    "password",
}


def _sanitize_activity_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_activity_value(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_FIELDS
        }
    if isinstance(value, list):
        return [_sanitize_activity_value(item) for item in value]
    return value


class ActivityJournal:
    def __init__(self, path, instance_id: str):
        self.path = Path(path)
        self.instance_id = instance_id or "default"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.counter = 0
        self._logger = logging.getLogger(f"DSH-Activity.{self.instance_id}.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = logging.handlers.RotatingFileHandler(
            self.path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)
        self._handler = handler

    def write(self, **fields: Any) -> Dict[str, Any]:
        self.counter += 1
        entry: Dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "instance": self.instance_id,
            "seq": self.counter,
        }
        entry.update(_sanitize_activity_value(fields))
        try:
            self._logger.info(json.dumps(entry, ensure_ascii=False))
            self._handler.flush()
        except Exception:
            pass
        return entry

    def tail(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            size = self.path.stat().st_size
            with open(self.path, "rb") as f:
                f.seek(max(0, size - 256 * 1024))
                raw = f.read().decode("utf-8", errors="replace")
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for line in raw.splitlines()[-limit:]:
            try:
                entry = json.loads(line)
                out.append(_sanitize_activity_value(entry))
            except Exception:
                continue
        return out

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._handler.close()


def summarize_tool_calls(tool_calls) -> List[Dict[str, str]]:
    """Chỉ ghi tên tool; không lưu arguments hoặc giá trị tham số."""
    out = []
    for tc in tool_calls or []:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = fn.get("name", "?")
        out.append({"name": str(name)[:80]})
    return out


def record_failed_tool_trace(
    path: Path,
    *,
    request_id: str,
    model: str,
    reply_text: str,
    reason: str = "invalid_tool_call",
    max_entries: int = 50,
    max_reply_chars: int = 16000,
) -> None:
    """Ghi vòng tối đa 50 raw reply khi parse/repair tool call thất bại để phục vụ error analysis."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "request_id": str(request_id),
            "model": str(model),
            "reason": str(reason),
            "reply_chars": len(reply_text or ""),
            "reply_text": (reply_text or "")[:max_reply_chars],
        }
        existing: List[str] = []
        if target.exists():
            try:
                existing = [
                    line
                    for line in target.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip()
                ]
            except OSError:
                existing = []
        keep = existing[-(max_entries - 1):] if max_entries > 1 else []
        keep.append(json.dumps(entry, ensure_ascii=False))
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=".failure_traces_",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_name = tmp.name
                tmp.write("\n".join(keep) + "\n")
                tmp.flush()
            os.replace(tmp_name, target)
            tmp_name = None
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
    except Exception:
        pass
