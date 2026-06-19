"""Patch and test execution ports for the repair workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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
    ) -> PatchResult: ...


class TestRunner(Protocol):
    async def run_tests(self, *, repo_path: Path, module_path: str | None, command: str) -> TestResult: ...


class FakePatchAgent:
    async def apply_patch(
        self,
        *,
        repo_path: Path,
        module_path: str | None,
        branch_name: str,
        selected_hypothesis: str,
    ) -> PatchResult:
        del repo_path, module_path
        return PatchResult(
            branch_name=branch_name,
            summary=f"Dry-run patch scaffold for: {selected_hypothesis}",
            changed_files=[],
        )


class FakeTestRunner:
    async def run_tests(self, *, repo_path: Path, module_path: str | None, command: str) -> TestResult:
        del repo_path, module_path
        return TestResult(command=command, exit_code=0, output="fake test runner: passed")
