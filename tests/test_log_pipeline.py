from pathlib import Path

from repairbot.config import RepairBotSettings
from repairbot.log_pipeline import (
    build_agent1_payload,
    cluster_errors_deterministically,
    ingest_log_file,
    summarize_entries,
)
from repairbot.models import ServiceConfig


ERROR_LOG = """2026-06-17T12:45:12.123Z ERROR [order-service] [order-api] [traceId=abc123] [spanId=spn1] [requestId=req-001] [POST /orders] [status=500] [durationMs=42] com.example.orders.OrderController.createOrder(OrderController.java:87) - java.lang.NullPointerException: Cannot invoke "Customer.getId()" because "customer" is null
    at com.example.orders.OrderController.createOrder(OrderController.java:87)
    at com.example.orders.OrderService.submit(OrderService.java:54)
"""


def test_fixed_pattern_parser_and_multiline_stack_trace(tmp_path: Path) -> None:
    log_file = tmp_path / "order-service.log"
    log_file.write_text(ERROR_LOG, encoding="utf-8")
    entries = list(ingest_log_file(run_id="run-1", service_name="order-service", path=log_file))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.level == "ERROR"
    assert entry.error_type == "NullPointerException"
    assert entry.class_name == "OrderController"
    assert entry.method_name == "createOrder"
    assert entry.line_number == 87
    assert "OrderService.submit" in (entry.stack_trace or "")


def test_summary_and_deterministic_clustering(tmp_path: Path) -> None:
    log_file = tmp_path / "order-service.log"
    log_file.write_text(
        "2026-06-17T12:45:11.123Z INFO [order-service] [order-api] [traceId=ok1] [requestId=req-ok] [GET /orders/1] [status=200] [durationMs=12] com.example.orders.OrderController.get(OrderController.java:40) - request completed\n"
        + ERROR_LOG,
        encoding="utf-8",
    )
    entries = list(ingest_log_file(run_id="run-1", service_name="order-service", path=log_file))
    summary = summarize_entries("run-1", "order-service", entries)
    clusters = cluster_errors_deterministically(
        run_id="run-1",
        entries_by_service={"order-service": entries},
        summaries={"order-service": summary},
        services={"order-service": ServiceConfig(name="order-service", application_name="order-api")},
    )
    assert summary.info_count == 1
    assert summary.error_count == 1
    assert clusters[0].cluster_id.startswith("cluster-001")
    assert clusters[0].hypotheses


def test_agent1_payload_uses_compact_excerpts_not_full_huge_logs(tmp_path: Path) -> None:
    huge_message = "x" * 100_000
    log_file = tmp_path / "order-service.log"
    log_file.write_text(ERROR_LOG + huge_message, encoding="utf-8")
    entries = list(ingest_log_file(run_id="run-1", service_name="order-service", path=log_file))
    summary = summarize_entries("run-1", "order-service", entries)
    settings = RepairBotSettings(max_log_examples_per_cluster=1)
    clusters = cluster_errors_deterministically(
        run_id="run-1",
        entries_by_service={"order-service": entries},
        summaries={"order-service": summary},
        settings=settings,
    )
    payload = build_agent1_payload(clusters, [summary], settings)
    assert len(str(payload)) < 20_000
    assert huge_message not in str(payload)
