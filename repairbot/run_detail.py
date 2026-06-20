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
    jira = await repository.get_jira_ticket_for_run(run.run_id)
    if jira is None:
        jira = await repository.get_jira_ticket_for_incident(run.incident_id)
    pull_request = await repository.get_pull_request_for_run(run.run_id)
    if pull_request is None:
        pull_request = await repository.get_pull_request_for_incident(run.incident_id)
    effective_config = run.effective_config
    trace = _trace_metadata(run, effective_config)
    metadata = {
        **project_mvp_metadata(),
        "execution_mode": _execution_mode(run),
        "external_calls_performed": bool(
            run.openai_api_invoked
            or (
                run.allow_network
                and (run.github_mode == "real" or run.jira_mode == "real")
            )
        ),
        "dry_run": run.dry_run,
        "repository_id": run.repository_id,
        "uploaded_log_id": run.uploaded_log_id,
        "incident_id": run.incident_id,
        "config_source": run.config_source,
        "effective_config": effective_config,
        "persisted_collections": list(COLLECTION_NAMES),
        "planner_mode": run.planner_mode,
        "model_provider": run.model_provider,
        "model_name": run.model_name,
        "agents_enabled": run.agents_enabled,
        "agent_count": run.agent_count,
        "agent_names": run.agent_names,
        "openai_api_invoked": run.openai_api_invoked,
        "langchain_used": run.langchain_used,
        "langgraph_used": run.langgraph_used,
        "langsmith_enabled": run.langsmith_enabled,
        "prompt_version": run.prompt_version,
        "prompt_template_names": run.prompt_template_names,
        "rendered_agent_inputs": run.rendered_agent_inputs,
        "structured_agent_outputs": run.agent_outputs,
        "agent_output_count": len(run.agent_outputs),
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
    log_details = run.agent_outputs.get("LogAnalysisAgent", {})
    planning_details = run.agent_outputs.get("RepairPlanningAgent", {})
    review_details = run.agent_outputs.get("ReviewPrAgent", {})
    log_analysis = ConceptualAgentOutput(
        agent_name="LogAnalysisAgent",
        status="completed" if log_details else "pending",
        summary=(
            str(log_details.get("description"))
            if log_details
            else "Log analysis has not completed."
        ),
        evidence=[str(item) for item in log_details.get("direct_evidence", [])],
        details=log_details,
    )
    repair_planning = ConceptualAgentOutput(
        agent_name="RepairPlanningAgent",
        status="completed" if planning_details else "pending",
        summary=(
            str(planning_details.get("jira_solution"))
            if planning_details
            else "Repair planning has not completed."
        ),
        evidence=[str(item) for item in planning_details.get("observations", [])],
        details=planning_details,
    )
    review_pr = ConceptualAgentOutput(
        agent_name="ReviewPrAgent",
        status="completed" if review_details else "pending",
        summary=(
            str(review_details.get("pr_summary"))
            if review_details
            else "Patch and test review has not completed."
        ),
        evidence=[str(item) for item in review_details.get("review_checklist", [])],
        details=review_details,
    )
    return {
        "log_analysis": log_analysis,
        "repair_planning": repair_planning,
        "review_pr": review_pr,
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
