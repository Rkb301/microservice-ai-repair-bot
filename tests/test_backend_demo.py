from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess

import httpx
from typer.testing import CliRunner

from repairbot.api.app import create_app
from repairbot.cli import app as cli_app
from repairbot.demo import DEMO_REPOSITORY_URLS, DEMO_SAMPLE_FILES
from repairbot.integrations.execution import FakePatchAgent, FakeTestRunner
from repairbot.integrations.github import FakeGitHubClient
from repairbot.integrations.jira import FakeJiraClient
from repairbot.persistence import InMemoryRepairPlatformRepository
from repairbot.tracing import LangSmithTracingConfig
from repairbot.workflow import WorkflowDependencies


EXPECTED_ERRORS = {
    "user-service": "NullPointerException",
    "order-service": "NullPointerException",
    "payment-service": "IllegalArgumentException",
}


def test_demo_samples_exist_and_are_non_empty() -> None:
    assert set(DEMO_SAMPLE_FILES) == {
        "user-service",
        "order-service",
        "payment-service",
    }
    for service, path in DEMO_SAMPLE_FILES.items():
        text = path.read_text(encoding="utf-8")
        assert text.strip(), service
        assert service in text
        assert "trace_id=demo-" in text


def test_demo_api_returns_complete_runs_for_all_three_services(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = InMemoryRepairPlatformRepository()
        dependencies = _dependencies(repository, tmp_path)
        app = create_app(
            repository=repository,
            dependencies=dependencies,
            workspace_root=tmp_path / ".workspace",
        )
        transport = httpx.ASGITransport(app=app)
        created_run_ids: list[str] = []
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for service, sample_path in DEMO_SAMPLE_FILES.items():
                response = await client.post(
                    "/api/demo/runs",
                    json={
                        "log_text": sample_path.read_text(encoding="utf-8"),
                        "github_repo_url": DEMO_REPOSITORY_URLS[service],
                        "service_name": service,
                        "target_branch": "main",
                    },
                )
                assert response.status_code == 201, response.text
                body = response.json()
                created_run_ids.append(body["run"]["run_id"])
                assert {
                    "run",
                    "incident",
                    "agents",
                    "jira",
                    "pull_request",
                    "effective_config",
                    "trace",
                    "metadata",
                } <= body.keys()
                assert body["run"]["status"] == "completed"
                assert body["run"]["service_name"] == service
                assert body["incident"]["error_name"] == EXPECTED_ERRORS[service]
                assert body["incident"]["proposed_solution"]
                assert set(body["agents"]) == {
                    "log_analysis",
                    "repair_planning",
                    "code_repair",
                }
                assert {
                    output["agent_name"] for output in body["agents"].values()
                } == {
                    "Log Analysis Agent",
                    "Repair Planning Agent",
                    "Code Repair Agent",
                }
                assert all(
                    output["status"] == "completed"
                    for output in body["agents"].values()
                )
                assert body["jira"]["jira_key"].startswith("DEMO-")
                assert body["jira"]["jira_ticket_url"].startswith(
                    "https://jira.local/browse/DEMO-"
                )
                assert body["jira"]["title"]
                assert body["jira"]["description"]
                assert body["jira"]["solution"] == body["incident"]["proposed_solution"]
                assert body["jira"]["pull_request_url"] == body["pull_request"][
                    "pull_request_url"
                ]
                assert body["pull_request"]["repo_url"] == DEMO_REPOSITORY_URLS[service]
                assert body["pull_request"]["base_branch"] == "main"
                assert body["pull_request"]["branch_name"].startswith("ai-fix/")
                assert body["pull_request"]["title"].startswith("Repair ")
                assert body["pull_request"]["pull_request_url"] == (
                    f"https://github.local/Rkb301/{service}/pull/1"
                )
                assert body["effective_config"]["model"]["model"] == "openai:gpt-5-mini"
                assert body["effective_config"]["model"]["allowed_models"] == [
                    "openai:gpt-5-mini"
                ]
                assert body["effective_config"]["jira"]["mode"] == "fake"
                assert body["effective_config"]["jira"]["project_key"] == "DEMO"
                assert body["effective_config"]["jira"]["issue_type"] == "Bug"
                assert body["effective_config"]["github"]["mode"] == "fake"
                assert body["effective_config"]["repair"]["test_command"] == "mvn test"
                assert body["effective_config"]["repair"]["require_plan_approval"] is False
                assert body["effective_config"]["langsmith"]["enabled"] is False
                assert body["effective_config"]["langsmith"]["project"] == (
                    "microservice-ai-repair-bot"
                )
                assert body["trace"]["local_trace_id"].startswith("trace_")
                assert body["trace"]["local_trace_url"] == (
                    f"langsmith://local-demo/{body['run']['run_id']}"
                )
                assert body["trace"]["langsmith_active"] is False
                assert body["trace"]["langsmith_project"] == "microservice-ai-repair-bot"
                assert body["trace"]["langsmith_tags"] == ["mvp-review"]
                assert body["metadata"]["external_calls_performed"] is False
                assert body["metadata"]["config_source"].startswith("operator-request")
                assert body["metadata"]["max_total_repositories"] == 4
                assert body["metadata"]["max_microservice_repositories"] == 3
                assert body["metadata"]["agent_output_count"] == 3

                detail = await client.get(
                    f"/api/repair-runs/{body['run']['run_id']}"
                )
                assert detail.status_code == 200
                assert detail.json() == body

            listed = await client.get("/api/repair-runs")
            assert listed.status_code == 200
            assert created_run_ids == [
                item["run_id"] for item in reversed(listed.json())
            ]

            cancel = await client.post(
                f"/api/repair-runs/{created_run_ids[0]}/cancel"
            )
            assert cancel.status_code == 409

    asyncio.run(scenario())


def test_demo_cli_help_and_local_run(tmp_path: Path) -> None:
    runner = CliRunner()
    help_result = runner.invoke(cli_app, ["demo-run", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "--sample" in help_result.output
    assert "--repo-url" in help_result.output

    result = runner.invoke(
        cli_app,
        [
            "demo-run",
            "--sample",
            "user-service",
            "--repo-url",
            DEMO_REPOSITORY_URLS["user-service"],
            "--workspace",
            str(tmp_path / ".workspace"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "run ID: run_" in result.output
    assert "status: completed" in result.output
    assert "error name: NullPointerException" in result.output
    assert "proposed solution:" in result.output
    assert "https://jira.local/browse/DEMO-1001" in result.output
    assert "https://github.local/Rkb301/user-service/pull/1" in result.output
    assert "model=openai:gpt-5-mini" in result.output
    assert "langsmith://local-demo/run_" in result.output


def test_tracked_source_avoids_disallowed_wording() -> None:
    disallowed = "men" + "tor"
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
        text=True,
    )
    for relative in completed.stdout.splitlines():
        assert disallowed not in relative.lower(), relative
        path = Path(relative)
        if path.suffix.lower() in {
            ".py",
            ".md",
            ".yaml",
            ".yml",
            ".txt",
            ".log",
        }:
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert disallowed not in text.lower(), relative


def _dependencies(
    repository: InMemoryRepairPlatformRepository,
    tmp_path: Path,
) -> WorkflowDependencies:
    return WorkflowDependencies(
        repository=repository,
        workspace_root=(tmp_path / ".workspace").resolve(),
        github=FakeGitHubClient(),
        jira=FakeJiraClient(),
        patch_agent=FakePatchAgent(),
        test_runner=FakeTestRunner(),
        tracing=LangSmithTracingConfig(False, False, None),
    )
