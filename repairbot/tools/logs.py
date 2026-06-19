"""Log loading and lightweight extraction."""

from __future__ import annotations

import re
from pathlib import Path


ERROR_RE = re.compile(r"\b([A-Za-z_][\w.]*?(?:Error|Exception))\b")
FILE_RE = re.compile(
    r"((?:[A-Za-z]:)?(?:[\\/A-Za-z0-9_.-]+[\\/])*[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|go|java|rs))(?::\d+)?"
)
ENDPOINT_RE = re.compile(r"(?<![A-Za-z0-9_./{}:-])(/[A-Za-z0-9_./{}:-]+)")


def read_logs(logs_path: str | Path, max_chars: int = 40_000) -> str:
    path = Path(logs_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Logs file does not exist: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + "\n\n[... logs truncated by repairbot; keeping head and tail ...]\n\n"
        + text[-half:]
    )


def extract_log_hints(raw_logs: str) -> dict[str, list[str]]:
    files = _unique(FILE_RE.findall(raw_logs), 20)
    errors = _unique(ERROR_RE.findall(raw_logs), 20)
    endpoints = _unique(ENDPOINT_RE.findall(raw_logs), 20)
    return {"files": files, "errors": errors, "endpoints": endpoints}


def summarize_logs_heuristic(raw_logs: str, max_lines: int = 20) -> str:
    interesting: list[str] = []
    for line in raw_logs.splitlines():
        lowered = line.lower()
        if any(token in lowered for token in ("error", "exception", "traceback", "failed")):
            interesting.append(line.strip())
        if len(interesting) >= max_lines:
            break
    if not interesting:
        interesting = [line.strip() for line in raw_logs.splitlines()[:max_lines] if line.strip()]
    return "\n".join(interesting) or "No non-empty log lines were found."


def _unique(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip().replace("\\", "/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result
