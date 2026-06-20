"""Deterministic backend-only demo helpers shared by the CLI and tests."""

from __future__ import annotations

from pathlib import Path

from .ingestion import UploadedLogIngestionService
from .persistence import InMemoryRepairPlatformRepository
from .platform_config import load_config_file
from .platform_models import RunDetail, UploadedRepairRequest
from .run_detail import build_run_detail
from .workflow import (
    build_repair_graph,
    default_workflow_dependencies,
    initial_workflow_state,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_SAMPLE_FILES = {
    "user-service": PROJECT_ROOT / "examples" / "demo-logs" / "user-service-missing-profile.log",
    "order-service": PROJECT_ROOT / "examples" / "demo-logs" / "order-service-null-customer.log",
    "payment-service": PROJECT_ROOT / "examples" / "demo-logs" / "payment-service-invalid-charge.log",
}
DEMO_REPOSITORY_URLS = {
    service: f"https://github.com/Rkb301/{service}" for service in DEMO_SAMPLE_FILES
}


def load_demo_log(sample: str) -> tuple[Path, str]:
    path = DEMO_SAMPLE_FILES.get(sample)
    if path is None:
        choices = ", ".join(sorted(DEMO_SAMPLE_FILES))
        raise ValueError(f"Unknown demo sample {sample!r}; choose one of: {choices}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"Demo sample is empty: {path.name}")
    return path, text


async def execute_demo_sample(
    *,
    sample: str,
    repo_url: str | None = None,
    target_branch: str = "main",
    workspace_root: Path = Path(".workspace"),
    config_path: Path | None = None,
) -> RunDetail:
    sample_path, log_text = load_demo_log(sample)
    repository = InMemoryRepairPlatformRepository()
    workspace = workspace_root.resolve()
    dependencies = default_workflow_dependencies(
        repository=repository,
        workspace_root=workspace,
    )
    if config_path is not None:
        load_config_file(config_path)
        dependencies.default_config_path = config_path.resolve()
    ingestion = UploadedLogIngestionService(repository, workspace)
    run = await ingestion.create_run(
        request=UploadedRepairRequest(
            repo_url=repo_url or DEMO_REPOSITORY_URLS[sample],
            repo_branch=target_branch,
            service_name=sample,
            require_approval=False,
        ),
        filename=sample_path.name,
        content=log_text.encode("utf-8"),
    )
    graph = build_repair_graph(dependencies)
    await graph.ainvoke(await initial_workflow_state(repository, run))
    completed = await repository.get_run(run.run_id)
    if completed is None:
        raise RuntimeError("Demo run was not persisted")
    return await build_run_detail(repository, completed)


__all__ = [
    "DEMO_REPOSITORY_URLS",
    "DEMO_SAMPLE_FILES",
    "execute_demo_sample",
    "load_demo_log",
]
