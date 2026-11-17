import json
import re
from typing import Any, Optional, Tuple


_TITLE_REQUEST_PREFIX = (
    "Generate the session title from this JSON array of human messages:"
)
_MAX_TITLE_REQUEST_CHARS = 32_000
_MAX_HUMAN_MESSAGES = 100
_MAX_TITLE_CHARS = 80
_WHITESPACE = re.compile(r"\s+")


def extract_local_title_request(
    messages: Any,
) -> Tuple[bool, Optional[str], int]:
    """Recognize DSH's dedicated title request and derive a short local title."""
    if not isinstance(messages, list):
        return False, None, 0

    if any(
        not isinstance(message, dict)
        or message.get("role") not in ("system", "developer", "user")
        for message in messages
    ):
        return False, None, 0

    user_messages = [message for message in messages if message.get("role") == "user"]
    if len(user_messages) != 1:
        return False, None, 0

    content = user_messages[0].get("content")
    if not isinstance(content, str):
        return False, None, 0

    request_text = content.lstrip()
    if not request_text.startswith(_TITLE_REQUEST_PREFIX):
        return False, None, 0

    encoded_messages = request_text[len(_TITLE_REQUEST_PREFIX):].strip()
    request_chars = len(request_text)
    if request_chars > _MAX_TITLE_REQUEST_CHARS:
        return True, None, request_chars

    try:
        human_messages, end = json.JSONDecoder().raw_decode(encoded_messages)
    except (json.JSONDecodeError, RecursionError):
        return True, None, request_chars

    if encoded_messages[end:].strip():
        return True, None, request_chars
    if (
        not isinstance(human_messages, list)
        or not human_messages
        or len(human_messages) > _MAX_HUMAN_MESSAGES
    ):
        return True, None, request_chars

    for message in human_messages:
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("text"), str)
            or (
                "role" in message
                and message["role"] not in ("human", "user")
            )
        ):
            return True, None, request_chars

    for message in human_messages:
        title = _WHITESPACE.sub(" ", message["text"]).strip()
        if title:
            if len(title) > _MAX_TITLE_CHARS:
                shortened = title[:_MAX_TITLE_CHARS].rsplit(" ", 1)[0].strip()
                title = (shortened or title[:_MAX_TITLE_CHARS]).rstrip() + "..."
            return True, title, request_chars

    return True, None, request_chars
