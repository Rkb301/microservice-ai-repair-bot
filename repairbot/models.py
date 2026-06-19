"""Pydantic models for the Jira-driven Java prototype."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ServiceConfig(BaseModel):
    name: str
    application_name: str
    repo_path: str | None = None
    github_repo: str | None = None
    build_tool: Literal["maven", "gradle"] = "maven"
    test_cmd: str | None = None
    source_roots: list[str] = Field(default_factory=lambda: ["src/main/java", "src/test/java"])
    log_file: str | None = None
    base_url: str | None = None


class ServicesConfig(BaseModel):
    services: list[ServiceConfig]

    def by_name(self) -> dict[str, ServiceConfig]:
        return {service.name: service for service in self.services}


class ParsedLogEntry(BaseModel):
    id: int | None = None
    run_id: str
    source_file: str
    line_start: int
    line_end: int
    timestamp: str | None = None
    level: str
    service_name: str
    application_name: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    http_method: str | None = None
    endpoint: str | None = None
    status_code: int | None = None
    duration_ms: int | None = None
    package_name: str | None = None
    class_name: str | None = None
    method_name: str | None = None
    file_name: str | None = None
    line_number: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    stack_trace: str | None = None
    raw_log_excerpt: str


class StackTraceBlock(BaseModel):
    source_file: str
    line_start: int
    line_end: int
    header: str
    stack_trace: str


class LogSummary(BaseModel):
    run_id: str
    service_name: str
    lines_scanned: int = 0
    request_count: int = 0
    info_count: int = 0
    warn_count: int = 0
    error_count: int = 0
    top_error_classes: dict[str, int] = Field(default_factory=dict)
    top_endpoints_by_error: dict[str, int] = Field(default_factory=dict)
    affected_trace_ids: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    rank: int
    hypothesis: str
    evidence: str
    confidence: float


class RejectedHypothesis(BaseModel):
    hypothesis: str
    reason: str


class ErrorCluster(BaseModel):
    cluster_id: str
    service_name: str
    application_name: str | None = None
    suspected_root_service: str
    symptom_services: list[str] = Field(default_factory=list)
    upstream_services: list[str] = Field(default_factory=list)
    downstream_services: list[str] = Field(default_factory=list)
    error_type: str | None = None
    class_name: str | None = None
    method_name: str | None = None
    file_name: str | None = None
    line_number: int | None = None
    endpoint: str | None = None
    normalized_error_message: str | None = None
    trace_id_examples: list[str] = Field(default_factory=list)
    affected_trace_ids: list[str] = Field(default_factory=list)
    occurrence_count: int = 0
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    total_lines_scanned: int = 0
    total_requests_detected: int = 0
    normal_log_count: int = 0
    warn_count: int = 0
    error_count: int = 0
    raw_log_excerpt: str | None = None
    stack_trace_excerpt: str | None = None
    representative_success_logs: list[str] = Field(default_factory=list)
    representative_warning_logs: list[str] = Field(default_factory=list)
    representative_error_logs: list[str] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    rejected_hypotheses: list[RejectedHypothesis] = Field(default_factory=list)
    selected_hypothesis: Hypothesis | None = None
    recommended_solution: str | None = None


class JiraTicketRecord(BaseModel):
    jira_key: str
    jira_url: str | None = None
    cluster_id: str
    service_name: str
    summary: str
    status: str = "created"
    payload: dict = Field(default_factory=dict)


class FixAttempt(BaseModel):
    jira_key: str
    attempt: int
    plan_id: str | None = None
    hypothesis_id: str | None = None
    files_inspected: list[str] = Field(default_factory=list)
    dependency_trace: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    diff_stat: str | None = None
    patch_summary: str | None = None
    test_cmd: str | None = None
    test_exit_code: int | None = None
    test_output: str | None = None
    tests_passed: bool = False
    rejection_reason: str | None = None
    artifact_path: str | None = None


class RepairObservation(BaseModel):
    category: str
    summary: str
    evidence: str | None = None


class RepairHypothesis(BaseModel):
    hypothesis_id: str
    suspected_root_cause: str
    evidence: str
    proposed_code_change: str
    expected_test_impact: str
    risk_level: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    confidence_score: float = 0.5
    files_likely_to_change: list[str] = Field(default_factory=list)


class RepairPlan(BaseModel):
    plan_id: str
    ticket_id: str
    selected_service: str
    service_root: str
    cluster_id: str | None = None
    observations: list[RepairObservation] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    hypotheses: list[RepairHypothesis] = Field(default_factory=list)
    selected_hypothesis: RepairHypothesis | None = None
    selection_reason: str | None = None
    planned_edits: list[str] = Field(default_factory=list)
    planned_tests: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)
    relevant_source_files: list[str] = Field(default_factory=list)
    relevant_test_files: list[str] = Field(default_factory=list)
    baseline_test_status: str = "not_run"


class RepairReflection(BaseModel):
    reflection_id: str
    plan_id: str
    ticket_id: str
    attempt: int
    hypothesis_id: str | None = None
    tests_passed: bool = False
    test_exit_code: int | None = None
    failed_test_summary: str | None = None
    decision: Literal["adjust_same_hypothesis", "try_next_hypothesis", "stop"] = "stop"
    next_hypothesis_id: str | None = None
    rationale: str
    post_test_reflection: str
    artifact_path: str | None = None


class RepairAttemptArtifact(BaseModel):
    jira_key: str
    attempt: int
    plan_id: str | None = None
    hypothesis_id: str | None = None
    artifact_path: str
    changed_files: list[str] = Field(default_factory=list)
    diff_stat_path: str | None = None
    diff_patch_path: str | None = None
    test_output_path: str | None = None
    reflection_path: str | None = None


class PullRequestRecord(BaseModel):
    jira_key: str
    service_name: str
    branch_name: str
    pr_url: str
    commit_sha: str | None = None


class AgentEvent(BaseModel):
    run_id: str
    agent_name: str
    event_type: str
    payload: dict = Field(default_factory=dict)


class RepairRunState(BaseModel):
    run_id: str
    logs_dir: str | None = None
    services_config_path: str | None = None
    mode: str = "analyze-only"
    summaries: list[LogSummary] = Field(default_factory=list)
    clusters: list[ErrorCluster] = Field(default_factory=list)
    jira_tickets: list[JiraTicketRecord] = Field(default_factory=list)
    fix_attempts: list[FixAttempt] = Field(default_factory=list)
    pull_requests: list[PullRequestRecord] = Field(default_factory=list)
