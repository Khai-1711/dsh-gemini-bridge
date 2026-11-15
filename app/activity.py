"""Nhật ký công việc RIÊNG cho từng instance (mỗi dòng 1 JSON)."""
import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


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
        entry.update(fields)
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
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._handler.close()


def summarize_tool_calls(tool_calls) -> List[Dict[str, str]]:
    """Chỉ ghi TÊN tool + gợi ý ngắn (lệnh/đường dẫn). KHÔNG ghi nội dung file."""
    out = []
    for tc in tool_calls or []:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = fn.get("name", "?")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        hint = ""
        for key in ("command", "file_path", "path", "pattern", "url"):
            if isinstance(args, dict) and key in args:
                hint = str(args[key])
                break
        out.append({"name": name, "hint": hint[:120]})
    return out
