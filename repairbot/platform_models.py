"""Domain models for uploaded-log repair runs against external repositories."""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


RunStatus = Literal[
    "queued",
    "running",
    "waiting_for_approval",
    "approved",
    "denied",
    "cancelled",
    "completed",
    "failed",
]


def utc_now() -> datetime:
    return datetime.now(UTC)


class UploadedRepairRequest(BaseModel):
    repo_url: str
    repo_branch: str | None = None
    service_name: str | None = None
    module_path: str | None = None
    language: str | None = None
    build_system: str | None = None
    test_cmd: str | None = None
    model_name: str | None = None
    jira_project: str | None = None
    require_approval: bool | None = None

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, value: str) -> str:
        candidate = value.strip()
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "github.com",
            "www.github.com",
        }:
            raise ValueError("repo_url must be an http(s) GitHub repository URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("repo_url must not contain credentials, query parameters, or fragments")
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) != 2:
            raise ValueError("repo_url must identify a GitHub owner and repository")
        owner, repository = path_parts
        repository = repository[:-4] if repository.endswith(".git") else repository
        if not re.fullmatch(r"[A-Za-z0-9-]+", owner) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+", repository
        ):
            raise ValueError("repo_url contains invalid GitHub owner or repository characters")
        return candidate.rstrip("/")

    @field_validator("module_path")
    @classmethod
    def validate_module_path(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.replace("\\", "/").strip("/")
        if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
            raise ValueError("module_path must stay inside the cloned repository")
        return normalized


class RepositoryRecord(BaseModel):
    repository_id: str
    repo_url: str
    repo_branch: str = "main"
    service_name: str | None = None
    module_path: str | None = None
    clone_path: str | None = None
    detected_language: str | None = None
    detected_build_system: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class LogUploadRecord(BaseModel):
    uploaded_log_id: str
    run_id: str
    original_filename: str
    storage_path: str
    content_sha256: str
    size_bytes: int
    redacted: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class IncidentCandidate(BaseModel):
    error_name: str
    description: str
    likely_files: list[str] = Field(default_factory=list)
    service_name: str | None = None
    module_path: str | None = None


class IncidentRecord(BaseModel):
    incident_id: str
    run_id: str
    error_name: str
    description: str
    proposed_solution: str | None = None
    jira_ticket_url: str | None = None
    pull_request_url: str | None = None
    repo_url: str = Field(description="GitHub repository URL supplied by the operator")
    github_repo_url: str | None = None
    repo_branch: str = "main"
    target_branch: str | None = None
    service_name: str | None = None
    module_path: str | None = None
    uploaded_log_id: str
    status: RunStatus = "queued"
    langsmith_trace_url: str | None = None
    plan_id: str | None = None
    selected_hypothesis: str | None = None
    branch_name: str | None = None
    commit_sha: str | None = None
    likely_files: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class JiraTicketMetadata(BaseModel):
    jira_ticket_id: str
    incident_id: str
    project_key: str
    issue_type: str = "Bug"
    jira_key: str
    jira_ticket_url: str
    title: str = ""
    description: str = ""
    solution: str = ""
    pull_request_url: str | None = None
    status: str = "recorded"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PlatformRepairPlan(BaseModel):
    plan_id: str
    run_id: str
    incident_id: str
    observations: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    selected_hypothesis: str
    selection_reason: str
    planned_edits: list[str] = Field(default_factory=list)
    planned_tests: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class RepairRunRecord(BaseModel):
    run_id: str
    repository_id: str
    incident_id: str
    uploaded_log_id: str
    status: RunStatus = "queued"
    current_step: str = "created"
    require_approval: bool = False
    approval_status: Literal["not_required", "pending", "approved", "denied"] = "not_required"
    repo_url: str = Field(description="GitHub repository URL supplied by the operator")
    github_repo_url: str | None = None
    repo_branch: str = "main"
    target_branch: str | None = None
    service_name: str | None = None
    module_path: str | None = None
    language: str | None = None
    build_system: str | None = None
    test_cmd: str | None = None
    jira_project: str | None = None
    config_source: str = "hardcoded-fallback"
    effective_config: dict[str, Any] = Field(default_factory=dict)
    operator_overrides: dict[str, Any] = Field(default_factory=dict)
    model_provider: str = "openai"
    model_name: str = "openai:gpt-5-mini"
    jira_issue_type: str = "Bug"
    jira_mode: Literal["fake", "real"] = "fake"
    github_mode: Literal["fake", "real"] = "fake"
    plan_id: str | None = None
    selected_hypothesis: str | None = None
    proposed_solution: str | None = None
    branch_name: str | None = None
    commit_sha: str | None = None
    jira_ticket_url: str | None = None
    pull_request_url: str | None = None
    langsmith_trace_url: str | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RepairAttemptRecord(BaseModel):
    repair_attempt_id: str
    run_id: str
    attempt_number: int
    hypothesis: str
    patch_summary: str
    test_command: str
    test_exit_code: int
    test_output: str
    tests_passed: bool
    reflection: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class PullRequestMetadata(BaseModel):
    pull_request_id: str
    incident_id: str
    repo_url: str
    branch_name: str
    base_branch: str
    commit_sha: str
    pull_request_url: str
    status: str = "recorded"
    created_at: datetime = Field(default_factory=utc_now)


class RunDetail(BaseModel):
    run: RepairRunRecord
    incident: IncidentRecord
    plan: PlatformRepairPlan | None = None
    attempts: list[RepairAttemptRecord] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
