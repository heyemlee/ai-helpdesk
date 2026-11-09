"""Redaction at log/audit sinks, not only at individual exception producers."""
import os
import re

_secrets: set[str] = set()
_sensitive = re.compile(r"password|secret|api.?key|authorization|cookie|access.?token|refresh.?token|b64|image_url", re.I)


def register_secret(value):
    if isinstance(value, str) and len(value) >= 8:
        _secrets.add(value)


def text(value):
    result = str(value)
    values = _secrets | {v for k, v in os.environ.items() if _sensitive.search(k) and len(v) >= 8}
    for secret in sorted(values, key=len, reverse=True):
        result = result.replace(secret, "[redacted]")
    result = re.sub(r"data:image/[^\s\"']+", "[image omitted]", result, flags=re.I)
    result = re.sub(r"(?i)(bearer\s+)[^\s\"',;]+", r"\1[redacted]", result)
    result = re.sub(r"(?i)((?:password|api[_-]?key|secret|token|cookie|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+", r"\1[redacted]", result)
    result = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[redacted]", result)
    result = re.sub(r"(https?://|postgresql://|redis://)[^/\s@]+:[^/\s@]+@", r"\1[redacted]@", result)
    return result


def data(value):
    if isinstance(value, dict):
        return {k: "[redacted]" if _sensitive.search(str(k)) else data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [data(v) for v in value]
    return text(value) if isinstance(value, str) else value
