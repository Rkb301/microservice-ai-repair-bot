from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from repairbot.architecture import (
    CONCEPTUAL_AGENTS,
    MAX_MICROSERVICE_REPOSITORIES,
    MAX_TOTAL_REPOSITORIES,
    MVP_MODEL,
    project_mvp_metadata,
)
from repairbot.api.app import create_app
from repairbot.config import DEFAULT_MODEL
from repairbot.ingestion import UploadedLogIngestionService
from repairbot.integrations.execution import FakePatchAgent, FakeTestRunner
from repairbot.integrations.github import FakeGitHubClient
from repairbot.integrations.jira import FakeJiraClient
from repairbot.langstack import build_langchain_tools
from repairbot.persistence import (
    COLLECTION_NAMES,
    InMemoryRepairPlatformRepository,
    MongoRepairPlatformRepository,
)
from repairbot.platform_models import IncidentRecord, JiraTicketMetadata, UploadedRepairRequest
from repairbot.service_config import load_services_config
from repairbot.tracing import LangSmithTracingConfig
from repairbot.workflow import (
    WorkflowDependencies,
    build_repair_graph,
    initial_workflow_state,
)


LOG_TEXT = """2026-06-18 ERROR order-service request failed
java.lang.NullPointerException: order customer was missing
    at com.example.order.OrderService.create(OrderService.java:42)
"""


