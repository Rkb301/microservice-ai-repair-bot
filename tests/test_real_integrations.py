from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from repairbot.api.app import _repository_from_env, create_app
from repairbot.cli import app as cli_app
from repairbot.integrations.execution import FakePatchAgent, FakeTestRunner
from repairbot.integrations.github import FakeGitHubClient
from repairbot.integrations.jira import (
    JiraIntegrationError,
    RealJiraClient,
    build_jira_backlink_comment,
    build_jira_client,
    build_jira_issue_payload,
)
from repairbot.persistence import (
    MONGODB_INDEX_SPECS,
    InMemoryRepairPlatformRepository,
    MongoRepairPlatformRepository,
)
from repairbot.platform_models import RepairAttemptRecord
from repairbot.tracing import LangSmithTracingConfig
from repairbot.workflow import WorkflowDependencies


class FakeMongoCollection:
    def __init__(self) -> None:
        self.indexes: list[dict] = []
        self.documents: list[dict] = []

    async def create_index(self, keys, **options):
        self.indexes.append({"keys": keys, **options})
        return options["name"]

    async def replace_one(self, query, document, *, upsert):
        self.documents.append(
            {"query": query, "document": document, "upsert": upsert}
        )


class FakeMongoDatabase:
    name = "repairbot_test"

    def __init__(self) -> None:
        self.collections = defaultdict(FakeMongoCollection)
        self.commands: list[str] = []

    def __getitem__(self, name: str) -> FakeMongoCollection:
        return self.collections[name]

    async def command(self, name: str) -> dict[str, int]:
        self.commands.append(name)
        return {"ok": 1}


def test_mongodb_ping_indexes_and_redacted_storage() -> None:
    async def scenario() -> None:
        database = FakeMongoDatabase()
        repository = MongoRepairPlatformRepository(database)
        assert await repository.ping() is True
        names = await repository.ensure_indexes()
        assert names == [spec[3] for spec in MONGODB_INDEX_SPECS]
        assert len(names) == 17
        assert sum(
            len(collection.indexes)
            for collection in database.collections.values()
        ) == 17
        unique_names = {
            options["name"]
            for collection in database.collections.values()
            for options in collection.indexes
            if options["unique"]
        }
        assert len(unique_names) == 9

        attempt = RepairAttemptRecord(
            repair_attempt_id="attempt_safe",
            run_id="run_safe",
            attempt_number=1,
            hypothesis="Guard the invalid input",
            patch_summary="Add validation",
            test_command="mvn test",
            test_exit_code=1,
            test_output=(
                "MONGODB_URI=mongodb://db-user:db-password@db.example/repairbot "
                "API_TOKEN=sample-value"
            ),
            tests_passed=False,
        )
        await repository.save_attempt(attempt)
        stored = database.collections["repair_attempts"].documents[0]["document"]
        assert "db-password" not in stored["test_output"]
        assert "sample-value" not in stored["test_output"]
        assert "<redacted>" in stored["test_output"]

    asyncio.run(scenario())


def test_mongodb_env_selection_fails_closed_on_partial_settings(monkeypatch) -> None:
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.delenv("MONGODB_DB", raising=False)
    with pytest.raises(RuntimeError, match="MONGODB_DB"):
        _repository_from_env()


