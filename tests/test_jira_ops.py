from pathlib import Path

from repairbot.jira_ops import build_jira_issue_payload, create_dry_run_ticket
from repairbot.models import ErrorCluster, Hypothesis
from repairbot.storage import RepairBotStorage


def _cluster() -> ErrorCluster:
    return ErrorCluster(
        cluster_id="cluster-001-abcdef1234",
        service_name="order-service",
        application_name="order-api",
        suspected_root_service="order-service",
        symptom_services=["order-service"],
        error_type="NullPointerException",
        class_name="OrderController",
        method_name="createOrder",
        line_number=87,
        endpoint="POST /orders",
        occurrence_count=12,
        severity="HIGH",
        hypotheses=[
            Hypothesis(rank=1, hypothesis="customer is null", evidence="NPE message", confidence=0.82)
        ],
        recommended_solution="Guard missing customer.",
    )


def test_jira_ticket_payload_generation() -> None:
    payload = build_jira_issue_payload(_cluster(), "BUG")
    fields = payload["fields"]
    assert fields["project"]["key"] == "BUG"
    assert fields["issuetype"]["name"] == "Bug"
    assert "[order-service]" in fields["summary"]
    assert fields["description"]["type"] == "doc"


def test_duplicate_jira_ticket_prevention(tmp_path: Path) -> None:
    storage = RepairBotStorage(tmp_path / "repairbot.db")
    first = create_dry_run_ticket(cluster=_cluster(), project_key="BUG", storage=storage)
    second = create_dry_run_ticket(cluster=_cluster(), project_key="BUG", storage=storage)
    assert first.jira_key == second.jira_key
    assert len(storage.list_tickets()) == 1
    storage.close()
