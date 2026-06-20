"""Build the complete backend run contract consumed by CLI and API clients."""

from __future__ import annotations

from typing import Any

from .architecture import project_mvp_metadata
from .persistence import COLLECTION_NAMES, RepairPlatformRepository
from .platform_models import (
    ConceptualAgentOutput,
    IncidentRecord,
    PlatformRepairPlan,
    RepairAttemptRecord,
    RepairRunRecord,
    RunDetail,
    TraceMetadata,
)


async def build_run_detail(
    repository: RepairPlatformRepository,
    run: RepairRunRecord,
) -> RunDetail:
    incident = await repository.get_incident(run.incident_id)
    if incident is None:
        raise KeyError(f"Missing incident for run {run.run_id}")
    plan = await repository.get_plan_for_run(run.run_id)
    attempts = await repository.list_attempts(run.run_id)
    jira = await repository.get_jira_ticket_for_incident(run.incident_id)
    pull_request = await repository.get_pull_request_for_incident(run.incident_id)
    effective_config = run.effective_config
    trace = _trace_metadata(run, effective_config)
    metadata = {
        **project_mvp_metadata(),
        "execution_mode": _execution_mode(run),
        "external_calls_performed": bool(
            run.allow_network and (run.github_mode == "real" or run.jira_mode == "real")
        ),
        "dry_run": run.dry_run,
        "repository_id": run.repository_id,
        "uploaded_log_id": run.uploaded_log_id,
        "incident_id": run.incident_id,
        "config_source": run.config_source,
        "effective_config": effective_config,
        "persisted_collections": list(COLLECTION_NAMES),
        "agent_output_count": 3,
    }
    return RunDetail(
        run=run,
        incident=incident,
        agents=_agent_outputs(run, incident, plan, attempts),
        jira=jira,
        pull_request=pull_request,
        effective_config=effective_config,
        trace=trace,
        plan=plan,
        attempts=attempts,
        metadata=metadata,
    )


def _agent_outputs(
    run: RepairRunRecord,
    incident: IncidentRecord,
    plan: PlatformRepairPlan | None,
    attempts: list[RepairAttemptRecord],
) -> dict[str, ConceptualAgentOutput]:
    likely_files = incident.likely_files or ["No source file inferred from the uploaded stack trace"]
    log_analysis = ConceptualAgentOutput(
        agent_name="Log Analysis Agent",
        status="completed",
        summary=(
            f"Parsed {incident.error_name} for "
            f"{incident.service_name or 'the selected service'}: {incident.description}"
        ),
        evidence=likely_files,
        details={
            "error_name": incident.error_name,
            "description": incident.description,
            "affected_service": incident.service_name,
            "likely_files": incident.likely_files,
        },
    )
    if plan is None:
        repair_planning = ConceptualAgentOutput(
            agent_name="Repair Planning Agent",
            status="pending",
            summary="Repair planning has not completed.",
        )
    else:
        repair_planning = ConceptualAgentOutput(
            agent_name="Repair Planning Agent",
            status="completed",
            summary=run.proposed_solution or "A repair plan was produced.",
            evidence=plan.observations,
            details={
                "plan_id": plan.plan_id,
                "hypotheses": plan.hypotheses,
                "selected_hypothesis": plan.selected_hypothesis,
                "planned_edits": plan.planned_edits,
                "planned_tests": plan.planned_tests,
            },
        )
    if not attempts:
        code_repair = ConceptualAgentOutput(
            agent_name="Code Repair Agent",
            status="pending",
            summary="Code repair is waiting for approval or execution.",
            details={"branch_name": run.branch_name},
        )
    else:
        latest = attempts[-1]
        code_repair = ConceptualAgentOutput(
            agent_name="Code Repair Agent",
            status="completed" if latest.tests_passed else "failed",
            summary=latest.patch_summary,
            evidence=[latest.test_output],
            details={
                "attempt_number": latest.attempt_number,
                "branch_name": run.branch_name,
                "commit_sha": run.commit_sha,
                "test_command": latest.test_command,
                "test_exit_code": latest.test_exit_code,
                "tests_passed": latest.tests_passed,
                "changed_files": latest.changed_files,
                "reflection": latest.reflection,
            },
        )
    return {
        "log_analysis": log_analysis,
        "repair_planning": repair_planning,
        "code_repair": code_repair,
    }


def _trace_metadata(
    run: RepairRunRecord, effective_config: dict[str, Any]
) -> TraceMetadata:
    langsmith = effective_config.get("langsmith", {})
    local_trace_id = run.local_trace_id or f"trace_{run.run_id.removeprefix('run_')}"
    local_trace_url = run.langsmith_trace_url or f"langsmith://local-demo/{run.run_id}"
    return TraceMetadata(
        local_trace_id=local_trace_id,
        local_trace_url=local_trace_url,
        langsmith_configured=bool(langsmith.get("enabled", False)),
        langsmith_active=run.langsmith_enabled,
        langsmith_project=run.langsmith_project or langsmith.get("project"),
        langsmith_tags=run.langsmith_tags or list(langsmith.get("tags", [])),
    )


def _execution_mode(run: RepairRunRecord) -> str:
    if run.execution_mode == "fake" and run.jira_mode == "fake" and run.github_mode == "fake":
        return "local-fake"
    if run.dry_run:
        return "credential-gated-dry-run"
    return "credential-gated-real"


__all__ = ["build_run_detail"]
