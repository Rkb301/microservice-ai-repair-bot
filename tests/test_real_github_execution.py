from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from repairbot.cli import app as cli_app
from repairbot.integrations.execution import FilePatchAgent, SubprocessTestRunner
from repairbot.integrations.github import (
    GitHubIntegrationError,
    RealGitHubClient,
    build_github_client,
    build_github_pull_request_payload,
    parse_github_repo_url,
    validate_repair_branch,
)
from repairbot.platform_config import ConfigSafetyError
from repairbot.platform_config import RepairBotProjectConfig
from repairbot.integrations.execution import FakePatchAgent, FakeTestRunner
from repairbot.integrations.github import FakeGitHubClient
from repairbot.integrations.jira import FakeJiraClient
from repairbot.tracing import LangSmithTracingConfig
from repairbot.tools.safety import SafetyError
from repairbot.workflow import WorkflowDependencies, commit_and_open_pr
from repairbot.openai_planner_check import run_planner_check


@pytest.mark.parametrize(
    ("service_name", "filename", "source", "expected"),
    [
        (
            "user-service",
            "UserServiceApp.java",
            """public class UserServiceApp {
    public static String getProfile(String userId) {
        String profile = null;
        return \"{\\\"profile\\\":\\\"\" + profile.toUpperCase() + \"\\\"}\";
    }
}
""",
            "profile == null",
        ),
        (
            "order-service",
            "OrderServiceApp.java",
            """public class OrderServiceApp {
    record Customer(String id) {}
    static void handleOrders(Object exchange) {
            write(exchange, 201, createOrder(null));
    }
    static void write(Object exchange, int code, String value) {}
    public static String createOrder(Customer customer) {
        String customerId = customer.id();
        return customerId;
    }
}
""",
            "customer == null",
        ),
        (
            "payment-service",
            "PaymentServiceApp.java",
            """public class PaymentServiceApp {
    static void handlePayments(Object exchange) {
            write(exchange, 200, charge(-1));
    }
    static void write(Object exchange, int code, String value) {}
    public static String charge(int amount) {
        if (amount == 0) {
            throw new IllegalArgumentException();
        }
        return String.valueOf(amount);
    }
}
""",
            "amount <= 0",
        ),
    ],
)
def test_file_patch_agent_applies_reviewed_service_patches(
    tmp_path: Path,
    service_name: str,
    filename: str,
    source: str,
    expected: str,
) -> None:
    target = tmp_path / "src" / "main" / "java" / filename
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")

    async def scenario() -> None:
        result = await FilePatchAgent().apply_patch(
            repo_path=tmp_path,
            module_path=None,
            branch_name=f"ai-fix/{service_name}/test",
            selected_hypothesis="The input needs validation",
            service_name=service_name,
        )
        assert result.changed_files == [target.relative_to(tmp_path).as_posix()]
        assert expected in target.read_text(encoding="utf-8")

    asyncio.run(scenario())


