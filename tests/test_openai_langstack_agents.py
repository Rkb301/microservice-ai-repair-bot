from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from repairbot.cli import app as cli_app
from repairbot.ingestion import UploadedLogIngestionService
from repairbot.integrations.execution import FakePatchAgent, FakeTestRunner
from repairbot.integrations.github import FakeGitHubClient
from repairbot.integrations.jira import FakeJiraClient
from repairbot.langstack import (
    AGENT_NAMES,
    LangChainStructuredPlanner,
    LogAnalysisOutput,
    ReviewPrOutput,
    StructuredPlanOutput,
)
from repairbot.persistence import InMemoryRepairPlatformRepository
from repairbot.platform_config import (
    DEFAULT_CONFIG_PATH,
    ConfigSafetyError,
    PromptsConfig,
    load_config_file,
    merge_effective_config,
)
from repairbot.platform_models import UploadedRepairRequest
from repairbot.run_detail import build_run_detail
from repairbot.tracing import LangSmithTracingConfig
from repairbot.workflow import (
    WorkflowDependencies,
    build_repair_graph,
    initial_workflow_state,
)


LOG_TEXT = """2026-06-20 ERROR user-service request failed
java.lang.NullPointerException: profile was missing
    at example.UserService.load(UserService.java:42)
"""


def test_default_prompt_pack_and_old_config_compatibility(tmp_path: Path) -> None:
    default = load_config_file(DEFAULT_CONFIG_PATH)
    assert default.prompts.version == 1
    assert default.prompts.planner_mode == "heuristic"
    assert AGENT_NAMES == (
        "LogAnalysisAgent",
        "RepairPlanningAgent",
        "ReviewPrAgent",
    )

    old_config = tmp_path / "old.yaml"
    old_config.write_text(
        "version: 1\nservice_name: old-service\nrepair:\n  test_command: mvn test\n",
        encoding="utf-8",
    )
    loaded = load_config_file(old_config)
    assert loaded.prompts.planner_mode == "heuristic"
    assert loaded.prompts.log_analysis_agent.system


def test_repo_prompt_override_wins_and_secret_values_fail(tmp_path: Path) -> None:
    repo_config_path = tmp_path / "repairbot.yaml"
    repo_config_path.write_text(
        """version: 1
prompts:
  planner_mode: openai
  log_analysis_agent:
    system: Custom LogAnalysisAgent system prompt.
    human: 'Custom service={service_name} log={log_excerpt}'
""",
        encoding="utf-8",
    )
    effective = merge_effective_config(
        None,
        load_config_file(repo_config_path),
        load_config_file(DEFAULT_CONFIG_PATH),
        None,
    )
    assert effective.prompts.planner_mode == "openai"
    assert effective.prompts.log_analysis_agent.system.startswith("Custom")
    assert effective.prompts.review_pr_agent.system

    unsafe = tmp_path / "unsafe.yaml"
    unsafe.write_text(
        """version: 1
prompts:
  log_analysis_agent:
    system: 'Use """
        + "sk-"
        + "x" * 24
        + """'
    human: '{log_excerpt}'
""",
        encoding="utf-8",
    )
    try:
        load_config_file(unsafe)
    except ConfigSafetyError:
        pass
    else:
        raise AssertionError("secret-looking prompt content must be rejected")


def test_prompt_config_cli_text_json_and_sample(tmp_path: Path) -> None:
    runner = CliRunner()
    text_result = runner.invoke(cli_app, ["prompt-config"])
    assert text_result.exit_code == 0, text_result.output
    assert "planner mode: heuristic" in text_result.output
    assert "LogAnalysisAgent" in text_result.output
    assert "real OpenAI calls require --allow-api: true" in text_result.output

    json_result = runner.invoke(cli_app, ["prompt-config", "--format", "json"])
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["agent_names"] == list(AGENT_NAMES)
    assert payload["api_invoked"] is False
    assert payload["model"].startswith("openai:")

    (tmp_path / "repairbot.yaml").write_text(
        """version: 1
prompts:
  planner_mode: heuristic
  review_pr_agent:
    system: Custom ReviewPrAgent prompt.
    human: 'Review {jira_key}: {patch_summary}'
""",
        encoding="utf-8",
    )
    sample_result = runner.invoke(
        cli_app,
        [
            "prompt-config",
            "--repo-path",
            str(tmp_path),
            "--show-rendered-sample",
        ],
    )
    assert sample_result.exit_code == 0, sample_result.output
    assert "Custom ReviewPrAgent prompt." in sample_result.output
    assert "Review DEMO-1: Add the reviewed null guard." in sample_result.output
    assert "repo:repairbot.yaml > default:repairbot.default.yaml" in sample_result.output


