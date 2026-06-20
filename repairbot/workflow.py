"""LangGraph orchestration for uploaded logs and external GitHub repositories."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict
from uuid import uuid4

from .ingestion import UploadedLogIngestionService
from .config import RepairBotSettings
from .integrations.execution import (
    FakePatchAgent,
    FakeTestRunner,
    FilePatchAgent,
    PatchAgent,
    SubprocessTestRunner,
    TestRunner,
)
from .integrations.github import (
    FakeGitHubClient,
    GitHubClient,
    RealGitHubClient,
    build_github_client,
)
from .integrations.jira import FakeJiraClient, JiraClient, RealJiraClient, build_jira_client
from .langstack import (
    AGENT_NAMES,
    HeuristicStructuredPlanner,
    LangStackAgentError,
    LangChainStructuredPlanner,
    LogAnalysisOutput,
    StructuredPlanner,
)
from .persistence import InMemoryRepairPlatformRepository, RepairPlatformRepository
from .platform_config import (
    DEFAULT_CONFIG_PATH,
    EffectiveRepairConfig,
    find_project_config_path,
    load_config_file,
    merge_effective_config,
    render_config_template,
)
from .platform_models import (
    IncidentCandidate,
    JiraTicketMetadata,
    PullRequestMetadata,
    RepairAttemptRecord,
    RepairRunRecord,
)
from .redaction import redact_sensitive_text
from .tracing import LangSmithTracingConfig


class RepairWorkflowState(TypedDict, total=False):
    run_id: str
    repository_id: str
    incident_id: str
    uploaded_log_id: str
    repo_url: str
    repo_branch: str
    service_name: str | None
    module_path: str | None
    language: str | None
    build_system: str | None
    test_cmd: str | None
    jira_project: str | None
    require_approval: bool
    approval_granted: bool | None
    log_path: str
    clone_path: str
    error_name: str
    description: str
    proposed_solution: str
    likely_files: list[str]
    log_analysis_output: dict[str, Any]
    jira_key: str
    jira_ticket_url: str
    plan_id: str
    selected_hypothesis: str
    branch_name: str
    patch_summary: str
    changed_files: list[str]
    no_patch_needed: bool
    review_pr_output: dict[str, Any]
    attempt_count: int
    resolved_test_executable: str | None
    test_exit_code: int
    test_output: str
    test_stdout_tail: str
    test_stderr_tail: str
    tests_passed: bool
    reflection: str
    commit_sha: str
    pull_request_url: str
    langsmith_trace_url: str | None
    status: str
    current_step: str
    operator_overrides: dict[str, Any]
    effective_config: dict[str, Any]
    config_source: str
    model_provider: str
    model_name: str
    jira_issue_type: str
    jira_mode: str
    github_mode: str
    execution_mode: str
    allow_network: bool
    allow_api: bool
    planner_mode: str
    dry_run: bool
    target_branch: str


@dataclass
class WorkflowDependencies:
    repository: RepairPlatformRepository
    workspace_root: Path
    github: GitHubClient = field(default_factory=FakeGitHubClient)
    jira: JiraClient = field(default_factory=FakeJiraClient)
    planner: StructuredPlanner = field(default_factory=HeuristicStructuredPlanner)
    patch_agent: PatchAgent = field(default_factory=FakePatchAgent)
    test_runner: TestRunner = field(default_factory=FakeTestRunner)
    tracing: LangSmithTracingConfig = field(
        default_factory=lambda: LangSmithTracingConfig(False, False, None)
    )
    use_interrupts: bool = False
    max_attempts: int = 2
    default_config_path: Path = DEFAULT_CONFIG_PATH


def default_workflow_dependencies(
    *,
    repository: RepairPlatformRepository | None = None,
    workspace_root: Path | None = None,
) -> WorkflowDependencies:
    return WorkflowDependencies(
        repository=repository or InMemoryRepairPlatformRepository(),
        workspace_root=(workspace_root or Path(".workspace")).resolve(),
    )


async def initial_workflow_state(
    repository: RepairPlatformRepository,
    run: RepairRunRecord,
) -> RepairWorkflowState:
    upload = await repository.get_log_upload(run.uploaded_log_id)
    if upload is None:
        raise KeyError(f"Missing uploaded log for run {run.run_id}")
    return RepairWorkflowState(
        run_id=run.run_id,
        repository_id=run.repository_id,
        incident_id=run.incident_id,
        uploaded_log_id=run.uploaded_log_id,
        repo_url=run.repo_url,
        repo_branch=run.repo_branch,
        service_name=run.service_name,
        module_path=run.module_path,
        language=run.language,
        build_system=run.build_system,
        test_cmd=run.test_cmd,
        jira_project=run.jira_project,
        operator_overrides=run.operator_overrides,
        effective_config=run.effective_config,
        config_source=run.config_source,
        proposed_solution=run.proposed_solution,
        require_approval=run.require_approval,
        execution_mode=run.execution_mode,
        allow_network=run.allow_network,
        dry_run=run.dry_run,
        approval_granted=True if run.approval_status == "approved" else None,
        log_path=upload.storage_path,
        attempt_count=0,
        status="queued",
        current_step="created",
        langsmith_trace_url=run.langsmith_trace_url,
        allow_api=run.allow_api,
        planner_mode=run.planner_mode,
    )


def build_repair_graph(
    dependencies: WorkflowDependencies | None = None,
    *,
    checkpointer: Any | None = None,
) -> Any:
    """Build the explicit platform state graph without contacting external services."""

    from langgraph.graph import END, START, StateGraph

    deps = dependencies or default_workflow_dependencies()
    builder = StateGraph(RepairWorkflowState)
    builder.add_node("ingest_log", _bind(ingest_log, deps))
    builder.add_node("clone_repository", _bind(clone_repository, deps))
    builder.add_node("load_project_config", _bind(load_effective_project_config, deps))
    builder.add_node("detect_project", _bind(detect_project, deps))
    builder.add_node("analyze_log", _bind(analyze_log, deps))
    builder.add_node("create_or_record_jira_ticket", _bind(create_or_record_jira_ticket, deps))
    builder.add_node("plan_repair", _bind(plan_repair, deps))
    builder.add_node("human_approval", _bind(human_approval, deps))
    builder.add_node("patch_repository", _bind(patch_repository, deps))
    builder.add_node("run_tests", _bind(run_tests, deps))
    builder.add_node("reflect_or_retry", _bind(reflect_or_retry, deps))
    builder.add_node("review_pr", _bind(review_pr, deps))
    builder.add_node("commit_and_open_pr", _bind(commit_and_open_pr, deps))
    builder.add_node("finalize_run", _bind(finalize_run, deps))

    builder.add_edge(START, "ingest_log")
    builder.add_edge("ingest_log", "clone_repository")
    builder.add_edge("clone_repository", "load_project_config")
    builder.add_edge("load_project_config", "detect_project")
    builder.add_edge("detect_project", "analyze_log")
    builder.add_edge("analyze_log", "plan_repair")
    builder.add_edge("plan_repair", "create_or_record_jira_ticket")
    builder.add_edge("create_or_record_jira_ticket", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        route_after_approval,
        {"patch": "patch_repository", "finalize": "finalize_run", "wait": END},
    )
    builder.add_edge("patch_repository", "run_tests")
    builder.add_edge("run_tests", "reflect_or_retry")
    builder.add_conditional_edges(
        "reflect_or_retry",
        lambda state: route_after_reflection(state, deps.max_attempts),
        {"retry": "patch_repository", "commit": "review_pr", "finalize": "review_pr"},
    )
    builder.add_conditional_edges(
        "review_pr",
        route_after_review,
        {"commit": "commit_and_open_pr", "finalize": "finalize_run"},
    )
    builder.add_edge("commit_and_open_pr", "finalize_run")
    builder.add_edge("finalize_run", END)
    return builder.compile(checkpointer=checkpointer)


async def ingest_log(state: RepairWorkflowState, deps: WorkflowDependencies) -> dict[str, Any]:
    await _progress(state, deps, "ingest_log")
    path = Path(state["log_path"])
    redacted = redact_sensitive_text(path.read_text(encoding="utf-8", errors="replace"))
    path.write_text(redacted, encoding="utf-8")
    return {"status": "running", "current_step": "ingest_log"}


async def clone_repository(state: RepairWorkflowState, deps: WorkflowDependencies) -> dict[str, Any]:
    await _progress(state, deps, "clone_repository")
    bootstrap = _effective_config(state, deps, repo_config=None)
    _configure_github_client(
        bootstrap,
        deps,
        allow_network=bool(state.get("allow_network")),
    )
    target_branch = bootstrap.github.default_base_branch
    destination = deps.workspace_root / "runs" / state["run_id"] / "repo"
    clone_path = await deps.github.clone_repository(
        state["repo_url"], target_branch, destination
    )
    await deps.repository.update_repository(
        state["repository_id"], clone_path=str(clone_path)
    )
    return {
        "clone_path": str(clone_path),
        "repo_branch": target_branch,
        "effective_config": bootstrap.model_dump(mode="json"),
        "config_source": _config_source(state, None, deps),
        "current_step": "clone_repository",
    }


async def load_effective_project_config(
    state: RepairWorkflowState, deps: WorkflowDependencies
) -> dict[str, Any]:
    await _progress(state, deps, "load_project_config")
    config_path = find_project_config_path(
        state["clone_path"], state.get("module_path")
    )
    repo_config = load_config_file(config_path) if config_path else None
    effective = _effective_config(state, deps, repo_config=repo_config)
    _configure_adapters(
        effective,
        deps,
        allow_network=bool(state.get("allow_network")),
    )
    deps.max_attempts = effective.model.max_attempts
    _configure_planner(
        effective,
        deps,
        allow_api=bool(state.get("allow_api")),
    )
    configure_planner = getattr(deps.planner, "configure", None)
    if callable(configure_planner):
        configure_planner(
            provider=effective.model.provider,
            model_name=effective.model.model,
            max_hypotheses=effective.model.max_hypotheses,
            prompts=effective.prompts,
        )
    tracing_env_enabled = any(
        os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}
        for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")
    )
    deps.tracing = LangSmithTracingConfig(
        requested=effective.langsmith.enabled,
        enabled=(
            effective.langsmith.enabled
            and tracing_env_enabled
            and bool(os.getenv("LANGSMITH_API_KEY"))
        ),
        project=effective.langsmith.project,
        tags=tuple(effective.langsmith.tags),
    )
    source = _config_source(state, config_path, deps)
    approval_status = (
        "approved"
        if state.get("approval_granted") is True
        else "pending" if effective.repair.require_plan_approval else "not_required"
    )
    snapshot = effective.model_dump(mode="json")
    run_changes = {
        "config_source": source,
        "effective_config": snapshot,
        "model_provider": effective.model.provider,
        "model_name": effective.model.model,
        "planner_mode": effective.prompts.planner_mode,
        "agents_enabled": True,
        "agent_count": len(AGENT_NAMES),
        "agent_names": list(AGENT_NAMES),
        "openai_api_invoked": False,
        "langchain_used": effective.prompts.planner_mode == "openai",
        "langgraph_used": True,
        "prompt_version": effective.prompts.version,
        "prompt_template_names": [
            "log_analysis_agent",
            "repair_planning_agent",
            "review_pr_agent",
        ],
        "jira_project": effective.jira.project_key,
        "jira_issue_type": effective.jira.issue_type,
        "jira_mode": effective.jira.mode,
        "github_mode": effective.github.mode,
        "execution_mode": effective.repair.execution_mode,
        "dry_run": effective.repair.dry_run_default,
        "langsmith_enabled": deps.tracing.enabled,
        "langsmith_project": effective.langsmith.project,
        "langsmith_tags": list(effective.langsmith.tags),
        "repo_branch": effective.github.default_base_branch,
        "target_branch": effective.github.default_base_branch,
        "service_name": effective.service_name,
        "module_path": effective.repair.module_path,
        "language": effective.repair.language,
        "build_system": effective.repair.build_system,
        "test_cmd": effective.repair.test_command,
        "require_approval": effective.repair.require_plan_approval,
        "approval_status": approval_status,
    }
    await deps.repository.update_run(state["run_id"], **run_changes)
    await deps.repository.update_incident(
        state["incident_id"],
        repo_branch=effective.github.default_base_branch,
        target_branch=effective.github.default_base_branch,
        service_name=effective.service_name,
        module_path=effective.repair.module_path,
    )
    await deps.repository.update_repository(
        state["repository_id"],
        repo_branch=effective.github.default_base_branch,
        service_name=effective.service_name,
        module_path=effective.repair.module_path,
    )
    return {
        **run_changes,
        "jira_project": effective.jira.project_key,
        "require_approval": effective.repair.require_plan_approval,
        "effective_config": snapshot,
        "config_source": source,
        "current_step": "load_project_config",
    }


async def detect_project(state: RepairWorkflowState, deps: WorkflowDependencies) -> dict[str, Any]:
    await _progress(state, deps, "detect_project")
    repo_root = Path(state["clone_path"]).resolve()
    module_root = (repo_root / (state.get("module_path") or ".")).resolve()
    if not module_root.is_relative_to(repo_root):
        raise ValueError("module_path escaped the cloned repository")
    language = state.get("language")
    build_system = state.get("build_system")
    test_cmd = state.get("test_cmd")
    if (module_root / "pom.xml").exists():
        language, build_system = language or "java", build_system or "maven"
        test_cmd = test_cmd or "mvn test"
    elif (module_root / "build.gradle").exists() or (module_root / "build.gradle.kts").exists():
        language, build_system = language or "java", build_system or "gradle"
        test_cmd = test_cmd or "gradle test"
    else:
        language, build_system, test_cmd = language or "java", build_system or "maven", test_cmd or "mvn test"
    await deps.repository.update_repository(
        state["repository_id"],
        detected_language=language,
        detected_build_system=build_system,
    )
    await deps.repository.update_run(
        state["run_id"], language=language, build_system=build_system, test_cmd=test_cmd
    )
    return {
        "language": language,
        "build_system": build_system,
        "test_cmd": test_cmd,
        "current_step": "detect_project",
    }


async def analyze_log(state: RepairWorkflowState, deps: WorkflowDependencies) -> dict[str, Any]:
    await _progress(state, deps, "analyze_log")
    text = Path(state["log_path"]).read_text(encoding="utf-8", errors="replace")
    try:
        analysis = await deps.planner.analyze_log(
            run_id=state["run_id"],
            log_text=text,
            service_name=state.get("service_name"),
            module_path=state.get("module_path"),
            tracing=deps.tracing,
        )
    except LangStackAgentError as exc:
        await _record_agent_failure(state, deps, exc)
        raise
    await deps.repository.update_incident(
        state["incident_id"],
        error_name=analysis.error_name,
        description=analysis.description,
        likely_files=analysis.likely_files,
        status="running",
    )
    await deps.repository.update_run(state["run_id"], **_agent_run_changes(deps))
    return {
        "error_name": analysis.error_name,
        "description": analysis.description,
        "likely_files": analysis.likely_files,
        "log_analysis_output": analysis.model_dump(mode="json"),
        "current_step": "analyze_log",
    }


async def create_or_record_jira_ticket(
    state: RepairWorkflowState, deps: WorkflowDependencies
) -> dict[str, Any]:
    await _progress(state, deps, "create_or_record_jira_ticket")
    config = EffectiveRepairConfig.model_validate(state["effective_config"])
    template_values = {
        "service_name": state.get("service_name") or "service",
        "error_name": state["error_name"],
        "description": state["description"],
        "proposed_solution": state["proposed_solution"],
        "selected_hypothesis": state["selected_hypothesis"],
        "repo_url": state["repo_url"],
        "target_branch": state.get("repo_branch") or "main",
    }
    title = redact_sensitive_text(
        render_config_template(config.jira.title_template, template_values)
    )
    description = redact_sensitive_text(
        render_config_template(config.jira.description_template, template_values)
    )
    solution = redact_sensitive_text(
        render_config_template(config.jira.solution_template, template_values)
    )
    result = await deps.jira.create_or_record_ticket(
        project_key=config.jira.project_key,
        issue_type=config.jira.issue_type,
        title=title,
        description=description,
        solution=solution,
        incident_id=state["incident_id"],
        service_name=state.get("service_name"),
        repo_url=state.get("repo_url"),
        run_id=state.get("run_id"),
    )
    await deps.repository.save_jira_ticket(
        JiraTicketMetadata(
            jira_ticket_id=f"jira_{result.jira_key.lower()}",
            incident_id=state["incident_id"],
            run_id=state["run_id"],
            project_key=config.jira.project_key,
            issue_type=config.jira.issue_type,
            jira_key=result.jira_key,
            jira_ticket_url=result.jira_ticket_url,
            title=title,
            description=description,
            solution=solution,
        )
    )
    await deps.repository.update_incident(
        state["incident_id"], jira_ticket_url=result.jira_ticket_url
    )
    await deps.repository.update_run(
        state["run_id"], jira_ticket_url=result.jira_ticket_url
    )
    return {
        "jira_key": result.jira_key,
        "jira_ticket_url": result.jira_ticket_url,
        "current_step": "create_or_record_jira_ticket",
    }


async def plan_repair(state: RepairWorkflowState, deps: WorkflowDependencies) -> dict[str, Any]:
    await _progress(state, deps, "plan_repair")
    candidate = IncidentCandidate(
        error_name=state["error_name"],
        description=state["description"],
        likely_files=state.get("likely_files", []),
        service_name=state.get("service_name"),
        module_path=state.get("module_path"),
    )
    try:
        plan = await deps.planner.create_plan(
            run_id=state["run_id"],
            incident_id=state["incident_id"],
            incident=candidate,
            repo_path=Path(state["clone_path"]),
            test_command=state["test_cmd"],
            analysis=LogAnalysisOutput.model_validate(state["log_analysis_output"]),
            repo_url=state["repo_url"],
            target_branch=state.get("repo_branch") or "main",
            build_system=state.get("build_system") or "unknown",
            tracing=deps.tracing,
        )
    except LangStackAgentError as exc:
        await _record_agent_failure(state, deps, exc)
        raise
    planning_output = deps.planner.last_outputs.get("RepairPlanningAgent", {})
    proposed_solution = redact_sensitive_text(
        str(planning_output.get("jira_solution") or _proposed_solution(plan))
    )
    await deps.repository.save_plan(plan)
    await deps.repository.update_incident(
        state["incident_id"],
        plan_id=plan.plan_id,
        selected_hypothesis=plan.selected_hypothesis,
        proposed_solution=proposed_solution,
    )
    await deps.repository.update_run(
        state["run_id"],
        plan_id=plan.plan_id,
        selected_hypothesis=plan.selected_hypothesis,
        proposed_solution=proposed_solution,
        **_agent_run_changes(deps),
    )
    return {
        "plan_id": plan.plan_id,
        "selected_hypothesis": plan.selected_hypothesis,
        "proposed_solution": proposed_solution,
        "current_step": "plan_repair",
    }


async def human_approval(state: RepairWorkflowState, deps: WorkflowDependencies) -> dict[str, Any]:
    await _progress(state, deps, "human_approval")
    if not state.get("require_approval"):
        return {"approval_granted": True, "current_step": "human_approval"}
    decision = state.get("approval_granted")
    if decision is None and deps.use_interrupts:
        from langgraph.types import interrupt

        response = interrupt(
            {
                "run_id": state["run_id"],
                "plan_id": state["plan_id"],
                "question": "Approve this repair plan?",
            }
        )
        decision = bool(response)
    if decision is None:
        await deps.repository.update_run(
            state["run_id"],
            status="waiting_for_approval",
            current_step="human_approval",
            approval_status="pending",
        )
        await deps.repository.update_incident(
            state["incident_id"], status="waiting_for_approval"
        )
        return {
            "status": "waiting_for_approval",
            "current_step": "human_approval",
            "approval_granted": None,
        }
    if not decision:
        await deps.repository.update_run(
            state["run_id"], status="denied", approval_status="denied"
        )
        return {"status": "denied", "approval_granted": False}
    await deps.repository.update_run(
        state["run_id"], status="approved", approval_status="approved"
    )
    return {"status": "approved", "approval_granted": True}


def route_after_approval(state: RepairWorkflowState) -> Literal["patch", "finalize", "wait"]:
    if state.get("status") == "waiting_for_approval":
        return "wait"
    if state.get("approval_granted") is False:
        return "finalize"
    return "patch"


async def patch_repository(state: RepairWorkflowState, deps: WorkflowDependencies) -> dict[str, Any]:
    await _progress(state, deps, "patch_repository")
    branch_name = state.get("branch_name") or _branch_name(state)
    await deps.github.create_branch(
        repo_path=Path(state["clone_path"]),
        base_branch=state.get("repo_branch") or "main",
        branch_name=branch_name,
    )
    result = await deps.patch_agent.apply_patch(
        repo_path=Path(state["clone_path"]),
        module_path=state.get("module_path"),
        branch_name=branch_name,
        selected_hypothesis=state["selected_hypothesis"],
        service_name=state.get("service_name"),
        error_name=state.get("error_name"),
    )
    await deps.repository.update_run(state["run_id"], branch_name=result.branch_name)
    await deps.repository.update_incident(
        state["incident_id"], branch_name=result.branch_name
    )
    return {
        "branch_name": result.branch_name,
        "patch_summary": redact_sensitive_text(result.summary),
        "changed_files": result.changed_files,
        "no_patch_needed": result.no_patch_needed,
        "current_step": "patch_repository",
    }


async def run_tests(state: RepairWorkflowState, deps: WorkflowDependencies) -> dict[str, Any]:
    await _progress(state, deps, "run_tests")
    result = await deps.test_runner.run_tests(
        repo_path=Path(state["clone_path"]),
        module_path=state.get("module_path"),
        command=state["test_cmd"],
    )
    return {
        "attempt_count": state.get("attempt_count", 0) + 1,
        "resolved_test_executable": result.resolved_executable,
        "test_exit_code": result.exit_code,
        "test_output": redact_sensitive_text(result.output)[-20_000:],
        "test_stdout_tail": redact_sensitive_text(result.stdout_tail)[-20_000:],
        "test_stderr_tail": redact_sensitive_text(result.stderr_tail)[-20_000:],
        "tests_passed": result.passed,
        "current_step": "run_tests",
    }


async def reflect_or_retry(state: RepairWorkflowState, deps: WorkflowDependencies) -> dict[str, Any]:
    await _progress(state, deps, "reflect_or_retry")
    if state["tests_passed"] and state.get("no_patch_needed"):
        reflection = (
            "Repository already contains the reviewed fix; tests passed and no pull request is needed."
        )
    elif state["tests_passed"]:
        reflection = "Tests passed; proceed to commit and pull-request metadata."
    elif state["attempt_count"] < deps.max_attempts:
        reflection = "Tests failed; retry the selected hypothesis with the captured test evidence."
    else:
        reflection = "Tests still fail after the allowed attempts; stop without opening a pull request."
    await deps.repository.save_attempt(
        RepairAttemptRecord(
            repair_attempt_id=f"attempt_{uuid4().hex}",
            run_id=state["run_id"],
            attempt_number=state["attempt_count"],
            hypothesis=state["selected_hypothesis"],
            patch_summary=state["patch_summary"],
            changed_files=state.get("changed_files", []),
            test_command=state["test_cmd"],
            resolved_test_executable=state.get("resolved_test_executable"),
            test_exit_code=state["test_exit_code"],
            test_output=state["test_output"],
            test_stdout_tail=state.get("test_stdout_tail", ""),
            test_stderr_tail=state.get("test_stderr_tail", ""),
            tests_passed=state["tests_passed"],
            reflection=reflection,
        )
    )
    return {"reflection": reflection, "current_step": "reflect_or_retry"}


async def review_pr(
    state: RepairWorkflowState, deps: WorkflowDependencies
) -> dict[str, Any]:
    await _progress(state, deps, "review_pr")
    planning_output = deps.planner.last_outputs.get("RepairPlanningAgent", {})
    try:
        output = await deps.planner.review_pr(
            run_id=state["run_id"],
            service_name=state.get("service_name") or "service",
            jira_key=state.get("jira_key") or "not-created",
            branch_name=state.get("branch_name") or "not-created",
            patch_summary=state.get("patch_summary") or "No patch summary",
            changed_files=state.get("changed_files", []),
            test_command=state.get("test_cmd") or "not run",
            test_result=(
                f"exit_code={state.get('test_exit_code')}; passed={state.get('tests_passed')}; "
                f"output={redact_sensitive_text(state.get('test_output', ''))[-4_000:]}"
            ),
            risk_notes=[str(item) for item in planning_output.get("risk_notes", [])],
            selected_hypothesis=state.get("selected_hypothesis") or "not selected",
            planning_output=planning_output,
            tracing=deps.tracing,
        )
    except LangStackAgentError as exc:
        await _record_agent_failure(state, deps, exc)
        raise
    changes = {
        "review_pr_output": output.model_dump(mode="json"),
        "current_step": "review_pr",
    }
    await deps.repository.update_run(state["run_id"], **_agent_run_changes(deps))
    return changes


def route_after_review(
    state: RepairWorkflowState,
) -> Literal["commit", "finalize"]:
    if state.get("tests_passed") and not state.get("no_patch_needed"):
        return "commit"
    return "finalize"


def route_after_reflection(
    state: RepairWorkflowState, max_attempts: int
) -> Literal["retry", "commit", "finalize"]:
    if state.get("tests_passed"):
        if state.get("no_patch_needed"):
            return "finalize"
        return "commit"
    if state.get("execution_mode") == "real":
        return "finalize"
    if state.get("attempt_count", 0) < max_attempts:
        return "retry"
    return "finalize"


async def commit_and_open_pr(
    state: RepairWorkflowState, deps: WorkflowDependencies
) -> dict[str, Any]:
    await _progress(state, deps, "commit_and_open_pr")
    config = EffectiveRepairConfig.model_validate(state["effective_config"])
    if state.get("dry_run") and (
        config.github.mode == "real" or config.repair.execution_mode == "real"
    ):
        return {
            "current_step": "commit_and_open_pr",
            "reflection": "Dry run completed after patch and tests; commit, push, and pull request were skipped.",
        }
    if not config.github.create_pull_request:
        return {
            "current_step": "commit_and_open_pr",
            "reflection": "Pull-request creation disabled by effective configuration.",
        }
    review = state.get("review_pr_output", {})
    pull_request_title = redact_sensitive_text(
        str(review.get("pr_title") or f"Repair {state['error_name']}")
    )
    checklist = "\n".join(
        f"- [ ] {redact_sensitive_text(str(item))}"
        for item in review.get("review_checklist", [])
    )
    result = await deps.github.commit_and_open_pull_request(
        repo_url=state["repo_url"],
        repo_path=Path(state["clone_path"]),
        base_branch=state.get("repo_branch") or "main",
        branch_name=state["branch_name"],
        title=pull_request_title,
        body=(
            f"{redact_sensitive_text(str(review.get('pr_summary') or 'Human review is required.'))}\n\n"
            f"Hypothesis: {state['selected_hypothesis']}\n"
            f"Tests: {state['test_cmd']}\n\n{checklist}"
        ),
        changed_files=state.get("changed_files", []),
    )
    await deps.repository.save_pull_request(
        PullRequestMetadata(
            pull_request_id=f"pr_{uuid4().hex}",
            incident_id=state["incident_id"],
            run_id=state["run_id"],
            repo_url=state["repo_url"],
            branch_name=result.branch_name,
            base_branch=state.get("repo_branch") or "main",
            title=pull_request_title,
            commit_sha=result.commit_sha,
            pull_request_url=result.pull_request_url,
        )
    )
    if config.jira.update_pr_link:
        await deps.jira.update_ticket_with_pull_request(
            jira_key=state["jira_key"],
            pull_request_url=result.pull_request_url,
        )
        await deps.repository.update_jira_ticket_with_pull_request(
            state["jira_key"], result.pull_request_url
        )
    changes = {
        "branch_name": result.branch_name,
        "commit_sha": result.commit_sha,
        "pull_request_url": result.pull_request_url,
    }
    await deps.repository.update_run(state["run_id"], **changes)
    await deps.repository.update_incident(state["incident_id"], **changes)
    return {**changes, "current_step": "commit_and_open_pr"}


async def finalize_run(state: RepairWorkflowState, deps: WorkflowDependencies) -> dict[str, Any]:
    if state.get("status") == "denied":
        status = "denied"
    elif state.get("tests_passed") is False:
        status = "failed"
    else:
        status = "completed"
    changes = {
        "status": status,
        "current_step": "finalize_run",
        "langsmith_trace_url": state.get("langsmith_trace_url"),
    }
    await deps.repository.update_run(state["run_id"], **changes)
    await deps.repository.update_incident(
        state["incident_id"],
        status=status,
        langsmith_trace_url=state.get("langsmith_trace_url"),
    )
    return changes


async def _progress(
    state: RepairWorkflowState, deps: WorkflowDependencies, step: str
) -> None:
    await deps.repository.update_run(
        state["run_id"], status="running", current_step=step
    )


def _bind(function: Any, dependencies: WorkflowDependencies) -> Any:
    async def bound(state: RepairWorkflowState) -> dict[str, Any]:
        return await function(state, dependencies)

    bound.__name__ = function.__name__
    return bound


def _branch_name(state: RepairWorkflowState) -> str:
    return build_repair_branch_name(
        jira_key=state.get("jira_key"),
        service_name=state.get("service_name"),
        error_name=state.get("error_name"),
        description=state.get("description"),
        run_id=state["run_id"],
    )


def build_repair_branch_name(
    *,
    jira_key: str | None,
    service_name: str | None,
    error_name: str | None,
    description: str | None,
    run_id: str,
) -> str:
    """Build a bounded Git ref from persisted ticket and incident metadata."""

    service = _branch_slug(service_name or "service", max_length=48) or "service"
    key_candidate = (jira_key or "").strip().upper()
    safe_key = (
        key_candidate
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}-[1-9][0-9]{0,11}", key_candidate)
        else None
    )
    if safe_key:
        error_slug = _short_error_slug(error_name, description)
        branch = f"bugfix/{safe_key}-{service}-{error_slug}"
    else:
        suffix = re.sub(r"[^a-z0-9]+", "", run_id.lower())[-12:] or "run"
        branch = f"bugfix/{service}-{suffix}"
    branch = branch[:180].rstrip("/.-")
    if (
        not re.fullmatch(r"bugfix/[A-Za-z0-9._-]+", branch)
        or ".." in branch
        or branch.endswith(("/", "."))
    ):
        raise ValueError("Generated repair branch is unsafe")
    return branch


def _short_error_slug(error_name: str | None, description: str | None) -> str:
    source = (error_name or description or "repair").strip()
    source = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", source)
    parts = [
        part
        for part in re.sub(r"[^A-Za-z0-9]+", "-", source).strip("-").split("-")
        if part.lower() not in {"exception", "error"}
    ]
    return _branch_slug("-".join(parts) or "repair", max_length=48) or "repair"


def _branch_slug(value: str, *, max_length: int) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-._")
    while ".." in slug:
        slug = slug.replace("..", ".")
    return slug[:max_length].rstrip("-._")


def _proposed_solution(plan: Any) -> str:
    edits = "; ".join(plan.planned_edits) or "Apply the smallest evidence-led code change."
    tests = "; ".join(plan.planned_tests) or "Run the configured regression tests."
    return redact_sensitive_text(
        f"Address the selected hypothesis: {plan.selected_hypothesis}. "
        f"Planned change: {edits} Verification: {tests}"
    )


def _effective_config(
    state: RepairWorkflowState,
    deps: WorkflowDependencies,
    *,
    repo_config: Any | None,
) -> EffectiveRepairConfig:
    default_config = (
        load_config_file(deps.default_config_path)
        if deps.default_config_path.is_file()
        else None
    )
    env_settings = RepairBotSettings.from_env(dotenv_path=None)
    return merge_effective_config(
        state.get("operator_overrides", {}),
        repo_config,
        default_config,
        env_settings,
    )


async def _record_agent_failure(
    state: RepairWorkflowState,
    deps: WorkflowDependencies,
    exc: LangStackAgentError,
) -> None:
    await deps.repository.update_run(
        state["run_id"],
        status="failed",
        last_error=redact_sensitive_text(str(exc)),
        **_agent_run_changes(deps),
    )
    await deps.repository.update_incident(state["incident_id"], status="failed")


def _configure_planner(
    config: EffectiveRepairConfig,
    deps: WorkflowDependencies,
    *,
    allow_api: bool,
) -> None:
    if config.prompts.planner_mode == "heuristic":
        if isinstance(deps.planner, LangChainStructuredPlanner):
            deps.planner = HeuristicStructuredPlanner()
        return
    if not allow_api:
        raise RuntimeError("OpenAI planner mode requires --allow-api")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OpenAI planner mode requires OPENAI_API_KEY")
    if config.model.provider != "openai" or not config.model.model.startswith("openai:"):
        raise RuntimeError("OpenAI planner mode requires an openai-prefixed model")
    if isinstance(deps.planner, LangChainStructuredPlanner):
        return
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI planner mode requires the installed LangChain OpenAI adapter"
        ) from exc
    model_name = config.model.model.split(":", 1)[1]
    deps.planner = LangChainStructuredPlanner(
        ChatOpenAI(model=model_name, max_retries=0),
        config.prompts,
    )


def _agent_run_changes(deps: WorkflowDependencies) -> dict[str, Any]:
    return {
        "agents_enabled": True,
        "agent_count": len(AGENT_NAMES),
        "agent_names": list(AGENT_NAMES),
        "openai_api_invoked": deps.planner.openai_invocations > 0,
        "langchain_used": deps.planner.langchain_used,
        "langgraph_used": True,
        "rendered_agent_inputs": dict(deps.planner.last_rendered_inputs),
        "agent_outputs": dict(deps.planner.last_outputs),
    }


def _configure_github_client(
    config: EffectiveRepairConfig,
    deps: WorkflowDependencies,
    *,
    allow_network: bool,
) -> None:
    if config.github.mode == "fake":
        if not isinstance(deps.github, FakeGitHubClient):
            deps.github = build_github_client("fake")
        return
    if not allow_network:
        raise RuntimeError(
            "Real GitHub mode requires an explicit runtime network allowance"
        )
    if not isinstance(deps.github, RealGitHubClient):
        deps.github = build_github_client("real")


def _configure_adapters(
    config: EffectiveRepairConfig,
    deps: WorkflowDependencies,
    *,
    allow_network: bool,
) -> None:
    _configure_github_client(config, deps, allow_network=allow_network)
    if config.repair.execution_mode == "real":
        if not allow_network:
            raise RuntimeError(
                "Real patch/test mode requires an explicit runtime network allowance"
            )
        if not isinstance(deps.patch_agent, FilePatchAgent):
            deps.patch_agent = FilePatchAgent()
        if not isinstance(deps.test_runner, SubprocessTestRunner):
            deps.test_runner = SubprocessTestRunner()
    else:
        if isinstance(deps.patch_agent, FilePatchAgent):
            deps.patch_agent = FakePatchAgent()
        if isinstance(deps.test_runner, SubprocessTestRunner):
            deps.test_runner = FakeTestRunner()
    if config.jira.mode == "fake":
        if not isinstance(deps.jira, FakeJiraClient):
            deps.jira = build_jira_client("fake")
        return
    if not allow_network:
        raise RuntimeError("Real Jira mode requires an explicit runtime network allowance")
    if not isinstance(deps.jira, RealJiraClient):
        deps.jira = build_jira_client("real")


def _config_source(
    state: RepairWorkflowState,
    repo_config_path: Path | None,
    deps: WorkflowDependencies,
) -> str:
    sources: list[str] = []
    if state.get("operator_overrides"):
        sources.append("operator-request")
    if repo_config_path is not None:
        sources.append(f"repo:{repo_config_path.name}")
    if deps.default_config_path.is_file():
        sources.append(f"default:{deps.default_config_path.name}")
    sources.append("environment/fallback")
    return " > ".join(sources)


__all__ = [
    "UploadedLogIngestionService",
    "WorkflowDependencies",
    "build_repair_graph",
    "default_workflow_dependencies",
    "initial_workflow_state",
    "load_effective_project_config",
    "route_after_approval",
    "route_after_reflection",
]
