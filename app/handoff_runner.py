"""Điều phối bàn giao công việc (Quota Handoff) qua model Flash + archive + FactLedger."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys

from app.config import (
    HANDOFF_ENABLE,
    HANDOFF_FLASH_TIMEOUT,
    HANDOFF_MAX_CHARS,
    HANDOFF_RECENT_TURNS,
    HANDOFF_SOURCE_MAX_CHARS,
    QUOTA_COOLDOWN_HOURS,
    REPLAY_MAX_CHARS,
    logger,
)
from app.handoff import (
    SECTION_ORDER,
    bound_sections,
    build_handoff_message,
    parse_sections,
)


def _gs():
    return sys.modules["app.gemini_session"]


class HandoffRunnerMixin:
    """Mixin sinh bản bàn giao khi hết quota và gắn vào lượt gửi kế tiếp."""

    HANDOFF_PROMPT_TEMPLATE = (
        "You are writing a HANDOFF NOTE so another assistant can continue this coding task.\n"
        "Reply with EXACTLY these seven sections. Each header goes alone on its own line, "
        "in square brackets, exactly as written. Write nothing before the first header or after the last section.\n"
        "[GOAL]\n[DONE]\n[DOING]\n[NEXT]\n[DECISIONS]\n[FILES]\n[LAST_ERROR]\n"
        "Limits: GOAL max 3 lines; DONE max 8 bullets (oldest first, newest last); DOING max 3 lines; "
        "NEXT max 5 bullets; DECISIONS max 5 bullets (important decisions and why); "
        "FILES max 15 lines, file paths only, never file contents; LAST_ERROR max 2 lines, leave empty if none.\n"
        "Rules: be terse, no code blocks, no markdown headings, write in the language the user used.\n"
        "CONVERSATION (most recent last):\n{source}\n"
    )

    def _recent_turns_for_handoff(self) -> list:
        convo = [line for line in self.transcript if line.startswith(("user:", "assistant:"))]
        return convo[-(2 * HANDOFF_RECENT_TURNS) :]

    def _apply_pending_handoff(self, prompt: str) -> str:
        """CHỈ DỰNG CHUỖI. KHÔNG được xóa self._pending_handoff ở đây."""
        if not self._pending_handoff:
            return prompt
        bounded, archive = self._pending_handoff
        msg = build_handoff_message(
            bounded, self._recent_turns_for_handoff(), archive, REPLAY_MAX_CHARS
        )
        logger.info(f"[QUOTA] Tin bàn giao gửi cho tài khoản mới: {len(msg)} ký tự.")
        return msg + "\n" + prompt

    def _pick_flash_model(self):
        for name in self.available_models or []:
            if "flash" in str(name).lower():
                return name
        return None

    async def _ask_flash_for_handoff(self) -> str:
        flash = self._pick_flash_model()
        if not flash:
            logger.warning("[QUOTA] Không thấy model Flash; dùng sổ sự thật.")
            return ""
        source = "\n".join(self.transcript)[-HANDOFF_SOURCE_MAX_CHARS:]
        prompt = self.HANDOFF_PROMPT_TEMPLATE.format(source=source)
        try:
            await self.client.init(
                timeout=60, auto_close=False, close_delay=300, auto_refresh=True
            )
            kwargs = {"model": str(flash)} if flash and flash != "unspecified" else {}
            chat = self.client.start_chat(**kwargs)
            out = await asyncio.wait_for(
                chat.send_message(prompt), timeout=HANDOFF_FLASH_TIMEOUT
            )
            return (getattr(out, "text", "") or "").strip()
        except Exception as exc:
            logger.warning(
                f"[QUOTA] Flash không viết được bản bàn giao ({exc}); dùng sổ sự thật."
            )
            return ""

    def _write_handoff_archive(self, bounded: str, flash_text: str) -> str:
        try:
            handoff_dir = Path(_gs().INSTANCE_PATHS["handoff_dir"])
            handoff_dir.mkdir(parents=True, exist_ok=True)
            now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive_file = handoff_dir / f"handoff_{now_str}.md"

            transcript_str = "\n".join(self.transcript[-60:])
            content = (
                f"# HANDOFF ARCHIVE ({now_str})\n\n"
                f"## Bounded Handoff Note\n{bounded}\n\n"
                f"## Flash Raw Output\n{flash_text or '(none)'}\n\n"
                f"## Fact Ledger\n```json\n"
                f"{json.dumps(self.ledger.data, ensure_ascii=False, indent=2)}\n```\n\n"
                f"## Recent Transcript\n```\n{transcript_str}\n```\n"
            )
            archive_file.write_text(content, encoding="utf-8")
            (handoff_dir / "latest.md").write_text(bounded, encoding="utf-8")

            files = sorted(handoff_dir.glob("handoff_*.md"))
            while len(files) > 20:
                oldest = files.pop(0)
                try:
                    oldest.unlink()
                except Exception:
                    pass

            return str(archive_file.resolve())
        except Exception as exc:
            logger.warning(f"[QUOTA] Không ghi được handoff archive: {exc}")
            return "(không ghi được archive)"

    async def _handle_quota_exhausted(self, exc) -> bool:
        if self.state == "off":
            return False
        old = (
            self.chain[self.active_index]["label"]
            if (self.chain and 0 <= self.active_index < len(self.chain))
            else "chinh"
        )
        logger.warning(f"[QUOTA] Tài khoản '{old}' hết quota: {exc}")
        self.mark_current_exhausted("quota", QUOTA_COOLDOWN_HOURS)

        fallback = self.ledger.to_sections()
        try:
            flash_text = (
                await self._ask_flash_for_handoff() if HANDOFF_ENABLE else ""
            )
        except Exception as err:
            logger.warning(f"[QUOTA] Flash handoff lỗi ({err}); dùng sổ sự thật.")
            flash_text = ""
        parsed = parse_sections(flash_text) if flash_text else {}
        sections = {
            name: parsed.get(name) or fallback.get(name, []) for name in SECTION_ORDER
        }
        bounded = bound_sections(sections, HANDOFF_MAX_CHARS)
        archive = self._write_handoff_archive(bounded, flash_text)
        self._pending_handoff = (bounded, archive)

        switched = await self._try_failover_to_backup()
        if switched:
            self._emit(
                event="account_switch",
                reason="quota",
                from_label=old,
                to_label=self.chain[self.active_index]["label"],
                handoff_chars=len(bounded),
                flash_used=bool(flash_text),
            )
        else:
            self._pending_handoff = None
        return switched
