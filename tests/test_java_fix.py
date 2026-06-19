from pathlib import Path

from repairbot.service_config import detect_test_command
from repairbot.tools.git_ops import make_bugfix_branch_name


def test_bugfix_branch_name() -> None:
    assert make_bugfix_branch_name("BUG-123") == "bugfix/BUG-123"


def test_maven_gradle_test_command_detection(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<project />", encoding="utf-8")
    assert detect_test_command(tmp_path) == "mvn test"
    (tmp_path / "pom.xml").unlink()
    (tmp_path / "build.gradle").write_text("", encoding="utf-8")
    assert detect_test_command(tmp_path) == "gradle test"
