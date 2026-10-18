"""Keep secrets and private addresses out of logs."""

from __future__ import annotations

SECRET_KEYS = {
    "authorization",
    "token",
    "access_token",
    "api_token",
    "password",
    "client_secret",
    "secret",
    "claude_key",
    "claude_code_oauth_token",
}
EMAIL_KEYS = {"email", "manager_email", "to", "from", "smtp_user"}


def redact_email(value: str) -> str:
    if "@" not in value:
        return "***"
    local, _, domain = value.partition("@")
    prefix = local[:1] if local else ""
    return f"{prefix}***@{domain}"


def redact(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in SECRET_KEYS:
                cleaned[key] = "***"
            elif lowered in EMAIL_KEYS and isinstance(item, str):
                cleaned[key] = redact_email(item)
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
