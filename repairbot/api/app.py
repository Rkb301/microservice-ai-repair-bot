"""FastAPI endpoints for uploaded-log repair runs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ValidationError

from ..architecture import project_mvp_metadata
from ..ingestion import UploadedLogIngestionService
from ..persistence import (
    InMemoryRepairPlatformRepository,
    MongoRepairPlatformRepository,
    RepairPlatformRepository,
)
from ..platform_models import IncidentRecord, RepairRunRecord, RunDetail, UploadedRepairRequest
from ..redaction import redact_sensitive_text
from ..tracing import LangSmithTracingConfig
from ..workflow import (
    WorkflowDependencies,
    build_repair_graph,
    default_workflow_dependencies,
    initial_workflow_state,
)


class RunCreatedResponse(BaseModel):
    run_id: str
    status: str
    current_step: str


def create_app(
    *,
    repository: RepairPlatformRepository | None = None,
    dependencies: WorkflowDependencies | None = None,
    workspace_root: Path | None = None,
) -> FastAPI:
    repo = repository or (dependencies.repository if dependencies else _repository_from_env())
    root = (workspace_root or Path(".workspace")).resolve()
    if dependencies is None:
        deps = default_workflow_dependencies(repository=repo, workspace_root=root)
        deps.tracing = LangSmithTracingConfig.from_env()
    else:
        deps = dependencies
    deps.repository = repo
    deps.workspace_root = root
    ingestion = UploadedLogIngestionService(repo, root)
    graph = build_repair_graph(deps)

    api = FastAPI(
        title="RepairBot Uploaded-Log Orchestrator",
        version="0.2.0",
        description="Operator API for redacted log uploads and external GitHub repository repair runs.",
    )
    api.state.repository = repo
    api.state.dependencies = deps

    @api.post(
        "/api/repair-runs",
        response_model=RunCreatedResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_repair_run(
        log_file: Annotated[UploadFile, File(description="Error log file")],
        repo_url: Annotated[str, Form()],
        branch: Annotated[str | None, Form()] = None,
        service_name: Annotated[str | None, Form()] = None,
        module_path: Annotated[str | None, Form()] = None,
        language: Annotated[str | None, Form()] = None,
        build_system: Annotated[str | None, Form()] = None,
        test_cmd: Annotated[str | None, Form()] = None,
        model_name: Annotated[str | None, Form()] = None,
        jira_project: Annotated[str | None, Form()] = None,
        require_approval: Annotated[bool | None, Form()] = None,
    ) -> RunCreatedResponse:
        try:
            request = UploadedRepairRequest(
                repo_url=repo_url,
                repo_branch=branch,
                service_name=service_name,
                module_path=module_path,
                language=language,
                build_system=build_system,
                test_cmd=test_cmd,
                model_name=model_name,
                jira_project=jira_project,
                require_approval=require_approval,
            )
            content = await log_file.read()
            run = await ingestion.create_run(
                request=request,
                filename=log_file.filename or "uploaded-error.log",
                content=content,
            )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=_safe_validation_message(exc)) from exc
        completed = await _run_graph(repo, graph, run)
        return RunCreatedResponse(
            run_id=completed.run_id,
            status=completed.status,
            current_step=completed.current_step,
        )

    @api.get("/api/repair-runs", response_model=list[RepairRunRecord])
    async def list_repair_runs() -> list[RepairRunRecord]:
        return await repo.list_runs()

    @api.get("/api/repair-runs/{run_id}", response_model=RunDetail)
    async def get_repair_run(run_id: str) -> RunDetail:
        run = await _require_run(repo, run_id)
        incident = await repo.get_incident(run.incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        return RunDetail(
            run=run,
            incident=incident,
            plan=await repo.get_plan_for_run(run_id),
            attempts=await repo.list_attempts(run_id),
            metadata={
                **project_mvp_metadata(),
                "config_source": run.config_source,
                "effective_config": run.effective_config,
            },
        )

    @api.post("/api/repair-runs/{run_id}/approve-plan", response_model=RunCreatedResponse)
    async def approve_plan(run_id: str) -> RunCreatedResponse:
        run = await _require_run(repo, run_id)
        if not run.require_approval:
            raise HTTPException(status_code=409, detail="This run does not require plan approval")
        if run.status != "waiting_for_approval":
            raise HTTPException(status_code=409, detail="Run is not waiting for plan approval")
        run = await repo.update_run(
            run_id,
            status="approved",
            approval_status="approved",
            current_step="human_approval",
        )
        completed = await _run_graph(repo, graph, run)
        return RunCreatedResponse(
            run_id=completed.run_id,
            status=completed.status,
            current_step=completed.current_step,
        )

    @api.post("/api/repair-runs/{run_id}/cancel", response_model=RunCreatedResponse)
    async def cancel_run(run_id: str) -> RunCreatedResponse:
        run = await _require_run(repo, run_id)
        if run.status in {"completed", "failed", "cancelled", "denied"}:
            raise HTTPException(status_code=409, detail="Run is already in a terminal state")
        run = await repo.update_run(
            run_id, status="cancelled", current_step="cancelled"
        )
        await repo.update_incident(run.incident_id, status="cancelled")
        return RunCreatedResponse(
            run_id=run.run_id, status=run.status, current_step=run.current_step
        )

    @api.get("/api/incidents", response_model=list[IncidentRecord])
    async def list_incidents() -> list[IncidentRecord]:
        return await repo.list_incidents()

    @api.get("/api/incidents/{incident_id}", response_model=IncidentRecord)
    async def get_incident(incident_id: str) -> IncidentRecord:
        incident = await repo.get_incident(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        return incident

    return api


async def _run_graph(
    repository: RepairPlatformRepository,
    graph: object,
    run: RepairRunRecord,
) -> RepairRunRecord:
    state = await initial_workflow_state(repository, run)
    try:
        await graph.ainvoke(state)  # type: ignore[attr-defined]
    except Exception as exc:
        safe_error = redact_sensitive_text(str(exc))[:1000]
        await repository.update_run(
            run.run_id,
            status="failed",
            current_step="failed",
            last_error=safe_error,
        )
        await repository.update_incident(run.incident_id, status="failed")
        raise HTTPException(status_code=500, detail="Repair workflow failed") from exc
    completed = await repository.get_run(run.run_id)
    if completed is None:
        raise HTTPException(status_code=500, detail="Repair run disappeared")
    return completed


async def _require_run(
    repository: RepairPlatformRepository, run_id: str
) -> RepairRunRecord:
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Repair run not found")
    return run


def _repository_from_env() -> RepairPlatformRepository:
    uri = os.getenv("MONGODB_URI")
    database_name = os.getenv("MONGODB_DB")
    if uri and database_name:
        return MongoRepairPlatformRepository.from_uri(uri, database_name)
    return InMemoryRepairPlatformRepository()


def _safe_validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(error["msg"] for error in exc.errors())
    return redact_sensitive_text(str(exc))[:500]


app = create_app()

