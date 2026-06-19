"""LangGraph scaffold for the Jira-driven sequential prototype."""

from __future__ import annotations

from typing import Any, Literal


JiraRoute = Literal["create_jira", "fix_tickets", "final_report"]


def route_after_analysis(state: dict[str, Any]) -> JiraRoute:
    mode = state.get("mode", "analyze-only")
    if mode == "analyze-only":
        return "final_report"
    return "create_jira"


def route_after_jira(state: dict[str, Any]) -> JiraRoute:
    mode = state.get("mode", "analyze-only")
    if mode == "analyze-jira":
        return "final_report"
    return "fix_tickets"


def build_jira_graph() -> Any:
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return None

    builder = StateGraph(dict)
    for name in [
        "load_services_config",
        "ingest_logs_streaming",
        "parse_log_files",
        "group_stack_traces",
        "persist_parsed_logs",
        "build_log_summary",
        "cluster_errors_deterministically",
        "agent_1_analyze_clusters",
        "persist_analysis",
        "agent_2_create_jira_tickets",
        "persist_jira",
        "process_tickets_one_by_one",
        "final_report",
    ]:
        builder.add_node(name, _identity)

    builder.add_edge(START, "load_services_config")
    builder.add_edge("load_services_config", "ingest_logs_streaming")
    builder.add_edge("ingest_logs_streaming", "parse_log_files")
    builder.add_edge("parse_log_files", "group_stack_traces")
    builder.add_edge("group_stack_traces", "persist_parsed_logs")
    builder.add_edge("persist_parsed_logs", "build_log_summary")
    builder.add_edge("build_log_summary", "cluster_errors_deterministically")
    builder.add_edge("cluster_errors_deterministically", "agent_1_analyze_clusters")
    builder.add_edge("agent_1_analyze_clusters", "persist_analysis")
    builder.add_conditional_edges(
        "persist_analysis",
        route_after_analysis,
        {"create_jira": "agent_2_create_jira_tickets", "final_report": "final_report"},
    )
    builder.add_edge("agent_2_create_jira_tickets", "persist_jira")
    builder.add_conditional_edges(
        "persist_jira",
        route_after_jira,
        {"fix_tickets": "process_tickets_one_by_one", "final_report": "final_report"},
    )
    builder.add_edge("process_tickets_one_by_one", "final_report")
    builder.add_edge("final_report", END)
    return builder.compile()


def _identity(state: dict[str, Any]) -> dict[str, Any]:
    return state
