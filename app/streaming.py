"""Incremental text splitter that withholds ``<tool_call>`` blocks from content.

Gemini returns tool calls as raw XML inside the same text stream. When streaming
to an OpenAI-compatible client we must emit the prose but withhold the XML so it
is later parsed into structured ``tool_calls`` instead of leaking as content.
"""
from __future__ import annotations


class ToolCallAwareStreamer:
    """Splits a stream of text deltas into content pieces safe to emit, while
    buffering the full text for final tool-call parsing."""

    OPEN = "<tool_call>"
    CLOSE = "</tool_call>"

    def __init__(self) -> None:
        self._buf = ""               # held text not yet emitted as content
        self._full: list[str] = []   # every fed delta (authoritative full text)
        self._in_tool = False
        self._in_string = False
        self._escaped = False

    def feed(self, delta: str) -> list[str]:
        """Feed an incremental delta; returns content pieces to emit now."""
        self._full.append(delta)
        self._buf += delta
        out: list[str] = []
        while self._buf:
            if self._in_tool:
                i = 0
                buf = self._buf
                n = len(buf)
                closed_at = -1
                hold_from = -1
                while i < n:
                    ch = buf[i]
                    if self._in_string:
                        if self._escaped:
                            self._escaped = False
                        elif ch == "\\":
                            self._escaped = True
                        elif ch == '"':
                            self._in_string = False
                        i += 1
                    else:
                        if ch == "<" and buf.startswith(self.CLOSE, i):
                            closed_at = i
                            break
                        if ch == "<" and (n - i) < len(self.CLOSE) and self.CLOSE.startswith(buf[i:]):
                            hold_from = i
                            break
                        if ch == '"':
                            self._in_string = True
                            self._escaped = False
                        i += 1

                if closed_at != -1:
                    self._buf = buf[closed_at + len(self.CLOSE):]
                    self._in_tool = False
                    self._in_string = False
                    self._escaped = False
                    continue
                if hold_from != -1:
                    self._buf = buf[hold_from:]
                else:
                    self._buf = ""
                break
            else:
                idx = self._buf.find(self.OPEN)
                if idx == -1:
                    hold = self._partial_suffix(self._buf, self.OPEN)
                    if hold:
                        out.append(self._buf[: len(self._buf) - hold])
                        self._buf = self._buf[-hold:]
                    else:
                        out.append(self._buf)
                        self._buf = ""
                    break
                out.append(self._buf[:idx])
                self._buf = self._buf[idx + len(self.OPEN):]
                self._in_tool = True
                self._in_string = False
                self._escaped = False
        return [p for p in out if p]

    def flush(self) -> list[str]:
        """Emit any remaining content once the stream ends."""
        if self._in_tool:
            self._buf = ""
            return []
        out = [self._buf]
        self._buf = ""
        return [p for p in out if p]

    @property
    def full_text(self) -> str:
        return "".join(self._full)

    @staticmethod
    def _partial_suffix(s: str, tag: str) -> int:
        """Longest k such that ``s[-k:] == tag[:k]``."""
        k = min(len(s), len(tag))
        while k > 0:
            if s[-k:] == tag[:k]:
                return k
            k -= 1
        return 0