def test_openai_mode_cli_gates_before_api_call(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "error.log"
    log_path.write_text(LOG_TEXT, encoding="utf-8")
    base = [
        "repair-run",
        "--log-file",
        str(log_path),
        "--repo-url",
        "https://github.com/example/user-service.git",
        "--service-name",
        "user-service",
        "--planner-mode",
        "openai",
    ]
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    no_allow = CliRunner().invoke(cli_app, base)
    assert no_allow.exit_code != 0
    assert "requires --allow-api" in no_allow.output

    missing_key = CliRunner().invoke(cli_app, [*base, "--allow-api"])
    assert missing_key.exit_code != 0
    assert "requires OPENAI_API_KEY" in missing_key.output


class _FakeOpenAIModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[object], dict | None]] = []

    def with_structured_output(self, schema):
        parent = self

        class Invoker:
            async def ainvoke(self, messages, config=None):
                parent.calls.append((schema.__name__, messages, config))
                if schema is LogAnalysisOutput:
                    return {
                        "error_name": "NullPointerException",
                        "description": "profile was missing",
                        "service_name": "user-service",
                        "likely_files": ["UserService.java"],
                        "observations": ["The stack frame names UserService.java."],
                        "direct_evidence": ["UserService.load(UserService.java:42)"],
                    }
                if schema is StructuredPlanOutput:
                    return {
                        "observations": ["The profile value is absent."],
                        "assumptions": ["The supplied log matches this revision."],
                        "hypotheses": ["The profile needs the reviewed null guard."],
                        "selected_hypothesis": "The profile needs the reviewed null guard.",
                        "selection_reason": "The stack frame directly supports it.",
                        "planned_edits": ["Apply the reviewed user-service patch."],
                        "planned_tests": ["mvn test"],
                        "validation_plan": ["Run mvn test."],
                        "risk_notes": ["Only the reviewed scenario is supported."],
                        "jira_solution": "Add the reviewed profile null guard and run mvn test.",
                    }
                if schema is ReviewPrOutput:
                    return {
                        "pr_title": "Guard a missing user profile",
                        "pr_summary": "The reviewed guard was applied and mvn test passed.",
                        "review_checklist": ["Inspect the guard.", "Verify test evidence."],
                        "risk_notes": ["Human review remains required."],
                        "jira_comment": "Dry-run review artifacts are ready; no merge is claimed.",
                    }
                raise AssertionError(f"unexpected schema: {schema}")

        return Invoker()


