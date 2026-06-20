"""Small redaction helpers for user-visible artifacts."""

from __future__ import annotations

import re


def redact_sensitive_text(text: str | None) -> str:
    if not text:
        return ""
    redacted = re.sub(r"sk-[A-Za-z0-9_-]{16,}", "sk-<redacted>", text)
    redacted = re.sub(r"github_pat_[A-Za-z0-9_]+", "github_pat_<redacted>", redacted)
    redacted = re.sub(r"ghp_[A-Za-z0-9_]+", "ghp_<redacted>", redacted)
    redacted = re.sub(r"AIza[A-Za-z0-9_-]{20,}", "AIza<redacted>", redacted)
    redacted = re.sub(
        r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@",
        r"\1<redacted>:<redacted>@",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)\b\s*[:=]\s*[^\s\"',;)]+",
        lambda match: f"{match.group(1)}=<redacted>",
        redacted,
    )
    return re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password|credential)\b\s*[:=]\s*\S+",
        lambda match: f"{match.group(1)}=<redacted>",
        redacted,
    )
