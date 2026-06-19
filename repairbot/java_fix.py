"""Jira-ticket Java fix orchestration helpers."""

import json
from pathlib import Path

from . import agents
from .config import RepairBotSettings
from .models import (
    ErrorCluster,
    FixAttempt,
    JiraTicketRecord,
    RepairAttemptArtifact,
    RepairPlan,
    RepairReflection,
    ServiceConfig,
)
from .redaction import redact_sensitive_text
from .service_config import detect_test_command
from .tools import git_ops, shell


def inspect_java_context(
    *,
    repo_path: str | Path,
    cluster: ErrorCluster | None,
    service: ServiceConfig | None,
    settings: RepairBotSettings,
) -> list[str]:
    repo = Path(repo_path)
    source_roots = service.source_roots if service else ["src/main/java", "src/test/java"]
    candidates: list[Path] = []
    if cluster and cluster.file_name:
        for root in source_roots:
            candidates.extend((repo / root).rglob(cluster.file_name))
    if not candidates:
        for root in source_roots:
            root_path = repo / root
            if root_path.exists():
                candidates.extend(root_path.rglob("*.java"))
    return [
        str(path.relative_to(repo).as_posix())
        for path in candidates[: settings.max_files_to_read]
        if path.is_file()
    ]


def run_java_fix_once(
    *,
    ticket: JiraTicketRecord,
    repo_path: str | Path,
    git_repo_path: str | Path | None = None,
    service: ServiceConfig | None,
    cluster: ErrorCluster | None,
    test_cmd: str | None,
    settings: RepairBotSettings,
    attempt_number: int,
    artifacts_dir: str | Path | None = None,
    plan: RepairPlan | None = None,
    hypothesis_id: str | None = None,
    previous_reflection: RepairReflection | None = None,
    allow_sensitive_edits: bool = False,
    allow_deploy_edits: bool = False,
) -> FixAttempt:
    work_path = Path(repo_path).resolve()
    git_repo = Path(git_repo_path or repo_path).resolve()
    files = inspect_java_context(
        repo_path=work_path,
        cluster=cluster,
        service=service,
        settings=settings,
    )
    command = test_cmd or detect_test_command(work_path, service)
    fix_attempt = FixAttempt(
        jira_key=ticket.jira_key,
        attempt=attempt_number,
        plan_id=plan.plan_id if plan else None,
        hypothesis_id=hypothesis_id,
        files_inspected=files,
        dependency_trace=files[: settings.max_dependency_depth],
        test_cmd=redact_sensitive_text(command),
    )
    context = agents.RepoToolContext(
        repo_path=str(work_path),
        allow_sensitive_edits=allow_sensitive_edits,
        allow_deploy_edits=allow_deploy_edits,
    )
    task = _java_fix_prompt(
        ticket,
        cluster,
        files,
        work_path,
        plan=plan,
        hypothesis_id=hypothesis_id,
        previous_reflection=previous_reflection,
    )
    try:
        result = agents.invoke_fix_agent(settings=settings, context=context, task=task)
        fix_attempt.patch_summary = redact_sensitive_text(result)
    except agents.AgentUnavailableError as exc:
        fix_attempt.rejection_reason = str(exc)
        _refresh_attempt_git_context(fix_attempt, git_repo)
        _write_attempt_artifacts(fix_attempt, git_repo, artifacts_dir)
        return fix_attempt

    try:
        result = shell.run_test_command(work_path, command)
        fix_attempt.test_exit_code = result.returncode
        fix_attempt.test_output = redact_sensitive_text(
            result.combined_output or f"Command exited with {result.returncode}"
        )
        fix_attempt.tests_passed = result.returncode == 0
    except Exception as exc:
        fix_attempt.test_exit_code = -1
        fix_attempt.test_output = redact_sensitive_text(str(exc))
        fix_attempt.tests_passed = False
    if not fix_attempt.tests_passed:
        fix_attempt.rejection_reason = "Patch rejected because tests failed."
    _refresh_attempt_git_context(fix_attempt, git_repo)
    _write_attempt_artifacts(fix_attempt, git_repo, artifacts_dir)
    return fix_attempt


