from pathlib import Path

from repairbot.safety_scan import scan_path


def test_secret_patterns_and_env_files_detected(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("OPENAI_API_KEY='sk-" + "a" * 24 + "'\n", encoding="utf-8")
    (tmp_path / "token.txt").write_text("ghp_" + "b" * 24, encoding="utf-8")
    (tmp_path / "jira.txt").write_text("JIRA_API_TOKEN=" + "c" * 24, encoding="utf-8")
    private_key_header = "-" * 5 + "BEGIN " + "PRIVATE KEY" + "-" * 5
    (tmp_path / "key.pem").write_text(private_key_header + "\nabc\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PASS" + "WORD=supersecret", encoding="utf-8")
    findings = scan_path(tmp_path)
    issue_types = {finding.issue_type for finding in findings}
    assert "openai_api_key" in issue_types
    assert "github_token" in issue_types
    assert "private_key_block" in issue_types
    assert "env_file" in issue_types
    assert "api_token_assignment" in issue_types


def test_logs_are_ignored_by_default_in_gitignore() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "*.log" in gitignore
    assert "logs/" in gitignore
    assert ".env" in gitignore


def test_safety_check_does_not_flag_env_example_placeholders() -> None:
    findings = scan_path(".")
    assert all(finding.path != ".env.example" for finding in findings)


def test_large_log_detection(tmp_path: Path) -> None:
    log_file = tmp_path / "large.log"
    log_file.write_text("x" * 260_000, encoding="utf-8")
    findings = scan_path(tmp_path)
    assert any(finding.issue_type == "large_log" for finding in findings)
