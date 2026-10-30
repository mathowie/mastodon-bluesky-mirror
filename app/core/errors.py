"""Useful, bounded error descriptions with credential redaction."""
import re
from ..config import settings


def describe_error(exc):
    response = getattr(exc, "response", None)
    content = getattr(response, "content", None)
    code = content.get("error") if isinstance(content, dict) else getattr(content, "error", None)
    message = content.get("message") if isinstance(content, dict) else getattr(content, "message", None)
    status = getattr(response, "status_code", None)
    details = [f"HTTP {status}"] if status is not None else []
    if code:
        details.append(str(code))
    text = type(exc).__name__ + (" (" + ", ".join(details) + ")" if details else "")
    if message:
        text += ": " + str(message)
    elif response is None and str(exc):
        text += ": " + str(exc)
    for name in ("MASTODON_ACCESS_TOKEN", "BLUESKY_APP_PASSWORD", "BLUESKY_PASSWORD", "DASHBOARD_PASSWORD"):
        secret = getattr(settings, name, None)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED JWT]", text)
    text = re.sub(r"(?i)(\b(?:access_?token|refresh_?token|authorization|password|secret|token)\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)", r"\1[REDACTED]", text)
    return " ".join(text.split())[:700]
