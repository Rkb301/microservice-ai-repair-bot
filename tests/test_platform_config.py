from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from repairbot.config import RepairBotSettings
from repairbot.ingestion import UploadedLogIngestionService
from repairbot.integrations.execution import FakePatchAgent, FakeTestRunner
from repairbot.integrations.github import (
    FakeGitHubClient,
    build_github_client,
)
from repairbot.integrations.jira import FakeJiraClient, build_jira_client
from repairbot.persistence import InMemoryRepairPlatformRepository
from repairbot.platform_config import (
    ConfigSafetyError,
    JiraConfig,
    RepairBotProjectConfig,
    find_project_config_path,
    load_config_file,
    load_project_config,
    merge_effective_config,
)
from repairbot.platform_models import UploadedRepairRequest
from repairbot.tracing import LangSmithTracingConfig
from repairbot.workflow import WorkflowDependencies, build_repair_graph, initial_workflow_state


LOG_TEXT = """ERROR order-service request failed
java.lang.NullPointerException: customer was missing
    at com.example.order.OrderService.create(OrderService.java:42)
"""


def test_loads_repairbot_yaml_and_dot_repairbot_yaml(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    primary = repo / "repairbot.yaml"
    primary.write_text(
        "version: 1\nservice_name: primary-service\njira:\n  project_key: CFG\n",
        encoding="utf-8",
    )
    loaded = load_project_config(repo)
    assert loaded is not None
    assert loaded.service_name == "primary-service"
    assert loaded.jira.project_key == "CFG"
    assert find_project_config_path(repo) == primary.resolve()

    primary.unlink()
    hidden = repo / ".repairbot.yaml"
    hidden.write_text(
        "version: 1\nservice_name: hidden-service\n",
        encoding="utf-8",
    )
    loaded_hidden = load_project_config(repo)
    assert loaded_hidden is not None
    assert loaded_hidden.service_name == "hidden-service"
    assert find_project_config_path(repo) == hidden.resolve()


def test_effective_config_precedence_operator_repo_default_env() -> None:
    env = RepairBotSettings(
        model="openai:env-model",
        provider="openai",
        jira_project_key="ENV",
    )
    default = RepairBotProjectConfig.model_validate(
        {
            "service_name": "default-service",
            "model": {
                "provider": "openai",
                "model": "openai:gpt-5-mini",
                "allowed_models": ["openai:gpt-5-mini"],
            },
            "jira": {"project_key": "DEFAULT"},
            "repair": {"test_command": "mvn test", "require_plan_approval": True},
        }
    )
    repo = RepairBotProjectConfig.model_validate(
        {
            "service_name": "repo-service",
            "jira": {"project_key": "REPO"},
            "repair": {"test_command": "gradle test"},
        }
    )
    effective = merge_effective_config(
        {
            "service_name": "operator-service",
            "jira_project": "OPS",
            "require_approval": False,
        },
        repo,
        default,
        env,
    )
    assert effective.service_name == "operator-service"
    assert effective.jira.project_key == "OPS"
    assert effective.repair.require_plan_approval is False
    assert effective.repair.test_command == "gradle test"
    assert effective.model.model == "openai:gpt-5-mini"


def test_model_changes_via_allowlisted_config_and_disallowed_model_fails() -> None:
    configured = RepairBotProjectConfig.model_validate(
        {
            "model": {
                "provider": "openai",
                "model": "openai:gpt-5-nano",
                "allowed_models": ["openai:gpt-5-mini", "openai:gpt-5-nano"],
            }
        }
    )
    assert configured.model.model == "openai:gpt-5-nano"
    assert configured.model.model in configured.model.allowed_models

    with pytest.raises(ValidationError, match="allowed_models"):
        RepairBotProjectConfig.model_validate(
            {
                "model": {
                    "provider": "openai",
                    "model": "openai:unreviewed-model",
                    "allowed_models": ["openai:gpt-5-mini"],
                }
            }
        )


def test_repo_model_can_use_orchestrator_allowlist_without_copying_it(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "repairbot.yaml").write_text(
        "model:\n  provider: openai\n  model: openai:gpt-5-nano\n",
        encoding="utf-8",
    )
    repo_config = load_project_config(repo)
    default = RepairBotProjectConfig.model_validate(
        {
            "model": {
                "provider": "openai",
                "model": "openai:gpt-5-mini",
                "allowed_models": ["openai:gpt-5-mini", "openai:gpt-5-nano"],
            }
        }
    )
    effective = merge_effective_config(None, repo_config, default, None)
    assert effective.model.model == "openai:gpt-5-nano"


def test_fake_modes_and_pr_backlink_defaults_are_safe() -> None:
    config = RepairBotProjectConfig()
    assert config.model.model == "openai:gpt-5-mini"
    assert config.jira.mode == "fake"
    assert config.github.mode == "fake"
    assert config.jira.update_pr_link is True
    assert isinstance(build_jira_client(config.jira.mode), FakeJiraClient)
    assert isinstance(build_github_client(config.github.mode), FakeGitHubClient)


def test_real_modes_require_environment_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="JIRA_BASE_URL"):
        build_jira_client("real")
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        build_github_client("real")


