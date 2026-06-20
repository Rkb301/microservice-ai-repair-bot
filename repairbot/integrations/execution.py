"""Controlled patch and subprocess test execution ports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Protocol

from ..platform_config import validate_test_command
from ..redaction import redact_sensitive_text
from ..tools.safety import assert_write_allowed, resolve_inside_repo


class PatchApplicationError(RuntimeError):
    """Raised when no reviewed deterministic patch can be applied safely."""


@dataclass(frozen=True)
class PatchResult:
    branch_name: str
    summary: str
    changed_files: list[str]
    no_patch_needed: bool = False


@dataclass(frozen=True)
class TestResult:
    command: str
    exit_code: int
    output: str
    resolved_executable: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


class PatchAgent(Protocol):
    async def apply_patch(
        self,
        *,
        repo_path: Path,
        module_path: str | None,
        branch_name: str,
        selected_hypothesis: str,
        service_name: str | None = None,
        error_name: str | None = None,
    ) -> PatchResult: ...


class TestRunner(Protocol):
    async def run_tests(
        self, *, repo_path: Path, module_path: str | None, command: str
    ) -> TestResult: ...


class FakePatchAgent:
    async def apply_patch(
        self,
        *,
        repo_path: Path,
        module_path: str | None,
        branch_name: str,
        selected_hypothesis: str,
        service_name: str | None = None,
        error_name: str | None = None,
    ) -> PatchResult:
        del repo_path, module_path, service_name, error_name
        return PatchResult(
            branch_name=branch_name,
            summary=f"Dry-run patch scaffold for: {selected_hypothesis}",
            changed_files=[],
        )


class FakeTestRunner:
    async def run_tests(
        self, *, repo_path: Path, module_path: str | None, command: str
    ) -> TestResult:
        del repo_path, module_path
        return TestResult(
            command=command,
            resolved_executable="fake-test-runner",
            exit_code=0,
            output="fake test runner: passed",
            stdout_tail="fake test runner: passed",
        )


class FilePatchAgent:
    """Reviewed deterministic Java patches for the three MVP service failures."""

    async def apply_patch(
        self,
        *,
        repo_path: Path,
        module_path: str | None,
        branch_name: str,
        selected_hypothesis: str,
        service_name: str | None = None,
        error_name: str | None = None,
    ) -> PatchResult:
        del error_name
        root = _module_root(repo_path, module_path)
        service = (service_name or "").strip().lower()
        if service == "user-service":
            changes = _user_service_patch(root)
        elif service == "order-service":
            changes = _order_service_patch(root)
        elif service == "payment-service":
            changes = _payment_service_patch(root)
        else:
            raise PatchApplicationError(
                "Real patch execution currently supports user-service, order-service, and payment-service"
            )
        if not changes:
            return PatchResult(
                branch_name=branch_name,
                summary="Repository already contains the reviewed fix",
                changed_files=[],
                no_patch_needed=True,
            )
        changed_files = self.apply_changes(repo_path, changes)
        return PatchResult(
            branch_name=branch_name,
            summary=(
                f"Applied reviewed {service} validation patch for: "
                f"{redact_sensitive_text(selected_hypothesis)}"
            ),
            changed_files=changed_files,
        )

    def apply_changes(
        self,
        repo_path: Path,
        changes: dict[Path | str, str],
    ) -> list[str]:
        root = repo_path.resolve()
        changed_files: list[str] = []
        for relative, content in changes.items():
            target = assert_write_allowed(root, relative)
            if not target.is_file():
                raise PatchApplicationError(
                    f"Controlled patch target does not exist: {Path(relative).as_posix()}"
                )
            target.write_text(content, encoding="utf-8")
            changed_files.append(target.relative_to(root).as_posix())
        if not changed_files:
            raise PatchApplicationError("Controlled patch produced no file changes")
        return sorted(changed_files)


class SubprocessTestRunner:
    """Shell-free runner for allowlisted test commands inside the clone."""

    def __init__(self, *, timeout_seconds: int = 300) -> None:
        if timeout_seconds < 1 or timeout_seconds > 1800:
            raise ValueError("Test timeout must be between 1 and 1800 seconds")
        self.timeout_seconds = timeout_seconds

    async def run_tests(
        self, *, repo_path: Path, module_path: str | None, command: str
    ) -> TestResult:
        reviewed = validate_test_command(command)
        arguments = shlex.split(reviewed, posix=os.name != "nt")
        cwd = _module_root(repo_path, module_path)
        resolved_executable = _resolve_test_executable(arguments[0])
        if resolved_executable is None:
            safe_name = redact_sensitive_text(arguments[0])
            message = (
                f"Configured test executable '{safe_name}' was not found on PATH. "
                "Install it or update repair.test_command to an available allowlisted executable."
            )
            return TestResult(
                command=reviewed,
                resolved_executable=None,
                exit_code=127,
                output=message,
                stderr_tail=message,
            )
        invocation = [resolved_executable, *arguments[1:]]
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                invocation,
                cwd=cwd,
                shell=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = _decode_subprocess_output(exc.stdout)
            stderr = _decode_subprocess_output(exc.stderr)
            timeout_message = (
                f"Test command timed out after {self.timeout_seconds} seconds"
            )
            stderr = f"{stderr}\n{timeout_message}".strip()
            exit_code = 124
        except OSError as exc:
            stdout = ""
            stderr = (
                "Resolved test executable could not be started: "
                f"{redact_sensitive_text(resolved_executable)}; "
                f"{redact_sensitive_text(str(exc))}"
            )
            exit_code = 127
        safe_stdout = redact_sensitive_text(str(stdout))[-20_000:]
        safe_stderr = redact_sensitive_text(str(stderr))[-20_000:]
        output = _combined_test_output(safe_stdout, safe_stderr)
        return TestResult(
            command=reviewed,
            resolved_executable=redact_sensitive_text(resolved_executable),
            exit_code=exit_code,
            output=output[-20_000:],
            stdout_tail=safe_stdout,
            stderr_tail=safe_stderr,
        )


def _resolve_test_executable(executable: str) -> str | None:
    """Resolve one reviewed executable without invoking a command shell."""

    resolved = shutil.which(executable)
    if resolved:
        return str(Path(resolved).resolve())
    if not _is_windows() or Path(executable).suffix:
        return None
    suffixes = _windows_executable_suffixes()
    for suffix in suffixes:
        resolved = shutil.which(f"{executable}{suffix}")
        if resolved:
            return str(Path(resolved).resolve())
    return None


def _windows_executable_suffixes() -> tuple[str, ...]:
    configured = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    suffixes = [item.strip().lower() for item in configured.split(";") if item.strip()]
    preferred = [".cmd", ".bat"]
    return tuple(dict.fromkeys([*preferred, *suffixes]))


def _is_windows() -> bool:
    return os.name == "nt"


def _decode_subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _combined_test_output(stdout: str, stderr: str) -> str:
    sections: list[str] = []
    if stdout.strip():
        sections.append(f"stdout:\n{stdout.strip()}")
    if stderr.strip():
        sections.append(f"stderr:\n{stderr.strip()}")
    return "\n".join(sections) or "Test command produced no output"


def _module_root(repo_path: Path, module_path: str | None) -> Path:
    root = repo_path.resolve()
    if not root.is_dir():
        raise ValueError("Repository path does not exist")
    module_root = resolve_inside_repo(root, module_path or ".")
    if not module_root.is_dir():
        raise ValueError("Configured module path does not exist")
    return module_root


def _find_java_file(root: Path, filename: str) -> Path:
    matches = sorted(path for path in root.rglob(filename) if path.is_file())
    if len(matches) != 1:
        raise PatchApplicationError(
            f"Expected exactly one controlled patch target named {filename}"
        )
    return matches[0]


def _user_service_patch(root: Path) -> dict[Path, str]:
    path = _find_java_file(root, "UserServiceApp.java")
    text = path.read_text(encoding="utf-8")
    if re.search(r"if\s*\(\s*profile\s*==\s*null\s*\)", text):
        return {}
    unsafe = '        return "{\\"profile\\":\\"" + profile.toUpperCase() + "\\"}";'
    replacement = (
        '        if (profile == null) {\n'
        '            return "{\\"error\\":\\"profile not found\\"}";\n'
        '        }\n'
        '        return "{\\"profile\\":\\"" + profile.toUpperCase() + "\\"}";'
    )
    updated = _replace_once(text, unsafe, replacement, "missing-profile guard")
    return {path.relative_to(root): updated}


def _order_service_patch(root: Path) -> dict[Path, str]:
    path = _find_java_file(root, "OrderServiceApp.java")
    text = path.read_text(encoding="utf-8")
    if not re.search(
        r"public\s+static\s+String\s+createOrder\s*\(\s*Customer\s+customer\s*\)",
        text,
    ) or not re.search(r"customer\s*\.\s*id\s*\(\s*\)", text):
        raise PatchApplicationError(
            "OrderServiceApp.java does not contain the reviewed createOrder shape"
        )
    updated, call_count = re.subn(
        r"createOrder\s*\(\s*null\s*\)",
        'createOrder(new Customer("42"))',
        text,
    )
    if call_count > 1:
        raise PatchApplicationError(
            "Reviewed order call-site patch matched multiple locations"
        )
    has_guard = bool(
        re.search(r"if\s*\(\s*customer\s*==\s*null\b", updated)
    )
    if not has_guard:
        method_pattern = re.compile(
            r"(?m)^(?P<indent>[ \t]*)public\s+static\s+String\s+"
            r"createOrder\s*\(\s*Customer\s+customer\s*\)\s*\{\s*$"
        )
        matches = list(method_pattern.finditer(updated))
        if len(matches) != 1:
            raise PatchApplicationError(
                "Reviewed order guard insertion requires one createOrder method"
            )
        match = matches[0]
        indent = match.group("indent")
        guard = (
            f"{match.group(0)}\n"
            f"{indent}    if (customer == null || customer.id() == null || customer.id().isBlank()) {{\n"
            f'{indent}        return "{{\\"error\\":\\"customer is required\\"}}";\n'
            f"{indent}    }}"
        )
        updated = updated[: match.start()] + guard + updated[match.end() :]
    if updated == text:
        return {}
    return {path.relative_to(root): updated}


def _payment_service_patch(root: Path) -> dict[Path, str]:
    path = _find_java_file(root, "PaymentServiceApp.java")
    text = path.read_text(encoding="utf-8")
    if not re.search(
        r"public\s+static\s+String\s+charge\s*\(\s*int\s+amount\s*\)",
        text,
    ):
        raise PatchApplicationError(
            "PaymentServiceApp.java does not contain the reviewed charge shape"
        )
    updated, call_count = re.subn(
        r"charge\s*\(\s*(?:-[0-9]+|0)\s*\)",
        "charge(25)",
        text,
    )
    if call_count > 1:
        raise PatchApplicationError(
            "Reviewed payment call-site patch matched multiple locations"
        )
    has_guard = bool(re.search(r"if\s*\(\s*amount\s*<=\s*0\s*\)", updated))
    if not has_guard:
        updated, guard_count = re.subn(
            r"if\s*\(\s*amount\s*==\s*0\s*\)",
            "if (amount <= 0)",
            updated,
        )
        if guard_count > 1:
            raise PatchApplicationError(
                "Reviewed payment guard patch matched multiple locations"
            )
        if guard_count == 0:
            method_pattern = re.compile(
                r"(?m)^(?P<indent>[ \t]*)public\s+static\s+String\s+"
                r"charge\s*\(\s*int\s+amount\s*\)\s*\{\s*$"
            )
            matches = list(method_pattern.finditer(updated))
            if len(matches) != 1:
                raise PatchApplicationError(
                    "Reviewed payment guard insertion requires one charge method"
                )
            match = matches[0]
            indent = match.group("indent")
            guard = (
                f"{match.group(0)}\n"
                f"{indent}    if (amount <= 0) {{\n"
                f'{indent}        throw new IllegalArgumentException("amount must be positive");\n'
                f"{indent}    }}"
            )
            updated = updated[: match.start()] + guard + updated[match.end() :]
    if updated == text:
        return {}
    return {path.relative_to(root): updated}


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise PatchApplicationError(f"Expected one reviewed {label} patch location")
    return text.replace(old, new, 1)


def _replace_if_present(text: str, old: str, new: str) -> str:
    if old not in text:
        return text
    if text.count(old) != 1:
        raise PatchApplicationError("Controlled patch matched multiple locations")
    return text.replace(old, new, 1)


__all__ = [
    "FakePatchAgent",
    "FakeTestRunner",
    "FilePatchAgent",
    "PatchAgent",
    "PatchApplicationError",
    "PatchResult",
    "SubprocessTestRunner",
    "TestResult",
    "TestRunner",
]