def test_mock_openai_workflow_invokes_exactly_three_agents_and_persists_proof(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("OPENAI_" + "API_KEY", "unit-test-placeholder")
        model = _FakeOpenAIModel()
        prompts = PromptsConfig(
            planner_mode="openai",
            log_analysis_agent={
                "system": "Configured LogAnalysisAgent system.",
                "human": "Configured log for {service_name}: {log_excerpt}",
            },
        )
        planner = LangChainStructuredPlanner(model, prompts)
        assert tuple(planner.agents) == AGENT_NAMES
        assert tuple(type(agent).__name__ for agent in planner.agents.values()) == AGENT_NAMES
        repository = InMemoryRepairPlatformRepository()
        workspace = tmp_path / ".workspace"
        config_path = tmp_path / "openai-default.yaml"
        config_path.write_text(
            """version: 1
prompts:
  planner_mode: openai
  log_analysis_agent:
    system: Configured LogAnalysisAgent system.
    human: 'Configured log for {service_name}: {log_excerpt}'
""",
            encoding="utf-8",
        )
        ingestion = UploadedLogIngestionService(repository, workspace)
        run = await ingestion.create_run(
            request=UploadedRepairRequest(
                repo_url="https://github.com/example/user-service.git",
                service_name="user-service",
                planner_mode="openai",
                allow_api=True,
                require_approval=False,
                dry_run=True,
            ),
            filename="error.log",
            content=LOG_TEXT.encode(),
        )
        deps = WorkflowDependencies(
            repository=repository,
            workspace_root=workspace.resolve(),
            github=FakeGitHubClient(),
            jira=FakeJiraClient(),
            planner=planner,
            patch_agent=FakePatchAgent(),
            test_runner=FakeTestRunner(),
            tracing=LangSmithTracingConfig(False, False, None),
            default_config_path=config_path,
        )
        result = await build_repair_graph(deps).ainvoke(
            await initial_workflow_state(repository, run)
        )
        assert result["status"] == "completed"
        assert [name for name, _, _ in model.calls] == [
            "LogAnalysisOutput",
            "StructuredPlanOutput",
            "ReviewPrOutput",
        ]
        assert len(model.calls) == 3
        assert "Configured log for user-service" in str(model.calls[0][1])

        stored = await repository.get_run(run.run_id)
        assert stored is not None
        assert stored.planner_mode == "openai"
        assert stored.openai_api_invoked is True
        assert stored.langchain_used is True
        assert stored.langgraph_used is True
        assert stored.langsmith_enabled is False
        assert stored.agent_count == 3
        assert stored.agent_names == list(AGENT_NAMES)
        assert set(stored.agent_outputs) == set(AGENT_NAMES)
        assert set(stored.rendered_agent_inputs) == set(AGENT_NAMES)
        assert stored.agent_outputs["RepairPlanningAgent"]["validation_plan"]
        assert stored.agent_outputs["ReviewPrAgent"]["jira_comment"]

        detail = await build_run_detail(repository, stored)
        assert detail.metadata["openai_api_invoked"] is True
        assert detail.metadata["agent_output_count"] == 3
        assert set(detail.agents) == {"log_analysis", "repair_planning", "review_pr"}
        assert detail.trace.langsmith_active is False

    asyncio.run(scenario())


def test_heuristic_mode_never_constructs_openai_and_patch_port_stays_separate(
    tmp_path: Path, monkeypatch
) -> None:
    class FailChatOpenAI:
        def __init__(self, **kwargs):
            raise AssertionError("heuristic mode must not construct an OpenAI client")

    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(ChatOpenAI=FailChatOpenAI),
    )

    async def scenario() -> None:
        repository = InMemoryRepairPlatformRepository()
        workspace = tmp_path / "heuristic-workspace"
        ingestion = UploadedLogIngestionService(repository, workspace)
        run = await ingestion.create_run(
            request=UploadedRepairRequest(
                repo_url="https://github.com/example/user-service.git",
                service_name="user-service",
                planner_mode="heuristic",
                require_approval=False,
            ),
            filename="error.log",
            content=LOG_TEXT.encode(),
        )
        patch_agent = FakePatchAgent()
        deps = WorkflowDependencies(
            repository=repository,
            workspace_root=workspace.resolve(),
            patch_agent=patch_agent,
        )
        await build_repair_graph(deps).ainvoke(
            await initial_workflow_state(repository, run)
        )
        stored = await repository.get_run(run.run_id)
        assert stored is not None
        assert stored.openai_api_invoked is False
        assert stored.langchain_used is False
        assert deps.patch_agent is patch_agent
        assert not hasattr(deps.planner, "apply_patch")

    asyncio.run(scenario())


def test_existing_command_surface_is_preserved() -> None:
    result = CliRunner().invoke(cli_app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in (
        "log-summary",
        "jira-analyze",
        "jira-create",
        "jira-fix",
        "jira-run",
        "uploaded-log-repair",
        "demo-run",
        "repair-run",
        "github-check",
        "ai-planner-check",
        "storage-check",
        "jira-check",
        "ai-smoke-test",
        "init-env",
        "install-safety-hook",
        "safety-check",
        "prompt-config",
    ):
        assert command in result.output