def test_uploaded_log_creation_stores_run_incident_and_redacted_log(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = InMemoryRepairPlatformRepository()
        ingestion = UploadedLogIngestionService(repository, tmp_path / ".workspace")
        run = await ingestion.create_run(
            request=UploadedRepairRequest(
                repo_url="https://github.com/example/order-service",
                service_name="order-service",
                test_cmd="mvn test",
            ),
            filename="../../production.log",
            content=(LOG_TEXT + "\nDEMO_API_KEY=not-a-real-key").encode(),
        )

        stored_run = await repository.get_run(run.run_id)
        incident = await repository.get_incident(run.incident_id)
        upload = await repository.get_log_upload(run.uploaded_log_id)
        assert stored_run is not None
        assert incident is not None
        assert upload is not None
        assert incident.error_name == "NullPointerException"
        assert incident.description == "order customer was missing"
        assert incident.jira_ticket_url is None
        assert incident.pull_request_url is None
        assert Path(upload.storage_path).parent.name == "uploads"
        stored_text = Path(upload.storage_path).read_text(encoding="utf-8")
        assert "not-a-real-key" not in stored_text
        assert "DEMO_API_KEY=<redacted>" in stored_text

    asyncio.run(scenario())


def test_api_accepts_uploaded_log_and_github_url_without_external_calls(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = InMemoryRepairPlatformRepository()
        dependencies = _dependencies(repository, tmp_path)
        app = create_app(
            repository=repository,
            dependencies=dependencies,
            workspace_root=tmp_path / ".workspace",
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/repair-runs",
                files={"log_file": ("error.log", LOG_TEXT, "text/plain")},
                data={
                    "repo_url": "https://github.com/example/order-service",
                    "branch": "main",
                    "service_name": "order-service",
                    "test_cmd": "mvn test",
                    "jira_project": "OPS",
                    "require_approval": "false",
                },
            )

            assert response.status_code == 201, response.text
            payload = response.json()
            assert payload["run_id"].startswith("run_")
            assert payload["status"] == "completed"

            detail = await client.get(f"/api/repair-runs/{payload['run_id']}")
            assert detail.status_code == 200
            body = detail.json()
            assert body["incident"]["error_name"] == "NullPointerException"
            assert body["incident"]["proposed_solution"]
            assert body["incident"]["jira_ticket_url"].startswith("https://jira.local/")
            assert body["incident"]["pull_request_url"].startswith("https://github.local/")
            assert body["incident"]["repo_url"] == "https://github.com/example/order-service"
            assert body["run"]["proposed_solution"] == body["incident"]["proposed_solution"]
            assert body["run"]["selected_hypothesis"]
            assert {
                key: body["metadata"][key] for key in project_mvp_metadata()
            } == project_mvp_metadata()
            assert body["metadata"]["effective_config"]["model"]["model"] == MVP_MODEL
            assert body["metadata"]["config_source"].startswith("operator-request")
            assert body["run"]["current_step"] == "finalize_run"

    asyncio.run(scenario())


def test_plan_approval_endpoint_resumes_waiting_graph(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = InMemoryRepairPlatformRepository()
        app = create_app(
            repository=repository,
            dependencies=_dependencies(repository, tmp_path),
            workspace_root=tmp_path / ".workspace",
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/repair-runs",
                files={"log_file": ("error.log", LOG_TEXT, "text/plain")},
                data={
                    "repo_url": "https://github.com/example/order-service",
                    "require_approval": "true",
                },
            )
            assert created.status_code == 201, created.text
            assert created.json()["status"] == "waiting_for_approval"

            approved = await client.post(
                f"/api/repair-runs/{created.json()['run_id']}/approve-plan"
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "completed"

            detail = await client.get(
                f"/api/repair-runs/{created.json()['run_id']}"
            )
            assert detail.status_code == 200
            assert detail.json()["jira"]["pull_request_url"] == detail.json()[
                "pull_request"
            ]["pull_request_url"]

    asyncio.run(scenario())


def test_persistence_schema_has_required_collections_and_incident_fields() -> None:
    assert set(COLLECTION_NAMES) == {
        "repositories",
        "log_uploads",
        "incidents",
        "jira_tickets",
        "repair_plans",
        "repair_runs",
        "repair_attempts",
        "pull_requests",
    }
    assert MongoRepairPlatformRepository.collection_names == COLLECTION_NAMES
    fields = IncidentRecord.model_fields
    assert {
        "error_name",
        "description",
        "proposed_solution",
        "jira_ticket_url",
        "pull_request_url",
        "repo_url",
        "github_repo_url",
        "target_branch",
    } <= fields.keys()
    assert {
        "title",
        "description",
        "solution",
        "pull_request_url",
        "updated_at",
    } <= JiraTicketMetadata.model_fields.keys()


def test_langgraph_dry_run_completes_with_injected_fakes(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = InMemoryRepairPlatformRepository()
        ingestion = UploadedLogIngestionService(repository, tmp_path / ".workspace")
        run = await ingestion.create_run(
            request=UploadedRepairRequest(
                repo_url="https://github.com/example/order-service",
                require_approval=False,
            ),
            filename="error.log",
            content=LOG_TEXT.encode(),
        )
        dependencies = _dependencies(repository, tmp_path)
        graph = build_repair_graph(dependencies)
        result = await graph.ainvoke(await initial_workflow_state(repository, run))
        stored = await repository.get_run(run.run_id)

        assert result["status"] == "completed"
        assert result["tests_passed"] is True
        assert stored is not None
        assert stored.status == "completed"
        assert stored.plan_id
        assert stored.proposed_solution
        assert stored.jira_ticket_url
        assert stored.pull_request_url
        incident = await repository.get_incident(run.incident_id)
        assert incident is not None
        assert incident.proposed_solution == stored.proposed_solution
        jira = await repository.get_jira_ticket(next(iter(dependencies.jira.tickets)))
        assert jira is not None
        assert jira.title == "[example-service] NullPointerException"
        assert jira.description == "order customer was missing"
        assert jira.solution == stored.proposed_solution
        assert jira.pull_request_url == stored.pull_request_url
        fake_ticket = dependencies.jira.tickets[jira.jira_key]
        assert fake_ticket.pull_request_url == stored.pull_request_url
        assert len(await repository.list_attempts(run.run_id)) == 1

    asyncio.run(scenario())


def test_langsmith_config_is_present_but_disabled_without_key(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_PROJECT", "repairbot-tests")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    config = LangSmithTracingConfig.from_env()
    assert config.requested is True
    assert config.enabled is False
    assert config.project == "repairbot-tests"
    assert "API" not in repr(config)


def test_github_and_jira_are_injected_fake_clients(tmp_path: Path) -> None:
    repository = InMemoryRepairPlatformRepository()
    dependencies = _dependencies(repository, tmp_path)
    assert isinstance(dependencies.github, FakeGitHubClient)
    assert isinstance(dependencies.jira, FakeJiraClient)
    assert isinstance(dependencies.patch_agent, FakePatchAgent)
    assert isinstance(dependencies.test_runner, FakeTestRunner)


def test_fake_jira_stores_ticket_content_and_pr_backlink() -> None:
    async def scenario() -> None:
        jira = FakeJiraClient()
        created = await jira.create_or_record_ticket(
            project_key="OPS",
            title="Order service null customer",
            description="The create path dereferences a missing customer.",
            solution="Add a narrow validation guard and regression test.",
            incident_id="inc-project",
        )
        ticket = jira.tickets[created.jira_key]
        assert ticket.title == "Order service null customer"
        assert ticket.description == "The create path dereferences a missing customer."
        assert ticket.solution == "Add a narrow validation guard and regression test."
        assert ticket.pull_request_url is None

        await jira.update_ticket_with_pull_request(
            jira_key=created.jira_key,
            pull_request_url="https://github.local/example/order-service/pull/1",
        )
        assert ticket.pull_request_url == "https://github.local/example/order-service/pull/1"

    asyncio.run(scenario())


def test_project_mvp_constraints_are_executable_and_documented() -> None:
    assert MAX_TOTAL_REPOSITORIES == 4
    assert MAX_MICROSERVICE_REPOSITORIES == 3
    assert CONCEPTUAL_AGENTS == (
        "Log Analysis Agent",
        "Repair Planning Agent",
        "Code Repair Agent",
    )
    assert MVP_MODEL == DEFAULT_MODEL == "openai:gpt-5-mini"
    config = load_services_config("services.mvp-review.example.yaml")
    assert len(config.services) == 3
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "four repositories total" in readme
    assert "exactly three conceptual agents" in readme
    assert "five model" in readme
    assert "not implemented" in readme


def test_langchain_tool_wrappers_cover_platform_boundaries(tmp_path: Path) -> None:
    repository = InMemoryRepairPlatformRepository()
    dependencies = _dependencies(repository, tmp_path)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    tools = build_langchain_tools(
        github=dependencies.github,
        jira=dependencies.jira,
        patch_agent=dependencies.patch_agent,
        test_runner=dependencies.test_runner,
        repo_path=repo_path,
        repo_url="https://github.com/example/order-service",
        incident_id="inc_test",
    )
    assert {tool.name for tool in tools} == {
        "inspect_repository",
        "analyze_uploaded_log",
        "run_repository_tests",
        "record_jira_ticket",
        "apply_repair_patch",
        "open_github_pull_request",
    }


def _dependencies(
    repository: InMemoryRepairPlatformRepository, tmp_path: Path
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

