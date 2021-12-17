"""Explicit JSON and HTTP contracts for the local service."""
import json

def strict_json(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("nonfinite JSON number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
