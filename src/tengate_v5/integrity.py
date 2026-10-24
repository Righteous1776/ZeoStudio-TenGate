from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}"),
)


def secret_findings(value: Any) -> tuple[str, ...]:
    text = canonical_json(value)
    return tuple(pattern.pattern for pattern in _SECRET_PATTERNS if pattern.search(text))

