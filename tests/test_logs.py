from repairbot.tools.logs import extract_log_hints


def test_extract_log_hints_keeps_file_and_endpoint_boundaries() -> None:
    hints = extract_log_hints("ValueError in app.py: boom at /api/orders")
    assert hints["errors"] == ["ValueError"]
    assert hints["files"] == ["app.py"]
    assert hints["endpoints"] == ["/api/orders"]
