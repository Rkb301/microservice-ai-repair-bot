"""FastAPI endpoints for uploaded-log repair runs."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ValidationError

from ..ingestion import UploadedLogIngestionService
from ..integrations.github import FakeGitHubClient
from ..integrations.jira import FakeJiraClient
from ..persistence import (
    InMemoryRepairPlatformRepository,
    MongoRepairPlatformRepository,
    RepairPlatformRepository,
)
from ..platform_models import (
    DemoRepairRequest,
    IncidentRecord,
    RepairRunRecord,
    RunDetail,
    UploadedRepairRequest,
)
from ..redaction import redact_sensitive_text
from ..run_detail import build_run_detail
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


class StorageHealthResponse(BaseModel):
    mode: str
    healthy: bool
    database: str | None = None


class IntegrationModesResponse(BaseModel):
    jira_mode: str
    github_mode: str


class HealthResponse(BaseModel):
    status: str
    storage: StorageHealthResponse
    integrations: IntegrationModesResponse


def create_app(
    *,
    repository: RepairPlatformRepository | None = None,
    dependencies: WorkflowDependencies | None = None,
    workspace_root: Path | None = None,
) -> FastAPI:
    owns_repository = repository is None and dependencies is None
    if repository is not None:
        repo = repository
    elif dependencies is not None:
        repo = dependencies.repository
    else:
        repo = _repository_from_env()
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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            if isinstance(repo, MongoRepairPlatformRepository):
                try:
                    if not await repo.ping():
                        raise RuntimeError("unhealthy ping response")
                    await repo.ensure_indexes()
                except Exception:
                    raise RuntimeError(
                        "MongoDB startup health/index initialization failed"
                    ) from None
            yield
        finally:
            close_jira = getattr(deps.jira, "aclose", None)
            if callable(close_jira):
                await close_jira()
            close_github = getattr(deps.github, "aclose", None)
            if callable(close_github):
                await close_github()
            if owns_repository:
                close_repository = getattr(repo, "close", None)
                if callable(close_repository):
                    close_repository()

    api = FastAPI(
        title="RepairBot Uploaded-Log Orchestrator",
        version="0.3.0",
        description="Operator API for redacted log uploads and external GitHub repository repair runs.",
        lifespan=lifespan,
    )
    api.state.repository = repo
    api.state.dependencies = deps

    @api.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        if isinstance(repo, MongoRepairPlatformRepository):
            try:
                healthy = await repo.ping()
            except Exception:
                healthy = False
            storage = StorageHealthResponse(
                mode="mongodb",
                healthy=healthy,
                database=redact_sensitive_text(repo.database_name),
            )
        else:
            storage = StorageHealthResponse(mode="memory", healthy=True)
        integrations = IntegrationModesResponse(
            jira_mode="fake" if isinstance(deps.jira, FakeJiraClient) else "real",
            github_mode="fake" if isinstance(deps.github, FakeGitHubClient) else "real",
        )
        return HealthResponse(
            status="ok" if storage.healthy else "degraded",
            storage=storage,
            integrations=integrations,
        )

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
        github_mode: Annotated[str | None, Form()] = None,
        execution_mode: Annotated[str | None, Form()] = None,
        allow_network: Annotated[bool, Form()] = False,
        dry_run: Annotated[bool | None, Form()] = None,
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
                github_mode=github_mode,
                execution_mode=execution_mode,
                allow_network=allow_network,
                dry_run=dry_run,
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
        try:
            return await build_run_detail(repo, run)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Incident not found") from exc

    @api.post(
        "/api/demo/runs",
        response_model=RunDetail,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_demo_run(request: DemoRepairRequest) -> RunDetail:
        """Run the complete deterministic backend flow from JSON log text."""

        try:
            repair_request = UploadedRepairRequest(
                repo_url=request.github_repo_url,
                repo_branch=request.target_branch,
                service_name=request.service_name,
                test_cmd=request.test_command,
                jira_project=request.jira_project,
                require_approval=request.require_approval,
            )
            run = await ingestion.create_run(
                request=repair_request,
                filename=f"{request.service_name or 'service'}-demo.log",
                content=request.log_text.encode("utf-8"),
            )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=_safe_validation_message(exc)) from exc
        completed = await _run_graph(repo, graph, run)
        return await build_run_detail(repo, completed)

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
    if not uri and not database_name:
        return InMemoryRepairPlatformRepository()
    return MongoRepairPlatformRepository.from_env()


def _safe_validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(error["msg"] for error in exc.errors())
    return redact_sensitive_text(str(exc))[:500]


app = create_app()

