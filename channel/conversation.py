import re
import secrets
from datetime import datetime
from typing import Optional

CONVERSATION_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")


def new_conversation_id() -> str:
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{now}-{secrets.token_hex(2)}"


def conversation_id_from_session_id(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    parts = str(session_id).split(":")
    if len(parts) < 3:
        return None
    candidate = parts[-1]
    if CONVERSATION_ID_PATTERN.match(candidate):
        return candidate
    return None
