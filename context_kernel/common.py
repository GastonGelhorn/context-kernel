"""Shared validation and serialization."""

from datetime import datetime, timezone
import hashlib
import json
import re
import uuid


class KernelError(Exception):
    """A safe, user-visible error without private payloads."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identifier():
    return uuid.uuid4().hex


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def timestamp(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise KernelError("Use an ISO 8601 date or timestamp.") from exc
    if not isinstance(value, datetime):
        raise KernelError("Invalid timestamp.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def key(value, name="key"):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", value):
        raise KernelError(f"Invalid {name}; use letters, numbers, underscores, or hyphens.")
    return value.lower()


def text(value, limit=4096):
    try:
        valid = isinstance(value, str) and value.strip() and len(value.encode()) <= limit
    except UnicodeError:
        valid = False
    if not valid:
        raise KernelError("Text is empty or exceeds its size limit.")
    return value.strip()


def checked_value(value):
    if isinstance(value, dict) and value.get("type") == "quantity":
        validate_quantity(value)
    try:
        encoded = canonical(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise KernelError("Value must be finite JSON data.") from exc
    try:
        size = len(encoded.encode())
    except UnicodeError as exc:
        raise KernelError("Value must contain valid Unicode.") from exc
    if size > 4096:
        raise KernelError("Value exceeds its size limit.")
    return encoded


def validate_quantity(value):
    if (set(value) - {"type", "amount", "unit", "currency", "period"}
            or not {"type", "amount"} <= set(value) or type(value["amount"]) not in {int, float}):
        raise KernelError("A quantity requires a numeric amount and only type, amount, unit, currency, and period fields.")
    for name in ("unit", "period"):
        label = value.get(name)
        if label is not None and (not isinstance(label, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_/-]{0,31}", label)):
            raise KernelError("Quantity unit and period must be short explicit labels or null.")
    currency = value.get("currency")
    if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
        raise KernelError("Quantity currency must be an explicit three-letter uppercase label or null.")


def quantity(value, unit=None, currency=None, period=None):
    result = {"type": "quantity", "amount": value, "unit": unit, "currency": currency, "period": period}
    checked_value(result)
    return result


def reject_secrets(value):
    if re.search(r"(?i)(?:api[_ -]?key|password|secret[_ -]?token)\s*[:=]|-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9]{20,}", value):
        raise KernelError("Possible credentials detected; secret storage is not supported.")
