import subprocess
import sys
from pathlib import Path


def test_traffic_generator_creates_mixed_logs(tmp_path: Path) -> None:
    out_dir = tmp_path / "logs"
    result = subprocess.run(
        [
            sys.executable,
            "examples/traffic_generator/generate_traffic.py",
            "--services-config",
            "services.yaml.example",
            "--requests",
            "40",
            "--error-rate",
            "0.5",
            "--mode",
            "small",
            "--out-dir",
            str(out_dir),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    text = (out_dir / "order-service.log").read_text(encoding="utf-8")
    assert "INFO" in text
    assert "WARN" in text or "ERROR" in text
    assert "traceId=" in text
