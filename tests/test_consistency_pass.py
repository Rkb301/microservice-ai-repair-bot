from __future__ import annotations

import asyncio
from pathlib import Path
import re
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from repairbot.cli import app as cli_app
from repairbot.ingestion import UploadedLogIngestionService
from repairbot.integrations.execution import (
    FakePatchAgent,
    FakeTestRunner,
    FilePatchAgent,
    PatchApplicationError,
)
from repairbot.integrations.github import FakeGitHubClient
from repairbot.integrations.jira import FakeJiraClient, RealJiraClient
from repairbot.persistence import InMemoryRepairPlatformRepository
from repairbot.platform_models import UploadedRepairRequest
from repairbot.run_detail import build_run_detail
from repairbot.tracing import LangSmithTracingConfig
from repairbot.workflow import (
    WorkflowDependencies,
    build_repair_graph,
    initial_workflow_state,
    route_after_reflection,
    _proposed_solution,
)


LOG_TEXT = """2026-06-20 ERROR order-service request failed
java.lang.NullPointerException: order customer was missing
    at com.example.order.OrderServiceApp.createOrder(OrderServiceApp.java:42)
"""


def test_real_jira_and_fake_github_share_one_run_and_run_id_links(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/rest/api/3/issue":
                return httpx.Response(201, json={"key": "BGSMFWRJ-12"})
            if request.url.path.endswith("/comment"):
                return httpx.Response(201, json={"id": "comment-1"})
            return httpx.Response(404)

        repository = InMemoryRepairPlatformRepository()
        ingestion = UploadedLogIngestionService(repository, tmp_path / ".workspace")
        run = await ingestion.create_run(
            request=UploadedRepairRequest(
                repo_url="https://github.com/Rkb301/order-service.git",
                service_name="order-service",
                jira_project="BGSMFWRJ",
                jira_mode="real",
                github_mode="fake",
                execution_mode="fake",
                require_approval=False,
                allow_network=True,
                dry_run=False,
            ),
            filename="order.log",
            content=LOG_TEXT.encode(),
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://jira.example.com",
        ) as http_client:
            jira = RealJiraClient(
                base_url="https://jira.example.com",
                email="engineer@example.com",
                api_token="unit-test-value",
                http_client=http_client,
            )
            dependencies = WorkflowDependencies(
                repository=repository,
                workspace_root=(tmp_path / ".workspace").resolve(),
                github=FakeGitHubClient(),
                jira=jira,
                patch_agent=FakePatchAgent(),
                test_runner=FakeTestRunner(),
                tracing=LangSmithTracingConfig(False, False, None),
            )
            result = await build_repair_graph(dependencies).ainvoke(
                await initial_workflow_state(repository, run)
            )

        assert result["status"] == "completed"
        assert result["branch_name"] == (
            "bugfix/BGSMFWRJ-12-order-service-null-pointer"
        )
        assert [request.url.path for request in requests] == [
            "/rest/api/3/issue",
            "/rest/api/3/issue/BGSMFWRJ-12/comment",
        ]
        ticket = await repository.get_jira_ticket_for_run(run.run_id)
        pull_request = await repository.get_pull_request_for_run(run.run_id)
        assert ticket is not None and ticket.run_id == run.run_id
        assert ticket.jira_key == "BGSMFWRJ-12"
        assert pull_request is not None and pull_request.run_id == run.run_id
        assert ticket.pull_request_url == pull_request.pull_request_url

        detail = await build_run_detail(
            repository, (await repository.get_run(run.run_id))
        )
        assert detail.jira is not None and detail.jira.run_id == run.run_id
        assert detail.pull_request is not None
        assert detail.pull_request.run_id == run.run_id

        await repository.save_jira_ticket(ticket.model_copy(update={"run_id": None}))
        await repository.save_pull_request(
            pull_request.model_copy(update={"run_id": None})
        )
        legacy_detail = await build_run_detail(
            repository, (await repository.get_run(run.run_id))
        )
        assert legacy_detail.jira is not None
        assert legacy_detail.pull_request is not None

    asyncio.run(scenario())


def test_fake_jira_keys_are_human_readable_and_cross_process_safe() -> None:
    async def create(client: FakeJiraClient, run_id: str, incident_id: str) -> str:
        result = await client.create_or_record_ticket(
            project_key="DEMO",
            title="Demo repair",
            description="Known failure",
            solution="Known fix",
            incident_id=incident_id,
            run_id=run_id,
        )
        return result.jira_key

    first = asyncio.run(create(FakeJiraClient(), "run_alpha", "inc_alpha"))
    second = asyncio.run(create(FakeJiraClient(), "run_beta", "inc_beta"))
    repeated = asyncio.run(create(FakeJiraClient(), "run_alpha", "inc_other"))
    assert re.fullmatch(r"DEMO-[0-9]{10}", first)
    assert first != second
    assert first == repeated


def test_workflow_proposed_solution_is_not_silently_shortened() -> None:
    marker = "CANONICAL-SOLUTION-END"
    plan = SimpleNamespace(
        selected_hypothesis=("Detailed hypothesis section. " * 250) + marker,
        planned_edits=["Apply the complete deterministic change." * 100],
        planned_tests=["Run the complete regression suite." * 100],
    )
    solution = _proposed_solution(plan)
    assert len(solution) > 4000
    assert marker in solution
    assert not solution.endswith("...")


def test_flexible_order_and_payment_patches_and_already_fixed_detection(
    tmp_path: Path,
) -> None:
    order = tmp_path / "order" / "src" / "OrderServiceApp.java"
    order.parent.mkdir(parents=True)
    order.write_text(
        """public class OrderServiceApp {
  record Customer(String id) {}
  static String route() { return createOrder( null ); }
  public static String createOrder ( Customer customer ) {
    String value = customer . id ( );
    return value;
  }
}
""",
        encoding="utf-8",
    )
    payment = tmp_path / "payment" / "src" / "PaymentServiceApp.java"
    payment.parent.mkdir(parents=True)
    payment.write_text(
        """public class PaymentServiceApp {
  static String route() { return charge( -5 ); }
  public static String charge ( int amount ) {
    if ( amount == 0 ) { throw new IllegalArgumentException(); }
    return String.valueOf(amount);
  }
}
""",
        encoding="utf-8",
    )
    agent = FilePatchAgent()
    order_result = asyncio.run(
        agent.apply_patch(
            repo_path=tmp_path / "order",
            module_path=None,
            branch_name="bugfix/DEMO-1-order-service-null-pointer",
            selected_hypothesis="Guard the missing customer",
            service_name="order-service",
        )
    )
    payment_result = asyncio.run(
        agent.apply_patch(
            repo_path=tmp_path / "payment",
            module_path=None,
            branch_name="bugfix/DEMO-2-payment-service-illegal-argument",
            selected_hypothesis="Reject invalid amounts",
            service_name="payment-service",
        )
    )
    assert order_result.changed_files == ["src/OrderServiceApp.java"]
    assert "createOrder(new Customer(\"42\"))" in order.read_text(encoding="utf-8")
    assert "customer == null" in order.read_text(encoding="utf-8")
    assert payment_result.changed_files == ["src/PaymentServiceApp.java"]
    assert "charge(25)" in payment.read_text(encoding="utf-8")
    assert "amount <= 0" in payment.read_text(encoding="utf-8")

    order_fixed = asyncio.run(
        agent.apply_patch(
            repo_path=tmp_path / "order",
            module_path=None,
            branch_name="bugfix/DEMO-3-order-service-null-pointer",
            selected_hypothesis="Guard the missing customer",
            service_name="order-service",
        )
    )
    payment_fixed = asyncio.run(
        agent.apply_patch(
            repo_path=tmp_path / "payment",
            module_path=None,
            branch_name="bugfix/DEMO-4-payment-service-illegal-argument",
            selected_hypothesis="Reject invalid amounts",
            service_name="payment-service",
        )
    )
    assert order_fixed.no_patch_needed is True
    assert payment_fixed.no_patch_needed is True
    assert order_fixed.changed_files == payment_fixed.changed_files == []
    assert order_fixed.summary == "Repository already contains the reviewed fix"
    assert route_after_reflection(
        {"tests_passed": True, "no_patch_needed": True}, 2
    ) == "finalize"


def test_user_service_already_fixed_is_a_clean_no_change(tmp_path: Path) -> None:
    target = tmp_path / "src" / "UserServiceApp.java"
    target.parent.mkdir(parents=True)
    target.write_text(
        """public class UserServiceApp {
  public static String getProfile(String userId) {
    String profile = null;
    if (profile == null) {
      return "{\\"error\\":\\"profile not found\\"}";
    }
    return profile.toUpperCase();
  }
}
""",
        encoding="utf-8",
    )
    result = asyncio.run(
        FilePatchAgent().apply_patch(
            repo_path=tmp_path,
            module_path=None,
            branch_name="bugfix/DEMO-6-user-service-null-pointer",
            selected_hypothesis="Guard the missing profile",
            service_name="user-service",
        )
    )
    assert result.no_patch_needed is True
    assert result.changed_files == []
    assert result.summary == "Repository already contains the reviewed fix"


def test_unfamiliar_service_source_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "src" / "OrderServiceApp.java"
    target.parent.mkdir(parents=True)
    target.write_text("public class OrderServiceApp {}\n", encoding="utf-8")
    with pytest.raises(PatchApplicationError, match="reviewed createOrder shape"):
        asyncio.run(
            FilePatchAgent().apply_patch(
                repo_path=tmp_path,
                module_path=None,
                branch_name="bugfix/DEMO-5-order-service-null-pointer",
                selected_hypothesis="Unknown shape",
                service_name="order-service",
            )
        )


def test_repair_run_real_jira_requires_real_git() -> None:
    result = CliRunner().invoke(
        cli_app,
        [
            "repair-run",
            "--log-file",
            "examples/demo-logs/user-service-missing-profile.log",
            "--repo-url",
            "https://github.com/Rkb301/user-service.git",
            "--service-name",
            "user-service",
            "--real-jira",
            "--allow-network",
        ],
    )
    assert result.exit_code == 2
    assert "--real-jira requires --real-git" in result.output
