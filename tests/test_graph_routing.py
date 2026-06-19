from repairbot.graph import route_after_tests
from repairbot.state import RepairState


def _state(**overrides):
    base = RepairState(
        repo_path="/tmp/repo",
        service_name="service-a",
        logs_path="/tmp/logs.txt",
    ).as_dict()
    base.update(overrides)
    return base


def test_tests_fail_routes_to_retry() -> None:
    route = route_after_tests(
        _state(status="tests_failed", tests_passed=False, retry_count=0, max_retries=2)
    )
    assert route == "plan_fix"


def test_tests_fail_after_retries_routes_to_final_report() -> None:
    route = route_after_tests(
        _state(status="tests_failed", tests_passed=False, retry_count=2, max_retries=2)
    )
    assert route == "final_report"


def test_tests_pass_dry_run_routes_to_final_report() -> None:
    route = route_after_tests(_state(tests_passed=True, dry_run=True, open_pr=True))
    assert route == "final_report"


def test_tests_pass_open_pr_routes_to_commit() -> None:
    route = route_after_tests(_state(tests_passed=True, dry_run=False, open_pr=True))
    assert route == "create_branch_commit"