def test_secret_looking_config_is_rejected_without_echoing_value(tmp_path: Path) -> None:
    secret_value = "sk-" + "x" * 24
    config_path = tmp_path / "repairbot.yaml"
    config_path.write_text(
        f"version: 1\napi_token: {secret_value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigSafetyError) as caught:
        load_config_file(config_path)
    assert secret_value not in str(caught.value)

    credential_url = tmp_path / "credential-url.yaml"
    credential_url.write_text(
        "version: 1\nservice_name: https://user:password@example.com/repo\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigSafetyError, match="Credential-bearing URL"):
        load_config_file(credential_url)


def test_unsafe_test_command_and_module_traversal_are_rejected() -> None:
    with pytest.raises(ValidationError, match="control operators"):
        RepairBotProjectConfig.model_validate(
            {"repair": {"test_command": "mvn test && curl example.com"}}
        )
    with pytest.raises(ValidationError, match="inside the cloned repository"):
        RepairBotProjectConfig.model_validate(
            {"repair": {"module_path": "../../outside"}}
        )


def test_repo_config_drives_workflow_templates_and_is_persisted(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = InMemoryRepairPlatformRepository()
        workspace = tmp_path / ".workspace"
        ingestion = UploadedLogIngestionService(repository, workspace)
        run = await ingestion.create_run(
            request=UploadedRepairRequest(
                repo_url="https://github.com/example/order-service"
            ),
            filename="error.log",
            content=LOG_TEXT.encode(),
        )
        github = ConfigWritingFakeGitHubClient(
            """version: 1
service_name: configured-order
model:
  provider: openai
  model: openai:gpt-5-mini
  allowed_models: [openai:gpt-5-mini]
  max_hypotheses: 2
  max_attempts: 3
jira:
  mode: fake
  project_key: CFG
  issue_type: Bug
  title_template: "CFG {service_name}: {error_name}"
  description_template: "Observed: {description}"
  solution_template: "Configured fix: {proposed_solution}"
  update_pr_link: false
github:
  mode: fake
  default_base_branch: develop
  create_pull_request: true
repair:
  require_plan_approval: false
  dry_run_default: true
  test_command: gradle test
  build_system: gradle
  language: java
langsmith:
  enabled: false
  project: configured-repairs
  tags: [mvp-review, repo-config]
"""
        )
        deps = WorkflowDependencies(
            repository=repository,
            workspace_root=workspace.resolve(),
            github=github,
            jira=FakeJiraClient(),
            patch_agent=FakePatchAgent(),
            test_runner=FakeTestRunner(),
            tracing=LangSmithTracingConfig(False, False, None),
        )
        graph = build_repair_graph(deps)
        result = await graph.ainvoke(await initial_workflow_state(repository, run))
        stored = await repository.get_run(run.run_id)
        assert stored is not None
        assert result["status"] == "completed"
        assert "repo:repairbot.yaml" in stored.config_source
        assert stored.model_name == "openai:gpt-5-mini"
        assert stored.jira_project == "CFG"
        assert stored.jira_mode == "fake"
        assert stored.github_mode == "fake"
        assert stored.require_approval is False
        assert stored.test_cmd == "gradle test"
        assert stored.target_branch == "develop"
        assert stored.effective_config["model"]["allowed_models"] == [
            "openai:gpt-5-mini"
        ]
        assert deps.max_attempts == 3
        assert deps.planner.model_name == "openai:gpt-5-mini"
        assert deps.planner.max_hypotheses == 2
        plan = await repository.get_plan_for_run(run.run_id)
        assert plan is not None
        assert len(plan.hypotheses) == 2
        jira_key = next(iter(deps.jira.tickets))
        fake_ticket = deps.jira.tickets[jira_key]
        assert fake_ticket.title == "CFG configured-order: NullPointerException"
        assert fake_ticket.description == "Observed: customer was missing"
        assert fake_ticket.solution.startswith("Configured fix:")
        assert fake_ticket.pull_request_url is None
        persisted_ticket = await repository.get_jira_ticket(jira_key)
        assert persisted_ticket is not None
        assert persisted_ticket.pull_request_url is None
        assert stored.pull_request_url is not None

    asyncio.run(scenario())


def test_unknown_jira_template_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Invalid Jira template"):
        JiraConfig(title_template="{unknown_field}")


class ConfigWritingFakeGitHubClient(FakeGitHubClient):
    def __init__(self, config_text: str) -> None:
        self.config_text = config_text

    async def clone_repository(
        self, repo_url: str, branch: str, destination: Path
    ) -> Path:
        prepared = await super().clone_repository(repo_url, branch, destination)
        (prepared / "repairbot.yaml").write_text(self.config_text, encoding="utf-8")
        return prepared

