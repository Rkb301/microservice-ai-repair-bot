from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repairbot.cli import app
from repairbot.models import ErrorCluster, Hypothesis, JiraTicketRecord
from repairbot.service_config import (
    get_service_config,
    load_services_config,
    resolve_service_repo_path,
)
from repairbot.storage import RepairBotStorage


def test_jira_fix_dry_run_reports_failure_and_restores_service_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, service_dir, services_config = _make_inventory_repo(tmp_path)
    _seed_ticket(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("REPAIRBOT_MAX_FIX_ATTEMPTS", "1")
    original_source = _service_source(service_dir).read_text(encoding="utf-8")
    seen: dict[str, str] = {}

    def fake_agent(*, settings, context, task):  # noqa: ANN001
        del settings
        assert list((repo / ".tmp" / "microservice-ai-repair-bot" / "jira-fix").rglob("plan.json"))
        seen["repo_path"] = context.repo_path
        seen["task"] = task
        _service_source(Path(context.repo_path)).write_text(
            original_source.replace("return stock.getOrDefault(sku, 0);", "return -1;"),
            encoding="utf-8",
        )
        return "patched inventory lookup"

    monkeypatch.setattr("repairbot.agents.invoke_fix_agent", fake_agent)
    fail_cmd = (
        "python -c \"import sys; "
        "[print('line %03d' % i) for i in range(105)]; "
        "print('DEMO_API_KEY=not-a-real-key'); "
        "sys.exit(7)\""
    )

    result = CliRunner().invoke(
        app,
        [
            "jira-fix",
            "--ticket",
            "BUG-6106",
            "--repo",
            str(repo),
            "--service",
            "inventory-service",
            "--services-config",
            str(services_config),
            "--base-branch",
            "master",
            "--test-cmd",
            fail_cmd,
            "--plan-first",
            "--dry-run",
            "--allow-api",
        ],
    )

    assert result.exit_code == 1, result.stdout
    assert _service_source(service_dir).read_text(encoding="utf-8") == original_source
    assert _git(repo, "status", "--short") == ""
    assert Path(seen["repo_path"]) == service_dir.resolve()
    assert f"Focus first on this service source path: {service_dir.resolve()}" in seen["task"]
    assert "Selected structured hypothesis:" in seen["task"]
    assert "--- structured repair plan ---" in result.stdout
    assert "attempt 1: tests_passed=False" in result.stdout
    assert "failed repair attempt" in result.stdout
    assert "changed files:" in result.stdout
    assert "examples/toy-java-microservices/inventory-service/src/main/java/com/example/inventory/InventoryServiceApp.java" in result.stdout
    assert "git diff --stat:" in result.stdout
    assert "test command:" in result.stdout
    assert "exit code: 7" in result.stdout
    assert "last 100 lines of test output:" in result.stdout
    assert "line 104" in result.stdout
    assert "line 000" not in result.stdout
    assert "not-a-real-key" not in result.stdout
    assert "DEMO_API_KEY=<redacted>" in result.stdout
    assert "dry-run restore complete" in result.stdout

    artifact_lines = [
        line.split("artifacts: ", 1)[1]
        for line in result.stdout.splitlines()
        if line.startswith("artifacts: ")
    ]
    assert artifact_lines
    attempt_dir = Path(artifact_lines[0])
    assert (attempt_dir / "test-output.txt").exists()
    assert (attempt_dir / "diff.stat").exists()
    assert (attempt_dir / "diff.patch").exists()
    reflection = attempt_dir / "reflection.json"
    assert reflection.exists()
    assert (attempt_dir / "reflection.md").exists()
    reflection_data = json.loads(reflection.read_text(encoding="utf-8"))
    assert reflection_data["decision"] in {"try_next_hypothesis", "adjust_same_hypothesis"}
    assert reflection_data["post_test_reflection"]
    assert "not-a-real-key" not in _read_artifact_texts(repo)


def test_jira_fix_plan_only_creates_structured_artifacts_without_editing_or_agent_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, service_dir, services_config = _make_inventory_repo(tmp_path)
    raw_secret = "not-a-real-key"
    _seed_ticket(repo, summary=f"[inventory-service] bad stock lookup DEMO_API_KEY={raw_secret}")
    original_source = _service_source(service_dir).read_text(encoding="utf-8")
    called = False

    def fail_agent(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal called
        called = True
        raise AssertionError("plan-only must not call the repair agent")

    monkeypatch.setattr("repairbot.agents.invoke_fix_agent", fail_agent)
    result = CliRunner().invoke(
        app,
        [
            "jira-fix",
            "--ticket",
            "BUG-6106",
            "--repo",
            str(repo),
            "--service",
            "inventory-service",
            "--services-config",
            str(services_config),
            "--test-cmd",
            "python -c \"print('baseline ok')\"",
            "--plan-only",
            "--max-hypotheses",
            "5",
            "--allow-api",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert called is False
    assert _service_source(service_dir).read_text(encoding="utf-8") == original_source
    assert _git(repo, "status", "--short") == ""
    assert "plan-only complete" in result.stdout
    assert "--- structured repair plan ---" in result.stdout
    assert raw_secret not in result.stdout

    plan_path = next((repo / ".tmp" / "microservice-ai-repair-bot" / "jira-fix").rglob("plan.json"))
    plan_markdown = plan_path.with_suffix(".md")
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    assert data["selected_service"] == "inventory-service"
    assert data["service_root"] == str(service_dir.resolve())
    assert data["ticket_id"] == "BUG-6106"
    assert len(data["hypotheses"]) >= 3
    assert data["selected_hypothesis"]["hypothesis_id"].startswith("H")
    assert data["planned_tests"]
    assert data["safety_notes"]
    assert data["baseline_test_status"] == "exit_code=0"
    for hypothesis in data["hypotheses"]:
        assert {
            "hypothesis_id",
            "suspected_root_cause",
            "evidence",
            "proposed_code_change",
            "expected_test_impact",
            "risk_level",
            "confidence_score",
            "files_likely_to_change",
        }.issubset(hypothesis)
    assert plan_markdown.exists()
    assert raw_secret not in plan_path.read_text(encoding="utf-8")
    assert raw_secret not in plan_markdown.read_text(encoding="utf-8")
    assert "DEMO_API_KEY=<redacted>" in plan_path.read_text(encoding="utf-8")


def test_inventory_service_config_resolves_to_toy_service_path(tmp_path: Path) -> None:
    repo, service_dir, services_config = _make_inventory_repo(tmp_path)
    config = load_services_config(services_config)
    service = get_service_config(config, "inventory-service")
    assert resolve_service_repo_path(
        repo_root=repo,
        service=service,
        services_config_path=services_config,
    ) == service_dir.resolve()


def test_jira_fix_without_allow_api_does_not_invoke_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, services_config = _make_inventory_repo(tmp_path)
    _seed_ticket(repo)
    called = False

    def fail_agent(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal called
        called = True
        raise AssertionError("agent must not run without --allow-api")

    monkeypatch.setattr("repairbot.agents.invoke_fix_agent", fail_agent)
    result = CliRunner().invoke(
        app,
        [
            "jira-fix",
            "--ticket",
            "BUG-6106",
            "--repo",
            str(repo),
            "--service",
            "inventory-service",
            "--services-config",
            str(services_config),
        ],
    )

    assert result.exit_code == 2
    assert called is False
    assert _git(repo, "status", "--short") == ""


def _make_inventory_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    service_dir = repo / "examples" / "toy-java-microservices" / "inventory-service"
    service_dir.mkdir(parents=True)
    source = _service_source(service_dir)
    source.parent.mkdir(parents=True)
    source.write_text(
        "package com.example.inventory;\n"
        "import java.util.Map;\n"
        "public class InventoryServiceApp {\n"
        "  private final Map<String, Integer> stock = Map.of(\"sku-1\", 2);\n"
        "  public int stockFor(String sku) {\n"
        "    return stock.getOrDefault(sku, 0);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (service_dir / "pom.xml").write_text("<project />\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".repairbot/\nruns/\n.tmp/\n", encoding="utf-8")
    services_config = repo / "services.yaml"
    services_config.write_text(
        "services:\n"
        "  - name: inventory-service\n"
        "    application_name: inventory-api\n"
        "    repo_path: examples/toy-java-microservices/inventory-service\n"
        "    build_tool: maven\n"
        "    test_cmd: mvn test\n"
        "    source_roots:\n"
        "      - src/main/java\n"
        "      - src/test/java\n",
        encoding="utf-8",
    )
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo, service_dir, services_config


def _seed_ticket(repo: Path, summary: str = "[inventory-service] bad stock lookup") -> None:
    cluster = ErrorCluster(
        cluster_id="cluster-inventory-6106",
        service_name="inventory-service",
        application_name="inventory-api",
        suspected_root_service="inventory-service",
        error_type="IllegalArgumentException",
        class_name="InventoryServiceApp",
        method_name="stockFor",
        file_name="InventoryServiceApp.java",
        occurrence_count=3,
        hypotheses=[
            Hypothesis(
                rank=1,
                hypothesis="Inventory stock lookup mishandles missing SKU",
                evidence="Stored cluster points to InventoryServiceApp.stockFor",
                confidence=0.84,
            )
        ],
    )
    storage = RepairBotStorage(repo / ".repairbot" / "repairbot.db")
    storage.store_cluster("run-1", cluster)
    storage.store_jira_ticket(
        JiraTicketRecord(
            jira_key="BUG-6106",
            cluster_id=cluster.cluster_id,
            service_name=cluster.service_name,
            summary=summary,
            status="dry-run",
        )
    )
    storage.close()


def _service_source(service_dir: Path) -> Path:
    return service_dir / "src" / "main" / "java" / "com" / "example" / "inventory" / "InventoryServiceApp.java"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


def _read_artifact_texts(repo: Path) -> str:
    artifact_root = repo / ".tmp" / "microservice-ai-repair-bot"
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in artifact_root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".md", ".txt", ".stat", ".patch"}
    )
