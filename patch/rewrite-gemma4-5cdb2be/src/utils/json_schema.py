from __future__ import annotations

import json
import re
from typing import Any


def repair_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from a plain or fenced model response.

    This intentionally performs only conservative cleanup. Invalid model
    output returns an empty object so the localization layer can keep its
    retrieval result as a safe fallback.
    """

    value = (text or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", value)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}
