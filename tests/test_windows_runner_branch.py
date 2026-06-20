from __future__ import annotations

import asyncio
from pathlib import Path
import re
import subprocess

import pytest
from typer.testing import CliRunner

from repairbot.cli import app as cli_app
from repairbot.integrations import execution
from repairbot.integrations.execution import SubprocessTestRunner
from repairbot.platform_config import ConfigSafetyError, validate_test_command
from repairbot.workflow import build_repair_branch_name


def test_maven_resolves_to_cmd_wrapper_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wrapper = tmp_path / "mvn.cmd"
    wrapper.write_text("@echo off\n", encoding="utf-8")
    lookups: list[str] = []
    invocations: list[tuple[list[str], bool]] = []

    def fake_which(name: str) -> str | None:
        lookups.append(name)
        return str(wrapper) if name.lower() == "mvn.cmd" else None

    def fake_run(arguments, **kwargs):
        invocations.append((list(arguments), kwargs["shell"]))
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout="BUILD SUCCESS\n",
            stderr="Maven diagnostic\n",
        )

    monkeypatch.setattr(execution, "_is_windows", lambda: True)
    monkeypatch.setattr(execution.shutil, "which", fake_which)
    monkeypatch.setattr(execution.subprocess, "run", fake_run)
    result = asyncio.run(
        SubprocessTestRunner().run_tests(
            repo_path=tmp_path,
            module_path=None,
            command="mvn test",
        )
    )

    assert lookups[:2] == ["mvn", "mvn.cmd"]
    assert result.resolved_executable == str(wrapper.resolve())
    assert result.passed is True
    assert result.stdout_tail == "BUILD SUCCESS\n"
    assert result.stderr_tail == "Maven diagnostic\n"
    assert "stdout:" in result.output and "stderr:" in result.output
    assert invocations == [([str(wrapper.resolve()), "test"], False)]


def test_maven_resolves_plain_executable_off_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "mvn"
    executable.write_text("", encoding="utf-8")

    monkeypatch.setattr(execution, "_is_windows", lambda: False)
    monkeypatch.setattr(
        execution.shutil,
        "which",
        lambda name: str(executable) if name == "mvn" else None,
    )
    monkeypatch.setattr(
        execution.subprocess,
        "run",
        lambda arguments, **kwargs: subprocess.CompletedProcess(
            arguments, 0, stdout="ok\n", stderr=""
        ),
    )
    result = asyncio.run(
        SubprocessTestRunner().run_tests(
            repo_path=tmp_path,
            module_path=None,
            command="mvn test",
        )
    )
    assert result.resolved_executable == str(executable.resolve())
    assert result.passed is True


def test_missing_or_unstartable_executable_has_redacted_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(execution.shutil, "which", lambda name: None)
    missing = asyncio.run(
        SubprocessTestRunner().run_tests(
            repo_path=tmp_path,
            module_path=None,
            command="mvn test",
        )
    )
    assert missing.exit_code == 127
    assert missing.resolved_executable is None
    assert "'mvn' was not found on PATH" in missing.output

    executable = tmp_path / "mvn.cmd"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(execution.shutil, "which", lambda name: str(executable))

    def fail_start(arguments, **kwargs):
        sensitive_label = "GITHUB_" + "TOKEN"
        raise OSError(f"{sensitive_label}=should-not-appear")

    monkeypatch.setattr(execution.subprocess, "run", fail_start)
    failed = asyncio.run(
        SubprocessTestRunner().run_tests(
            repo_path=tmp_path,
            module_path=None,
            command="mvn.cmd test",
        )
    )
    assert failed.exit_code == 127
    assert "Resolved test executable could not be started" in failed.output
    assert "should-not-appear" not in failed.output
    assert "<redacted>" in failed.output


@pytest.mark.parametrize(
    "operator",
    ["&&", "&", "||", ";", "|", ">", "<", "`", "$(whoami)", "%PATH%", "!X!", "^"],
)
def test_test_command_rejects_shell_operators(operator: str) -> None:
    with pytest.raises(ConfigSafetyError, match="shell control operators"):
        validate_test_command(f"mvn test {operator} anything")


@pytest.mark.parametrize(
    "command",
    [
        "mvn test",
        "mvn -q test",
        "mvn.cmd test",
        "gradle test",
        "gradle.bat test",
        "python -m pytest",
    ],
)
def test_reviewed_cross_platform_commands_remain_allowlisted(command: str) -> None:
    assert validate_test_command(command) == command


def test_branch_uses_jira_key_service_and_short_error_slug() -> None:
    assert build_repair_branch_name(
        jira_key="demo-1001",
        service_name="User Service",
        error_name="NullPointerException",
        description="profile missing",
        run_id="run_123456789abc",
    ) == "bugfix/DEMO-1001-user-service-null-pointer"


def test_branch_falls_back_and_sanitizes_invalid_components() -> None:
    branch = build_repair_branch_name(
        jira_key="../../main",
        service_name="../User Service...",
        error_name="Bad/Thing",
        description=None,
        run_id="run_ABC../123",
    )
    assert branch == "bugfix/user-service-runabc123"
    assert ".." not in branch
    assert re.fullmatch(r"bugfix/[A-Za-z0-9._-]+", branch)
    assert not branch.endswith(("/", "."))


def test_repair_run_summary_includes_execution_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_PROJECT_KEY", "DEMO")
    result = CliRunner().invoke(
        cli_app,
        [
            "repair-run",
            "--log-file",
            "examples/demo-logs/user-service-missing-profile.log",
            "--repo-url",
            "https://github.com/Rkb301/user-service.git",
            "--service-name",
            "user-service",
            "--target-branch",
            "main",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert re.search(
        r"branch: bugfix/DEMO-[0-9]+-user-service-null-pointer",
        result.output,
    )
    assert "test command: mvn test" in result.output
    assert "resolved executable: fake-test-runner" in result.output
    assert "exit code: 0" in result.output
    assert "test output tail:" in result.output
    assert "fake test runner: passed" in result.output
    assert "storage mode: memory" in result.output