def test_file_patch_agent_rejects_path_traversal(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.java"
    outside.write_text("unchanged", encoding="utf-8")
    with pytest.raises(SafetyError, match="escapes repository"):
        FilePatchAgent().apply_changes(
            tmp_path,
            {Path("..") / outside.name: "changed"},
        )
    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_subprocess_test_runner_captures_pass_and_fail_without_shell(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    runner = SubprocessTestRunner(timeout_seconds=60)

    async def scenario() -> None:
        passed = await runner.run_tests(
            repo_path=tmp_path,
            module_path=None,
            command="python -m pytest -q test_sample.py",
        )
        assert passed.passed is True
        assert "1 passed" in passed.output

        test_file.write_text("def test_sample():\n    assert False\n", encoding="utf-8")
        failed = await runner.run_tests(
            repo_path=tmp_path,
            module_path=None,
            command="python -m pytest -q test_sample.py",
        )
        assert failed.passed is False
        assert failed.exit_code != 0
        assert "1 failed" in failed.output

    asyncio.run(scenario())

    with pytest.raises(ConfigSafetyError, match="control operators"):
        asyncio.run(
            runner.run_tests(
                repo_path=tmp_path,
                module_path=None,
                command="python -m pytest && curl example.com",
            )
        )


def test_github_payload_and_validation_are_network_free() -> None:
    payload = build_github_pull_request_payload(
        title="Repair missing profile",
        body="Validated with mvn test",
        head_branch="ai-fix/user-service/test",
        base_branch="main",
    )
    assert payload == {
        "title": "Repair missing profile",
        "body": "Validated with mvn test",
        "head": "ai-fix/user-service/test",
        "base": "main",
    }
    assert parse_github_repo_url("https://github.com/Rkb301/user-service.git") == (
        "Rkb301",
        "user-service",
    )
    with pytest.raises(ValueError, match="credential-free"):
        parse_github_repo_url("https://user:token@github.com/Rkb301/user-service")
    with pytest.raises((ValueError, SafetyError)):
        validate_repair_branch("main")
    with pytest.raises((ValueError, SafetyError)):
        validate_repair_branch("feature/unreviewed")
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        build_github_client("real", environ={})


def test_real_github_local_git_and_mock_pr_flow(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "-b", "main", str(seed))
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(
        seed,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "seed",
    )
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", "--branch", "main", str(origin), str(clone))

    async def scenario() -> None:
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                201,
                json={"html_url": "https://github.com/example/test-repo/pull/1"},
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://api.github.com",
        ) as http_client:
            client = RealGitHubClient(
                token="unit-test-token",
                http_client=http_client,
            )
            branch = "ai-fix/user-service/unit-test"
            await client.create_branch(
                repo_path=clone,
                base_branch="main",
                branch_name=branch,
            )
            changed = clone / "repairbot-integration" / "result.txt"
            changed.parent.mkdir()
            changed.write_text("verified\n", encoding="utf-8")
            result = await client.commit_and_open_pull_request(
                repo_url="https://github.com/example/test-repo",
                repo_path=clone,
                base_branch="main",
                branch_name=branch,
                title="Repair integration path",
                body="Local Git with mocked REST",
                changed_files=["repairbot-integration/result.txt"],
            )
        assert result.branch_name == branch
        assert len(result.commit_sha) == 40
        assert result.pull_request_url == "https://github.com/example/test-repo/pull/1"
        assert len(captured) == 1
        assert captured[0].url.path == "/repos/example/test-repo/pulls"
        assert captured[0].headers["Authorization"] == "Bearer unit-test-token"
        assert _git(clone, "status", "--porcelain") == ""
        assert _git(origin, "branch", "--list", branch).strip()

    asyncio.run(scenario())


def test_ai_planner_check_is_explicit_and_safe_without_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runner = CliRunner()
    help_result = runner.invoke(cli_app, ["ai-planner-check", "--help"])
    assert help_result.exit_code == 0
    assert "--allow-api" in help_result.output

    skipped = runner.invoke(cli_app, ["ai-planner-check"])
    assert skipped.exit_code == 0
    assert "planner check: skipped" in skipped.output

    missing = runner.invoke(cli_app, ["ai-planner-check", "--allow-api"])
    assert missing.exit_code == 1
    assert "OPENAI_API_KEY is not set" in missing.output


def test_openai_planner_check_structured_path_with_mock_model(monkeypatch) -> None:
    class FakeStructuredModel:
        async def ainvoke(self, prompt: str):
            assert "missing null guard" in prompt
            return {
                "hypothesis": "The value lacks a null guard",
                "proposed_change": "Add a narrow validation check",
                "test_command": "mvn test",
            }

    class FakeChatOpenAI:
        def __init__(self, *, model: str, max_retries: int):
            assert model == "gpt-5-mini"
            assert max_retries == 0

        def with_structured_output(self, schema):
            assert schema.__name__ == "PlannerCheckOutput"
            return FakeStructuredModel()

    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(ChatOpenAI=FakeChatOpenAI),
    )
    assert asyncio.run(run_planner_check("openai:gpt-5-mini")) == (
        "openai:gpt-5-mini"
    )


def test_repair_run_requires_explicit_network_allowance() -> None:
    result = CliRunner().invoke(
        cli_app,
        [
            "repair-run",
            "--log-file",
            "examples/demo-logs/user-service-missing-profile.log",
            "--repo-url",
            "https://github.com/Rkb301/user-service",
            "--service-name",
            "user-service",
            "--real-git",
        ],
    )
    assert result.exit_code == 2
    assert "--real-git requires --allow-network" in result.output


def test_real_dry_run_skips_commit_push_pr_and_jira_backlink(tmp_path: Path) -> None:
    class RecordingGitHub(FakeGitHubClient):
        def __init__(self) -> None:
            self.pr_calls = 0

        async def commit_and_open_pull_request(self, **kwargs):
            self.pr_calls += 1
            return await super().commit_and_open_pull_request(**kwargs)

    class ProgressRepository:
        async def update_run(self, run_id: str, **changes):
            return {"run_id": run_id, **changes}

    async def scenario() -> None:
        repository = ProgressRepository()
        github = RecordingGitHub()
        jira = FakeJiraClient()
        dependencies = WorkflowDependencies(
            repository=repository,
            workspace_root=tmp_path,
            github=github,
            jira=jira,
            patch_agent=FakePatchAgent(),
            test_runner=FakeTestRunner(),
            tracing=LangSmithTracingConfig(False, False, None),
        )
        config = RepairBotProjectConfig.model_validate(
            {
                "github": {"mode": "real", "create_pull_request": True},
                "repair": {"execution_mode": "real", "dry_run_default": True},
            }
        )
        result = await commit_and_open_pr(
            {
                "run_id": "run_dry",
                "incident_id": "inc_dry",
                "repo_url": "https://github.com/Rkb301/user-service",
                "repo_branch": "main",
                "branch_name": "ai-fix/user-service/dry-run",
                "error_name": "NullPointerException",
                "selected_hypothesis": "Guard missing profile",
                "test_cmd": "mvn test",
                "changed_files": ["src/UserServiceApp.java"],
                "dry_run": True,
                "effective_config": config.model_dump(mode="json"),
            },
            dependencies,
        )
        assert github.pr_calls == 0
        assert jira.tickets == {}
        assert "commit, push, and pull request were skipped" in result["reflection"]

    asyncio.run(scenario())


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()
