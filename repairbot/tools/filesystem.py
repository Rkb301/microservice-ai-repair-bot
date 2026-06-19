"""Safe filesystem helpers for repository inspection and patching."""

from __future__ import annotations

from pathlib import Path

from .safety import assert_write_allowed, resolve_inside_repo, resolve_repo_path


TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".go",
    ".html",
    ".java",
    ".js",
    ".json",
    ".md",
    ".py",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


def list_files(repo_path: str | Path, pattern: str = "**/*", limit: int = 200) -> list[str]:
    repo = resolve_repo_path(repo_path)
    files: list[str] = []
    for path in repo.glob(pattern):
        if len(files) >= limit:
            break
        if path.is_file() and ".git" not in path.parts:
            files.append(path.relative_to(repo).as_posix())
    return files


def read_file(repo_path: str | Path, file_path: str | Path, max_bytes: int = 80_000) -> str:
    resolved = resolve_inside_repo(repo_path, file_path)
    data = resolved.read_bytes()
    if len(data) > max_bytes:
        head = data[: max_bytes // 2]
        tail = data[-max_bytes // 2 :]
        data = head + b"\n\n[... truncated by repairbot ...]\n\n" + tail
    return data.decode("utf-8", errors="replace")


def search_repo(
    repo_path: str | Path,
    query: str,
    *,
    limit: int = 50,
    max_file_bytes: int = 250_000,
) -> list[str]:
    repo = resolve_repo_path(repo_path)
    if not query:
        return []
    query_lower = query.lower()
    matches: list[str] = []
    for path in repo.rglob("*"):
        if len(matches) >= limit:
            break
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix and path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if query_lower in text.lower() or query_lower in path.name.lower():
            matches.append(path.relative_to(repo).as_posix())
    return matches


def write_file_safely(
    repo_path: str | Path,
    file_path: str | Path,
    content: str,
    *,
    allow_sensitive_edits: bool = False,
    allow_deploy_edits: bool = False,
    max_bytes: int = 200_000,
) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"Refusing to write file larger than {max_bytes} bytes")
    resolved = assert_write_allowed(
        repo_path,
        file_path,
        allow_sensitive_edits=allow_sensitive_edits,
        allow_deploy_edits=allow_deploy_edits,
    )
    if not resolved.exists():
        raise ValueError(f"Refusing to create new file: {file_path}")
    resolved.write_text(content, encoding="utf-8")
    return resolved.relative_to(resolve_repo_path(repo_path)).as_posix()


def replace_text_safely(
    repo_path: str | Path,
    file_path: str | Path,
    old: str,
    new: str,
    *,
    occurrence: int | None = None,
    allow_sensitive_edits: bool = False,
    allow_deploy_edits: bool = False,
) -> str:
    if old == "":
        raise ValueError("Old text must not be empty")
    resolved = assert_write_allowed(
        repo_path,
        file_path,
        allow_sensitive_edits=allow_sensitive_edits,
        allow_deploy_edits=allow_deploy_edits,
    )
    text = resolved.read_text(encoding="utf-8", errors="replace")
    if old not in text:
        raise ValueError(f"Target text not found in {file_path}")
    if occurrence is None:
        updated = text.replace(old, new, 1)
    else:
        parts = text.split(old)
        if occurrence < 1 or occurrence >= len(parts):
            raise ValueError(f"Occurrence {occurrence} not found in {file_path}")
        updated = old.join(parts[:occurrence]) + new + old.join(parts[occurrence:])
    resolved.write_text(updated, encoding="utf-8")
    return resolved.relative_to(resolve_repo_path(repo_path)).as_posix()