def test_health_endpoint_reports_memory_without_secrets(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = InMemoryRepairPlatformRepository()
        dependencies = WorkflowDependencies(
            repository=repository,
            workspace_root=(tmp_path / ".workspace").resolve(),
            github=FakeGitHubClient(),
            jira=build_jira_client("fake"),
            patch_agent=FakePatchAgent(),
            test_runner=FakeTestRunner(),
            tracing=LangSmithTracingConfig(False, False, None),
        )
        app = create_app(
            repository=repository,
            dependencies=dependencies,
            workspace_root=tmp_path / ".workspace",
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "storage": {
                "mode": "memory",
                "healthy": True,
                "database": None,
            },
            "integrations": {"jira_mode": "fake", "github_mode": "fake"},
        }
        serialized = response.text.lower()
        assert "api_token" not in serialized
        assert "mongodb_uri" not in serialized

    asyncio.run(scenario())


def test_api_lifespan_initializes_mongodb_and_health_reports_database(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = FakeMongoDatabase()
        repository = MongoRepairPlatformRepository(database)
        dependencies = WorkflowDependencies(
            repository=repository,
            workspace_root=(tmp_path / ".workspace").resolve(),
            github=FakeGitHubClient(),
            jira=build_jira_client("fake"),
            patch_agent=FakePatchAgent(),
            test_runner=FakeTestRunner(),
            tracing=LangSmithTracingConfig(False, False, None),
        )
        app = create_app(
            repository=repository,
            dependencies=dependencies,
            workspace_root=tmp_path / ".workspace",
        )
        async with app.router.lifespan_context(app):
            assert database.commands == ["ping"]
            assert sum(
                len(collection.indexes)
                for collection in database.collections.values()
            ) == 17
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.get("/api/health")
            assert response.status_code == 200
            assert response.json()["storage"] == {
                "mode": "mongodb",
                "healthy": True,
                "database": "repairbot_test",
            }
        assert database.commands == ["ping", "ping"]

    asyncio.run(scenario())


def test_jira_payload_builders_use_cloud_document_format_and_redact() -> None:
    issue = build_jira_issue_payload(
        project_key="DEMO",
        issue_type="Bug",
        title="User profile failure",
        description="Profile lookup failed API_TOKEN=sample-value",
        solution="Guard missing profile and return safe JSON",
        incident_id="inc_123",
        service_name="user-service",
        repo_url="https://github.com/Rkb301/user-service",
        run_id="run_123",
    )
    assert issue["fields"]["project"] == {"key": "DEMO"}
    assert issue["fields"]["issuetype"] == {"name": "Bug"}
    document = issue["fields"]["description"]
    assert document["type"] == "doc"
    assert document["version"] == 1
    text = " ".join(
        paragraph["content"][0]["text"] for paragraph in document["content"]
    )
    assert "sample-value" not in text
    assert "Affected service: user-service" in text
    assert "Repository: https://github.com/Rkb301/user-service" in text
    assert "Run ID: run_123" in text

    comment = build_jira_backlink_comment(
        "https://github.local/Rkb301/user-service/pull/1"
    )
    assert comment["body"]["content"][0]["content"][0]["text"] == (
        "Pull request: https://github.local/Rkb301/user-service/pull/1"
    )
    with pytest.raises(ValueError, match="credential-free"):
        build_jira_backlink_comment("https://user:password@example.com/pull/1")


def test_real_jira_client_creates_issue_updates_backlink_and_checks_health() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/rest/api/3/issue":
                return httpx.Response(201, json={"key": "DEMO-4321"})
            if request.url.path.endswith("/comment"):
                return httpx.Response(201, json={"id": "10001"})
            if request.url.path == "/rest/api/3/myself":
                return httpx.Response(200, json={"accountId": "local-test"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://jira.example.com"
        ) as http_client:
            client = RealJiraClient(
                base_url="https://jira.example.com",
                email="engineer@example.com",
                api_token="unit-test-value",
                http_client=http_client,
            )
            created = await client.create_or_record_ticket(
                project_key="DEMO",
                issue_type="Bug",
                title="Automated repair test",
                description="A profile lookup failed",
                solution="Add a missing-value guard",
                incident_id="inc_456",
                service_name="user-service",
                repo_url="https://github.com/Rkb301/user-service",
                run_id="run_456",
            )
            assert created.jira_key == "DEMO-4321"
            assert created.jira_ticket_url == (
                "https://jira.example.com/browse/DEMO-4321"
            )
            pr_url = "https://github.com/Rkb301/user-service/pull/1"
            await client.update_ticket_with_pull_request(created.jira_key, pr_url)
            await client.update_ticket_with_pull_request(created.jira_key, pr_url)
            assert await client.health_check() is True
        assert [request.url.path for request in requests] == [
            "/rest/api/3/issue",
            "/rest/api/3/issue/DEMO-4321/comment",
            "/rest/api/3/myself",
        ]

    asyncio.run(scenario())


def test_real_jira_errors_do_not_echo_response_or_credentials() -> None:
    async def scenario() -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                text="api_token=server-leaked-value",
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://jira.example.com"
        ) as http_client:
            client = RealJiraClient(
                base_url="https://jira.example.com",
                email="engineer@example.com",
                api_token="client-secret-value",
                http_client=http_client,
            )
            with pytest.raises(JiraIntegrationError) as caught:
                await client.health_check()
        message = str(caught.value)
        assert "server-leaked-value" not in message
        assert "client-secret-value" not in message
        assert "HTTP 401" in message

    asyncio.run(scenario())


def test_real_jira_factory_requires_complete_env_and_does_not_connect() -> None:
    client = build_jira_client(
        "real",
        environ={
            "JIRA_BASE_URL": "https://jira.example.com",
            "JIRA_EMAIL": "engineer@example.com",
            "JIRA_API_TOKEN": "factory-test-value",
        },
    )
    assert isinstance(client, RealJiraClient)
    asyncio.run(client.aclose())


def test_storage_and_jira_cli_checks_are_safe_without_credentials(
    monkeypatch,
) -> None:
    for name in (
        "MONGODB_URI",
        "MONGODB_DB",
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "JIRA_PROJECT_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    runner = CliRunner()
    storage_help = runner.invoke(cli_app, ["storage-check", "--help"])
    jira_help = runner.invoke(cli_app, ["jira-check", "--help"])
    assert storage_help.exit_code == 0
    assert jira_help.exit_code == 0
    assert "--allow-network" in jira_help.output

    storage = runner.invoke(cli_app, ["storage-check"])
    assert storage.exit_code == 0
    assert "storage mode: memory" in storage.output
    assert "healthy: true" in storage.output

    jira = runner.invoke(cli_app, ["jira-check"])
    assert jira.exit_code == 1
    assert "jira mode: unavailable" in jira.output
    assert "JIRA_API_TOKEN" in jira.output


def test_jira_cli_config_check_does_not_call_network_or_print_credentials(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "private-engineer@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "private-token-value")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "DEMO")
    result = CliRunner().invoke(cli_app, ["jira-check"])
    assert result.exit_code == 0, result.output
    assert "network check: skipped" in result.output
    assert "https://jira.example.com" in result.output
    assert "project key: DEMO" in result.output
    assert "private-engineer" not in result.output
    assert "private-token-value" not in result.output
