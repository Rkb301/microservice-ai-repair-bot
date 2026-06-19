"""High-level analysis orchestration for log summary and Jira analysis commands."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from .config import RepairBotSettings
from .log_pipeline import (
    build_agent1_payload,
    cluster_errors_deterministically,
    ingest_log_file,
    summarize_entries,
)
from .models import ErrorCluster, LogSummary, ServicesConfig
from .service_config import resolve_service_logs
from .storage import RepairBotStorage


def run_log_analysis(
    *,
    services_config: ServicesConfig,
    logs_dir: str | Path | None,
    explicit_logs: dict[str, str] | None,
    out_path: str | Path | None,
    db_path: str | Path = ".repairbot/repairbot.db",
    run_id: str | None = None,
    mode: str = "analyze-only",
    settings: RepairBotSettings | None = None,
) -> dict:
    settings = settings or RepairBotSettings.from_env()
    run_id = run_id or f"run-{uuid4().hex[:8]}"
    storage = RepairBotStorage(db_path)
    storage.create_run(run_id, mode)
    log_paths = resolve_service_logs(
        services_config=services_config,
        logs_dir=logs_dir,
        explicit_logs=explicit_logs,
    )
    entries_by_service = {}
    summaries: list[LogSummary] = []
    for service in services_config.services:
        path = log_paths.get(service.name)
        if not path or not path.exists():
            entries_by_service[service.name] = []
            summaries.append(LogSummary(run_id=run_id, service_name=service.name))
            continue
        entries = list(ingest_log_file(run_id=run_id, service_name=service.name, path=path))
        entries_by_service[service.name] = entries
        storage.store_log_entries(entries)
        summary = summarize_entries(run_id, service.name, entries)
        summaries.append(summary)
        storage.store_summary(summary)

    summary_by_service = {summary.service_name: summary for summary in summaries}
    clusters = cluster_errors_deterministically(
        run_id=run_id,
        entries_by_service=entries_by_service,
        summaries=summary_by_service,
        services=services_config.by_name(),
        settings=settings,
    )
    for cluster in clusters:
        storage.store_cluster(run_id, cluster)
    artifact = {
        "run_id": run_id,
        "summaries": [summary.model_dump() for summary in summaries],
        "clusters": [cluster.model_dump() for cluster in clusters],
        "agent1_payload": build_agent1_payload(clusters, summaries, settings),
    }
    if out_path:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    storage.close()
    return artifact


def load_analysis(path: str | Path) -> tuple[str, list[ErrorCluster]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return raw.get("run_id", "run-unknown"), [
        ErrorCluster.model_validate(item) for item in raw.get("clusters", [])
    ]
