from repairbot.state import RepairState


def test_state_defaults() -> None:
    state = RepairState(
        repo_path="/tmp/repo",
        service_name="service-a",
        logs_path="/tmp/logs.txt",
    )
    assert state.status == "initialized"
    assert state.suspected_files == []
    assert state.changed_files == []
    assert state.tests_passed is False
    assert state.retry_count == 0
    assert state.max_retries == 2
    assert state.open_pr is False
    assert state.diagnose_only is False
