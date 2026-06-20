"""Controlled patch and subprocess test execution ports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
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


@dataclass(frozen=True)
class TestResult:
    command: str
    exit_code: int
    output: str

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
        return TestResult(command=command, exit_code=0, output="fake test runner: passed")


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
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                arguments,
                cwd=cwd,
                shell=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout_seconds,
                check=False,
            )
            output = completed.stdout or ""
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            output = f"{partial}\nTest command timed out after {self.timeout_seconds} seconds"
            exit_code = 124
        except OSError:
            output = "Configured test executable could not be started"
            exit_code = 127
        return TestResult(
            command=reviewed,
            exit_code=exit_code,
            output=redact_sensitive_text(str(output))[-20_000:],
        )


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
    updated = text
    updated = _replace_if_present(
        updated,
        "            write(exchange, 201, createOrder(null));",
        '            write(exchange, 201, createOrder(new Customer("42")));',
    )
    guard_anchor = "    public static String createOrder(Customer customer) {\n        String customerId = customer.id();"
    guarded = (
        "    public static String createOrder(Customer customer) {\n"
        "        if (customer == null || customer.id() == null || customer.id().isBlank()) {\n"
        '            return "{\\"error\\":\\"customer is required\\"}";\n'
        "        }\n"
        "        String customerId = customer.id();"
    )
    updated = _replace_if_present(updated, guard_anchor, guarded)
    if updated == text:
        raise PatchApplicationError("No reviewed null-customer patch matched the repository")
    return {path.relative_to(root): updated}


def _payment_service_patch(root: Path) -> dict[Path, str]:
    path = _find_java_file(root, "PaymentServiceApp.java")
    text = path.read_text(encoding="utf-8")
    updated = _replace_if_present(
        text,
        "            write(exchange, 200, charge(-1));",
        "            write(exchange, 200, charge(25));",
    )
    updated = _replace_if_present(
        updated,
        "        if (amount == 0) {",
        "        if (amount <= 0) {",
    )
    if updated == text:
        raise PatchApplicationError("No reviewed invalid-charge patch matched the repository")
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