def prepare_bugfix_branch(
    *,
    repo_path: str | Path,
    jira_key: str,
    base_branch: str,
    allow_dirty: bool = False,
) -> str:
    repo = git_ops.ensure_git_repo(repo_path)
    git_ops.ensure_clean_worktree(repo, allow_dirty=allow_dirty)
    git_ops.checkout_base_branch(repo, base_branch)
    branch = git_ops.make_bugfix_branch_name(jira_key)
    git_ops.create_branch(repo, branch)
    return branch


def _java_fix_prompt(
    ticket: JiraTicketRecord,
    cluster: ErrorCluster | None,
    files: list[str],
    service_path: Path,
    *,
    plan: RepairPlan | None = None,
    hypothesis_id: str | None = None,
    previous_reflection: RepairReflection | None = None,
) -> str:
    selected = None
    if plan and hypothesis_id:
        selected = next(
            (item for item in plan.hypotheses if item.hypothesis_id == hypothesis_id),
            None,
        )
    hypothesis_block = (
        f"""
Selected structured hypothesis:
- id: {selected.hypothesis_id}
- suspected root cause: {selected.suspected_root_cause}
- evidence: {selected.evidence}
- proposed code change: {selected.proposed_code_change}
- expected test impact: {selected.expected_test_impact}
- risk: {selected.risk_level}
- files likely to change: {', '.join(selected.files_likely_to_change) or 'none'}
"""
        if selected
        else "\nSelected structured hypothesis: none; use the ticket and cluster evidence.\n"
    )
    reflection_block = (
        f"""
Previous structured reflection:
- attempt: {previous_reflection.attempt}
- decision: {previous_reflection.decision}
- next hypothesis: {previous_reflection.next_hypothesis_id or 'none'}
- failure summary: {previous_reflection.failed_test_summary or 'none'}
- post-test reflection: {previous_reflection.post_test_reflection}
"""
        if previous_reflection
        else ""
    )
    return f"""Process Jira ticket {ticket.jira_key} one Java service at a time.
Use the repository tools only. Inspect relevant Java code before editing.
Make the smallest safe fix and avoid deployment/secret/CI files.
Do not weaken tests to make the repair pass.
Focus first on this service source path: {service_path}

Ticket summary: {redact_sensitive_text(ticket.summary)}
Cluster: {redact_sensitive_text(cluster.model_dump_json() if cluster else 'No stored cluster context found.')}
Plan id: {plan.plan_id if plan else 'none'}
{hypothesis_block}
{reflection_block}
Suggested files to inspect:
{chr(10).join('- ' + item for item in files) or '- None found'}

After editing, tests will be run outside the agent and failed patches will be rejected.
"""


def _refresh_attempt_git_context(attempt: FixAttempt, git_repo: Path) -> None:
    attempt.changed_files = git_ops.changed_files(git_repo)
    attempt.diff_stat = git_ops.diff_stat(git_repo)


def _write_attempt_artifacts(
    attempt: FixAttempt,
    git_repo: Path,
    artifacts_dir: str | Path | None,
) -> None:
    if artifacts_dir is None:
        return
    attempt_dir = Path(artifacts_dir) / f"attempt-{attempt.attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    attempt.artifact_path = str(attempt_dir.resolve())
    if attempt.test_output:
        (attempt_dir / "test-output.txt").write_text(attempt.test_output, encoding="utf-8")
    if attempt.diff_stat:
        (attempt_dir / "diff.stat").write_text(attempt.diff_stat, encoding="utf-8")
    diff_text = git_ops.diff_patch(git_repo)
    if diff_text:
        (attempt_dir / "diff.patch").write_text(redact_sensitive_text(diff_text), encoding="utf-8")
    artifact = RepairAttemptArtifact(
        jira_key=attempt.jira_key,
        attempt=attempt.attempt,
        plan_id=attempt.plan_id,
        hypothesis_id=attempt.hypothesis_id,
        artifact_path=str(attempt_dir.resolve()),
        changed_files=attempt.changed_files,
        diff_stat_path=str((attempt_dir / "diff.stat").resolve()) if attempt.diff_stat else None,
        diff_patch_path=str((attempt_dir / "diff.patch").resolve()) if diff_text else None,
        test_output_path=str((attempt_dir / "test-output.txt").resolve()) if attempt.test_output else None,
    )
    (attempt_dir / "attempt-artifact.json").write_text(
        json.dumps(artifact.model_dump(), indent=2),
        encoding="utf-8",
    )
    (attempt_dir / "attempt.json").write_text(
        json.dumps(attempt.model_dump(), indent=2),
        encoding="utf-8",
    )
