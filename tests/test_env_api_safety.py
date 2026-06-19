from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repairbot.cli import app
from repairbot.config import RepairBotSettings, load_local_env
from repairbot.models import ErrorCluster
from repairbot.openai_smoke import OpenAISmokeError, run_openai_smoke_test


def test_env_example_has_placeholders_only() -> None:
    text = Path(".env.example").read_text(encoding="utf-8")
    assert "sk-" not in text
    assert "ghp_" not in text
    assert "github_pat_" not in text
    assert "AIza" not in text
    assert "https://your-domain" not in text
    assert "OPENAI_API_KEY=" in text
    assert "JIRA_API_TOKEN=" in text


def test_env_is_ignored() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore
    assert ".env.*" in gitignore
    assert "!.env.example" in gitignore


def test_dotenv_loads_without_overriding_shell_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("REPAIRBOT_MODEL=openai:from-env-file\nREPAIRBOT_MAX_RETRIES=9\n", encoding="utf-8")
    monkeypatch.setenv("REPAIRBOT_MODEL", "openai:from-shell")
    assert load_local_env(env_file) is True
    settings = RepairBotSettings.from_env(dotenv_path=None)
    assert settings.model == "openai:from-shell"
    assert settings.max_retries == 9
    monkeypatch.delenv("REPAIRBOT_MAX_RETRIES", raising=False)


def test_init_env_creates_blank_local_env(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\nGITHUB_TOKEN=\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["init-env", "--path", str(tmp_path)])
    assert result.exit_code == 0
    created = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=" in created
    assert "sk-" not in created
    second = CliRunner().invoke(app, ["init-env", "--path", str(tmp_path)])
    assert second.exit_code == 0
    assert "already exists" in second.stdout


def test_log_summary_does_not_require_openai_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    services = tmp_path / "services.yaml"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    services.write_text(
        "services:\n"
        "  - name: order-service\n"
        "    application_name: order-api\n"
        "    log_file: logs/order-service.log\n",
        encoding="utf-8",
    )
    (logs_dir / "order-service.log").write_text(
        "2026-06-17T12:45:11.123Z INFO [order-service] [order-api] [traceId=t1] [requestId=r1] [GET /orders/1] [status=200] [durationMs=12] com.example.order.OrderController.get(OrderController.java:40) - request completed\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        ["log-summary", "--logs-dir", str(logs_dir), "--services-config", str(services)],
    )
    assert result.exit_code == 0
    assert "order-service" in result.stdout


def test_jira_create_dry_run_does_not_require_openai_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    cluster = ErrorCluster(
        cluster_id="cluster-001-abcdef1234",
        service_name="order-service",
        application_name="order-api",
        suspected_root_service="order-service",
        error_type="NullPointerException",
        class_name="OrderController",
        method_name="createOrder",
        line_number=87,
        occurrence_count=3,
    )
    analysis = tmp_path / "analysis.json"
    out = tmp_path / "tickets.json"
    analysis.write_text(json.dumps({"run_id": "run-1", "clusters": [cluster.model_dump()]}), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["jira-create", "--analysis", str(analysis), "--out", str(out), "--dry-run"],
    )
    assert result.exit_code == 0
    assert out.exists()


def test_ai_smoke_test_requires_allow_api_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["ai-smoke-test"])
    assert result.exit_code == 2

    def fail_post(*args, **kwargs):
        raise AssertionError("requests.post must not run without a key")

    monkeypatch.setattr("requests.post", fail_post)
    with pytest.raises(OpenAISmokeError):
        run_openai_smoke_test()
