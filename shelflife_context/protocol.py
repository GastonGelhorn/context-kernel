"""Strict JSON at process and model boundaries."""

import json

from .common import KernelError


def parse_json(raw):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise KernelError("Duplicate JSON fields are not allowed.")
            result[name] = value
        return result

    def constant(_value):
        raise KernelError("Non-finite JSON values are not allowed.")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise KernelError("Invalid JSON input.") from exc


def read_event(stream, limit=65536):
    raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise KernelError("Hook input exceeds the byte limit.")
    event = parse_json(raw)
    if not isinstance(event, dict):
        raise KernelError("Hook input must be an object.")
    return event
