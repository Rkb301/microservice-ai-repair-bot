from repairbot.jira_graph import route_after_analysis, route_after_jira


def test_jira_graph_routing_modes() -> None:
    assert route_after_analysis({"mode": "analyze-only"}) == "final_report"
    assert route_after_analysis({"mode": "analyze-jira"}) == "create_jira"
    assert route_after_jira({"mode": "analyze-jira"}) == "final_report"
    assert route_after_jira({"mode": "analyze-jira-fix-pr"}) == "fix_tickets"
