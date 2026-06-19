"""Typed workflow state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


RepairStatus = Literal[
    "initialized",
    "diagnosed",
    "patched",
    "tests_failed",
    "tests_passed",
    "pr_opened",
    "failed",
]


class RepairState(BaseModel):
    repo_path: str
    github_repo: str | None = None
    service_name: str
    logs_path: str
    raw_logs: str = ""
    log_summary: str | None = None
    suspected_files: list[str] = Field(default_factory=list)
    diagnosis: str | None = None
    fix_plan: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    diff: str | None = None
    test_cmd: str | None = None
    test_output: str | None = None
    tests_passed: bool = False
    retry_count: int = 0
    max_retries: int = 2
    branch_name: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    status: RepairStatus = "initialized"

    # Runtime flags used by the local MVP orchestration.
    base_branch: str = "main"
    dry_run: bool = False
    diagnose_only: bool = False
    open_pr: bool = False
    allow_dirty: bool = False
    allow_sensitive_edits: bool = False
    allow_deploy_edits: bool = False

    def as_dict(self) -> dict:
        return self.model_dump()
