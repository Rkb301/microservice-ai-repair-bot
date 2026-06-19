from pathlib import Path

import pytest

from repairbot.tools import filesystem
from repairbot.tools.safety import SafetyError, assert_safe_branch_name, resolve_inside_repo


def test_path_traversal_blocked(tmp_path: Path) -> None:
    with pytest.raises(SafetyError):
        resolve_inside_repo(tmp_path, "../outside.py")


def test_blocked_file_writes(tmp_path: Path) -> None:
    with pytest.raises(SafetyError):
        filesystem.write_file_safely(tmp_path, ".env", "OPENAI_API_KEY=x")
    with pytest.raises(SafetyError):
        filesystem.write_file_safely(tmp_path, "secrets.toml", "token='x'")
    with pytest.raises(SafetyError):
        filesystem.write_file_safely(tmp_path, ".github/workflows/ci.yml", "name: ci")
    with pytest.raises(SafetyError):
        filesystem.write_file_safely(tmp_path, "Dockerfile", "FROM python:3.13")


def test_protected_branch_push_blocked() -> None:
    with pytest.raises(SafetyError):
        assert_safe_branch_name("main")
    with pytest.raises(SafetyError):
        assert_safe_branch_name("feature/not-ai")
    assert_safe_branch_name("ai-fix/service-a/20260614120000")
    assert_safe_branch_name("bugfix/BUG-123")
