"""Typer CLI for the local-first repair bot."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing_extensions import Annotated

import typer

from .analysis import load_analysis, run_log_analysis
from . import agents
from .config import RepairBotSettings
from .graph import build_graph
from .java_fix import prepare_bugfix_branch, run_java_fix_once
from .jira_ops import create_dry_run_ticket, create_jira_issue
from .models import ErrorCluster, FixAttempt, ServiceConfig
from .openai_smoke import OpenAISmokeError, run_openai_smoke_test
from .planning import (
    build_repair_plan,
    build_repair_reflection,
    plan_to_markdown,
    write_reflection_artifacts,
    write_repair_plan_artifacts,
)
from .redaction import redact_sensitive_text
from .safety_scan import install_pre_commit_hook, scan_path
from .service_config import (
    detect_test_command,
    get_service_config,
    load_services_config,
    parse_log_mappings,
    resolve_service_repo_path,
)
from .state import RepairState
from .storage import RepairBotStorage
from .tools import git_ops


app = typer.Typer(help="Local-first AI repair bot for checked-out microservice repos.")


@app.command()
def diagnose(
    repo: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False)],
    service: Annotated[str, typer.Option("--service")],
    logs: Annotated[Path, typer.Option("--logs", exists=True, dir_okay=False)],
    allow_dirty: Annotated[bool, typer.Option("--allow-dirty")] = False,
) -> None:
    """Load local logs, inspect the repo, and print a diagnosis without editing files."""

    repo_root = _validate_repo(repo, allow_dirty=allow_dirty)
    settings = RepairBotSettings.from_env()
    state = RepairState(
        repo_path=str(repo_root),
        service_name=service,
        logs_path=str(logs.resolve()),
        test_cmd="python -c \"print('diagnose only')\"",
        max_retries=0,
        dry_run=True,
        diagnose_only=True,
        allow_dirty=allow_dirty,
    )
    result = build_graph(settings).invoke(state.as_dict())
    _print_report(RepairState.model_validate(result), include_diff=False)


@app.command()
def repair(
    repo: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False)],
    service: Annotated[str, typer.Option("--service")],
    logs: Annotated[Path, typer.Option("--logs", exists=True, dir_okay=False)],
    test_cmd: Annotated[str, typer.Option("--test-cmd")],
    github_repo: Annotated[str | None, typer.Option("--github-repo")] = None,
    base_branch: Annotated[str, typer.Option("--base-branch")] = "main",
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Apply and test locally, but skip branch creation, commit, push, and PR.",
        ),
    ] = False,
    open_pr: Annotated[bool, typer.Option("--open-pr")] = False,
    allow_dirty: Annotated[bool, typer.Option("--allow-dirty")] = False,
    allow_sensitive_edits: Annotated[bool, typer.Option("--allow-sensitive-edits")] = False,
    allow_deploy_edits: Annotated[bool, typer.Option("--allow-deploy-edits")] = False,
    allow_api: Annotated[bool, typer.Option("--allow-api", help="Allow this command to spend provider API credits.")] = False,
) -> None:
    """Apply a small AI-generated local fix, run tests, and optionally open a PR."""

    _require_api_allowed(allow_api, "repair")
    repo_root = _validate_repo(repo, allow_dirty=allow_dirty)
    if open_pr and not github_repo:
        raise typer.BadParameter("--github-repo is required when --open-pr is set")
    settings = RepairBotSettings.from_env()
    state = RepairState(
        repo_path=str(repo_root),
        github_repo=github_repo,
        service_name=service,
        logs_path=str(logs.resolve()),
        test_cmd=test_cmd,
        max_retries=settings.max_retries,
        base_branch=base_branch,
        dry_run=dry_run,
        open_pr=open_pr,
        allow_dirty=allow_dirty,
        allow_sensitive_edits=allow_sensitive_edits,
        allow_deploy_edits=allow_deploy_edits,
    )
    result = build_graph(settings).invoke(state.as_dict())
    _print_report(RepairState.model_validate(result), include_diff=True)


@app.command("log-summary")
def log_summary(
    logs_dir: Annotated[Path, typer.Option("--logs-dir", exists=True, file_okay=False)],
    services_config: Annotated[Path, typer.Option("--services-config", exists=True, dir_okay=False)],
) -> None:
    """Summarize mixed Java service logs without calling an LLM."""

    config = load_services_config(services_config)
    artifact = run_log_analysis(
        services_config=config,
        logs_dir=logs_dir,
        explicit_logs=None,
        out_path=None,
        mode="log-summary",
    )
    for summary in artifact["summaries"]:
        typer.echo(
            f"{summary['service_name']}: lines={summary['lines_scanned']} "
            f"requests={summary['request_count']} INFO={summary['info_count']} "
            f"WARN={summary['warn_count']} ERROR={summary['error_count']}"
        )
        typer.echo(f"  top errors: {summary['top_error_classes']}")
        typer.echo(f"  error endpoints: {summary['top_endpoints_by_error']}")
    typer.echo("candidate root-cause clusters:")
    for cluster in artifact["clusters"]:
        typer.echo(
            f"- {cluster['cluster_id']} {cluster['service_name']} "
            f"{cluster.get('error_type')} root={cluster['suspected_root_service']} "
            f"count={cluster['occurrence_count']} traces={cluster['trace_id_examples']}"
        )


@app.command("jira-analyze")
def jira_analyze(
    services_config: Annotated[Path, typer.Option("--services-config", exists=True, dir_okay=False)],
    out: Annotated[Path, typer.Option("--out")],
    logs_dir: Annotated[Path | None, typer.Option("--logs-dir", file_okay=False)] = None,
    logs: Annotated[list[str] | None, typer.Option("--logs")] = None,
) -> None:
    """Analyze mixed service logs and write compact structured analysis JSON."""

    config = load_services_config(services_config)
    artifact = run_log_analysis(
        services_config=config,
        logs_dir=logs_dir,
        explicit_logs=parse_log_mappings(logs),
        out_path=out,
        mode="analyze-only",
    )
    typer.echo(f"analysis written: {out}")
    typer.echo(f"clusters: {len(artifact['clusters'])}")


@app.command("jira-create")
def jira_create(
    analysis: Annotated[Path, typer.Option("--analysis", exists=True, dir_okay=False)],
    out: Annotated[Path, typer.Option("--out")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Create one Jira Bug per real error cluster, with duplicate prevention."""

    _, clusters = load_analysis(analysis)
    settings = RepairBotSettings.from_env()
    storage = RepairBotStorage()
    tickets = []
    for cluster in clusters:
        record = (
            create_dry_run_ticket(
                cluster=cluster,
                project_key=settings.jira_project_key or "BUG",
                storage=storage,
            )
            if dry_run
            else create_jira_issue(cluster=cluster, settings=settings, storage=storage)
        )
        tickets.append(record.model_dump())
        typer.echo(f"{record.jira_key}: {record.summary}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tickets": tickets}, indent=2), encoding="utf-8")
    storage.close()
    typer.echo(f"tickets written: {out}")


@app.command("jira-fix")
def jira_fix(
    ticket: Annotated[str, typer.Option("--ticket")],
    repo: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False)],
    service: Annotated[str, typer.Option("--service")],
    services_config: Annotated[Path | None, typer.Option("--services-config", exists=True, dir_okay=False)] = None,
    logs_dir: Annotated[Path | None, typer.Option("--logs-dir", file_okay=False)] = None,
    base_branch: Annotated[str, typer.Option("--base-branch")] = "main",
    test_cmd: Annotated[str | None, typer.Option("--test-cmd")] = None,
    open_pr: Annotated[bool, typer.Option("--open-pr")] = False,
    github_repo: Annotated[str | None, typer.Option("--github-repo")] = None,
    plan_only: Annotated[
        bool,
        typer.Option("--plan-only", help="Generate and save the structured repair plan, then stop before edits/tests/commit/PR."),
    ] = False,
    plan_first: Annotated[
        bool,
        typer.Option("--plan-first", help="Print and save the structured repair plan before continuing."),
    ] = False,
    require_plan_approval: Annotated[
        bool,
        typer.Option("--require-plan-approval", help="Stop after planning unless --approve-plan matches the generated plan id."),
    ] = False,
    approve_plan: Annotated[str | None, typer.Option("--approve-plan", help="Plan id that approves a --require-plan-approval run.")] = None,
    max_hypotheses: Annotated[int, typer.Option("--max-hypotheses", min=1)] = 3,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Apply and test locally without commit, push, or PR. With --allow-api "
                "this can still spend API credits; changes are restored unless "
                "--keep-dry-run-changes is set."
            ),
        ),
    ] = False,
    keep_dry_run_changes: Annotated[
        bool,
        typer.Option(
            "--keep-dry-run-changes",
            help="Keep dry-run edits in the worktree instead of restoring them.",
        ),
    ] = False,
    allow_dirty: Annotated[bool, typer.Option("--allow-dirty")] = False,
    allow_sensitive_edits: Annotated[bool, typer.Option("--allow-sensitive-edits")] = False,
    allow_deploy_edits: Annotated[bool, typer.Option("--allow-deploy-edits")] = False,
    allow_api: Annotated[bool, typer.Option("--allow-api", help="Allow this command to spend provider API credits.")] = False,
) -> None:
    """Fix one Jira ticket on a bugfix/{JIRA_KEY} branch."""

    del logs_dir
    if keep_dry_run_changes and not dry_run:
        raise typer.BadParameter("--keep-dry-run-changes only applies with --dry-run")
    if plan_only and keep_dry_run_changes:
        raise typer.BadParameter("--keep-dry-run-changes does not apply with --plan-only")
    if dry_run and allow_dirty and not keep_dry_run_changes:
        raise typer.BadParameter(
            "--dry-run with --allow-dirty requires --keep-dry-run-changes so pre-existing edits are not restored or removed"
        )
    _require_api_allowed(allow_api, "jira-fix")
    settings = RepairBotSettings.from_env()
    repo_root = git_ops.ensure_git_repo(repo)
    storage = RepairBotStorage(repo_root / ".repairbot" / "repairbot.db")
    record = storage.get_ticket(ticket)
    if not record:
        raise typer.BadParameter(f"No stored Jira ticket found for {ticket}; run jira-create first")
    cluster = storage.get_cluster(record.cluster_id)
    service_config = _resolve_jira_fix_service_config(
        service_name=service,
        repo_root=repo_root,
        services_config_path=services_config,
    )
    repair_path = (
        resolve_service_repo_path(
            repo_root=repo_root,
            service=service_config,
            services_config_path=services_config,
        )
        if service_config
        else repo_root
    )
    typer.echo(f"repair scope: {repair_path}")
    artifact_dir = _create_jira_fix_artifact_dir(repo_root, ticket)
    typer.echo(f"run artifacts: {artifact_dir}")
    effective_test_cmd = test_cmd or detect_test_command(repair_path, service_config)
    plan = build_repair_plan(
        ticket=record,
        cluster=cluster,
        selected_service=service,
        service_root=repair_path,
        service=service_config,
        test_cmd=effective_test_cmd,
        settings=settings,
        max_hypotheses=max_hypotheses,
    )
    plan_json, plan_md = write_repair_plan_artifacts(plan, artifact_dir)
    typer.echo(f"plan id: {plan.plan_id}")
    typer.echo(f"plan artifacts: {plan_json}, {plan_md}")
    if plan_first or plan_only or require_plan_approval:
        _print_plan(plan)
    if approve_plan and approve_plan != plan.plan_id:
        storage.close()
        raise typer.BadParameter(f"--approve-plan must match generated plan id {plan.plan_id}")
    if require_plan_approval and approve_plan != plan.plan_id:
        storage.close()
        typer.echo(f"Plan approval required. Re-run with --approve-plan {plan.plan_id} to continue.")
        raise typer.Exit(code=2)
    if plan_only:
        storage.close()
        typer.echo("plan-only complete; no files were edited, tests were not run for repair, and no commit/push/PR was created")
        return

    if dry_run:
        git_ops.ensure_clean_worktree(repo_root, allow_dirty=allow_dirty)
        branch = git_ops.current_branch(repo_root)
        if open_pr:
            typer.echo("dry-run mode: skipping commit, push, and PR creation even though --open-pr was passed")
    else:
        branch = prepare_bugfix_branch(
            repo_path=repo_root,
            jira_key=ticket,
            base_branch=base_branch,
            allow_dirty=allow_dirty,
        )
    attempts = []
    active_hypothesis_id = plan.selected_hypothesis.hypothesis_id if plan.selected_hypothesis else None
    previous_reflection = None
    for attempt_number in range(1, settings.max_fix_attempts + 1):
        attempt = run_java_fix_once(
            ticket=record,
            repo_path=repair_path,
            git_repo_path=repo_root,
            service=service_config,
            cluster=cluster,
            test_cmd=test_cmd,
            settings=settings,
            attempt_number=attempt_number,
            artifacts_dir=artifact_dir,
            plan=plan,
            hypothesis_id=active_hypothesis_id,
            previous_reflection=previous_reflection,
            allow_sensitive_edits=allow_sensitive_edits,
            allow_deploy_edits=allow_deploy_edits,
        )
        storage.store_fix_attempt(attempt)
        attempts.append(attempt)
        typer.echo(f"attempt {attempt_number}: tests_passed={attempt.tests_passed}")
        reflection = build_repair_reflection(
            plan=plan,
            attempt_number=attempt_number,
            hypothesis_id=active_hypothesis_id,
            tests_passed=attempt.tests_passed,
            test_exit_code=attempt.test_exit_code,
            test_output=attempt.test_output,
        )
        attempt_dir = Path(attempt.artifact_path or artifact_dir / f"attempt-{attempt_number}")
        write_reflection_artifacts(reflection, attempt_dir)
        if not attempt.tests_passed:
            _print_failed_attempt(attempt, reflection_path=attempt_dir / "reflection.json")
            previous_reflection = reflection
            active_hypothesis_id = reflection.next_hypothesis_id
        if attempt.tests_passed:
            break
    passed = any(attempt.tests_passed for attempt in attempts)
    if not passed:
        if dry_run and not keep_dry_run_changes:
            restored = _restore_dry_run_changes(repo_root, artifact_dir)
            _print_dry_run_restore(restored, artifact_dir)
        elif dry_run:
            typer.echo("dry-run changes kept because --keep-dry-run-changes was set")
        storage.close()
        raise typer.Exit(code=1)
    diff = git_ops.diff(repo_root)
    typer.echo("\n--- repairbot diff before commit ---\n")
    typer.echo(diff or "(empty)")
    if dry_run:
        if keep_dry_run_changes:
            typer.echo("dry-run changes kept because --keep-dry-run-changes was set")
        else:
            restored = _restore_dry_run_changes(repo_root, artifact_dir)
            _print_dry_run_restore(restored, artifact_dir)
        storage.close()
        typer.echo(f"dry-run complete on branch {branch}; no commit, push, or PR was created")
        return
    changed = git_ops.changed_files(repo_root)
    commit_sha = git_ops.commit_changes(repo_root, changed, f"{ticket}: Fix {cluster.error_type if cluster else service} in {service}")
    pr_url = None
    if open_pr:
        if not github_repo:
            raise typer.BadParameter("--github-repo is required with --open-pr")
        git_ops.push_branch(repo_root, branch)
        pr_url = agents.open_pull_request_from_state(
            github_repo=github_repo,
            branch_name=branch,
            base_branch=base_branch,
            service_name=service,
            diagnosis=record.summary,
            fix_plan=attempts[-1].patch_summary,
            changed_files=changed,
            test_cmd=attempts[-1].test_cmd,
            test_output=attempts[-1].test_output,
            token=settings.github_token,
        )
    typer.echo(f"branch={branch} commit={commit_sha} pr={pr_url or '(none)'}")
    storage.close()


@app.command("jira-run")
def jira_run(
    logs_dir: Annotated[Path, typer.Option("--logs-dir", exists=True, file_okay=False)],
    services_config: Annotated[Path, typer.Option("--services-config", exists=True, dir_okay=False)],
    base_branch: Annotated[str, typer.Option("--base-branch")] = "main",
    mode: Annotated[
        str,
        typer.Option("--mode"),
    ] = "analyze-only",
    dry_run_jira: Annotated[bool, typer.Option("--dry-run-jira")] = False,
) -> None:
    """Run the sequential Jira prototype pipeline."""

    if mode not in {"analyze-only", "analyze-jira", "analyze-jira-fix-local", "analyze-jira-fix-pr"}:
        raise typer.BadParameter("Unsupported mode")
    config = load_services_config(services_config)
    out = Path("runs") / "latest" / "analysis.json"
    artifact = run_log_analysis(
        services_config=config,
        logs_dir=logs_dir,
        explicit_logs=None,
        out_path=out,
        mode=mode,
    )
    typer.echo(f"analysis written: {out}")
    if mode == "analyze-only":
        return
    settings = RepairBotSettings.from_env()
    storage = RepairBotStorage()
    tickets = []
    for item in artifact["clusters"]:
        cluster = ErrorCluster.model_validate(item)
        ticket = (
            create_dry_run_ticket(cluster=cluster, project_key=settings.jira_project_key or "BUG", storage=storage)
            if dry_run_jira
            else create_jira_issue(cluster=cluster, settings=settings, storage=storage)
        )
        tickets.append(ticket)
    typer.echo(f"tickets: {len(tickets)}")
    if mode == "analyze-jira":
        storage.close()
        return
    typer.echo("fix stage is sequential; run jira-fix per ticket for this local prototype:")
    for ticket in tickets:
        typer.echo(
            f"python -m repairbot.cli jira-fix --ticket {ticket.jira_key} "
            f"--repo . --service {ticket.service_name} "
            f"--services-config {services_config} "
            f"--base-branch {base_branch}"
        )
    storage.close()


@app.command("uploaded-log-repair")
def uploaded_log_repair(
    log_file: Annotated[Path, typer.Option("--log-file", exists=True, dir_okay=False)],
    repo_url: Annotated[str, typer.Option("--repo-url")],
    branch: Annotated[str | None, typer.Option("--branch")] = None,
    service_name: Annotated[str | None, typer.Option("--service-name")] = None,
    module_path: Annotated[str | None, typer.Option("--module-path")] = None,
    test_cmd: Annotated[str | None, typer.Option("--test-cmd")] = None,
    jira_project: Annotated[str | None, typer.Option("--jira-project")] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", exists=True, dir_okay=False, help="Safe YAML defaults file."),
    ] = None,
    require_approval: Annotated[
        bool | None,
        typer.Option("--require-approval/--no-require-approval"),
    ] = None,
) -> None:
    """Run the network-free uploaded-log + external-repository scaffold."""

    import asyncio

    from .ingestion import UploadedLogIngestionService
    from .persistence import InMemoryRepairPlatformRepository
    from .platform_config import load_config_file
    from .platform_models import UploadedRepairRequest
    from .workflow import (
        build_repair_graph,
        default_workflow_dependencies,
        initial_workflow_state,
    )

    async def execute() -> dict:
        repository = InMemoryRepairPlatformRepository()
        workspace = Path(".workspace").resolve()
        ingestion = UploadedLogIngestionService(repository, workspace)
        run = await ingestion.create_run(
            request=UploadedRepairRequest(
                repo_url=repo_url,
                repo_branch=branch,
                service_name=service_name,
                module_path=module_path,
                test_cmd=test_cmd,
                jira_project=jira_project,
                require_approval=require_approval,
            ),
            filename=log_file.name,
            content=log_file.read_bytes(),
        )
        dependencies = default_workflow_dependencies(
            repository=repository, workspace_root=workspace
        )
        if config is not None:
            load_config_file(config)
            dependencies.default_config_path = config.resolve()
        graph = build_repair_graph(dependencies)
        await graph.ainvoke(await initial_workflow_state(repository, run))
        stored = await repository.get_run(run.run_id)
        if stored is None:
            raise RuntimeError("Repair run was not stored")
        return {
            "run_id": stored.run_id,
            "status": stored.status,
            "current_step": stored.current_step,
            "jira_ticket_url": stored.jira_ticket_url,
            "pull_request_url": stored.pull_request_url,
            "langsmith_trace_url": stored.langsmith_trace_url,
        }

    try:
        result = asyncio.run(execute())
    except ValueError as exc:
        raise typer.BadParameter(redact_sensitive_text(str(exc))) from exc
    typer.echo(json.dumps(result, indent=2))


@app.command("demo-run")
def demo_run(
    sample: Annotated[
        str,
        typer.Option(
            "--sample",
            help="Built-in service sample: user-service, order-service, or payment-service.",
        ),
    ] = "user-service",
    repo_url: Annotated[
        str | None,
        typer.Option("--repo-url", help="GitHub repository URL shown in demo metadata."),
    ] = None,
    target_branch: Annotated[str, typer.Option("--target-branch")] = "main",
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Ignored local directory for deterministic run artifacts."),
    ] = Path(".workspace"),
    config: Annotated[
        Path | None,
        typer.Option("--config", exists=True, dir_okay=False, help="Optional safe YAML defaults file."),
    ] = None,
) -> None:
    """Run a complete backend-only sample using local deterministic adapters."""

    import asyncio

    from .demo import execute_demo_sample

    try:
        detail = asyncio.run(
            execute_demo_sample(
                sample=sample,
                repo_url=repo_url,
                target_branch=target_branch,
                workspace_root=workspace,
                config_path=config,
            )
        )
    except ValueError as exc:
        raise typer.BadParameter(redact_sensitive_text(str(exc))) from exc

    model = detail.effective_config.get("model", {})
    jira = detail.jira
    pull_request = detail.pull_request
    typer.echo(f"run ID: {detail.run.run_id}")
    typer.echo(f"status: {detail.run.status}")
    typer.echo(f"error name: {detail.incident.error_name}")
    typer.echo(f"proposed solution: {detail.incident.proposed_solution}")
    typer.echo(f"Jira-style URL: {jira.jira_ticket_url if jira else '(not recorded)'}")
    typer.echo(
        f"PR-style URL: {pull_request.pull_request_url if pull_request else '(not recorded)'}"
    )
    typer.echo(
        "effective config: "
        f"provider={model.get('provider')} model={model.get('model')} "
        f"jira={detail.run.jira_mode} github={detail.run.github_mode}"
    )
    typer.echo(f"trace placeholder: {detail.trace.local_trace_url}")


@app.command("repair-run")
def repair_run(
    log_file: Annotated[Path, typer.Option("--log-file", exists=True, dir_okay=False)],
    repo_url: Annotated[str, typer.Option("--repo-url")],
    service_name: Annotated[str, typer.Option("--service-name")],
    target_branch: Annotated[str, typer.Option("--target-branch")] = "main",
    real_git: Annotated[
        bool,
        typer.Option("--real-git", help="Select real clone, patch/test, push, and PR adapters."),
    ] = False,
    real_jira: Annotated[
        bool,
        typer.Option(
            "--real-jira",
            help="Create a real Jira issue before branch naming and update it after PR creation.",
        ),
    ] = False,
    jira_project: Annotated[
        str | None,
        typer.Option(
            "--jira-project",
            help="Jira project key for this run; required for real Jira unless supplied by env or explicit config.",
        ),
    ] = None,
    allow_network: Annotated[
        bool,
        typer.Option("--allow-network", help="Required for real GitHub and other configured network adapters."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Clone, branch, patch, and test, but do not commit, push, or open a PR."),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option("--config", exists=True, dir_okay=False, help="Optional safe YAML defaults file."),
    ] = None,
    planner_mode: Annotated[
        str | None,
        typer.Option(
            "--planner-mode",
            help="Planner mode: heuristic (network-free) or openai (exactly three model calls).",
        ),
    ] = None,
    allow_api: Annotated[
        bool,
        typer.Option("--allow-api", help="Required when the effective planner mode is openai."),
    ] = False,
) -> None:
    """Run the credential-gated backend repair workflow."""

    import asyncio

    from .ingestion import UploadedLogIngestionService
    from .persistence import InMemoryRepairPlatformRepository, MongoRepairPlatformRepository
    from .platform_config import load_config_file
    from .platform_models import UploadedRepairRequest
    from .run_detail import build_run_detail
    from .workflow import (
        build_repair_graph,
        default_workflow_dependencies,
        initial_workflow_state,
    )

    settings = RepairBotSettings.from_env()
    if planner_mode not in {None, "heuristic", "openai"}:
        raise typer.BadParameter("--planner-mode must be heuristic or openai")
    if real_git and not allow_network:
        raise typer.BadParameter("--real-git requires --allow-network")
    if real_git and not os.getenv("GITHUB_TOKEN"):
        raise typer.BadParameter("--real-git requires GITHUB_TOKEN in the environment")
    if real_jira and not real_git:
        raise typer.BadParameter("--real-jira requires --real-git for a real repair workflow")
    if real_jira and not allow_network:
        raise typer.BadParameter("--real-jira requires --allow-network")
    jira_credentials = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
    missing_jira = [name for name in jira_credentials if not os.getenv(name)]
    if real_jira and missing_jira:
        raise typer.BadParameter(
            "--real-jira requires environment credentials: " + ", ".join(missing_jira)
        )
    explicit_config = load_config_file(config) if config is not None else None
    configured_jira_project = explicit_config.jira.project_key if explicit_config else None
    selected_jira_project = (
        jira_project or os.getenv("JIRA_PROJECT_KEY") or configured_jira_project
    )
    if real_jira and not selected_jira_project:
        raise typer.BadParameter(
            "--real-jira requires --jira-project, JIRA_PROJECT_KEY, or an explicit config project"
        )
    selected_planner_mode = (
        planner_mode
        or (explicit_config.prompts.planner_mode if explicit_config else "heuristic")
    )
    if selected_planner_mode == "openai" and not allow_api:
        raise typer.BadParameter("--planner-mode openai requires --allow-api")
    if selected_planner_mode == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise typer.BadParameter("--planner-mode openai requires OPENAI_API_KEY")
    configured_model = explicit_config.model.model if explicit_config else settings.model
    if selected_planner_mode == "openai" and not configured_model.startswith("openai:"):
        raise typer.BadParameter("--planner-mode openai requires an openai-prefixed model")

    async def execute():
        mongodb_configured = bool(os.getenv("MONGODB_URI") or os.getenv("MONGODB_DB"))
        if allow_network and mongodb_configured:
            repository = MongoRepairPlatformRepository.from_env()
            if not await repository.ping():
                raise RuntimeError("MongoDB ping returned an unhealthy response")
            await repository.ensure_indexes()
            storage_mode = "mongodb"
        else:
            repository = InMemoryRepairPlatformRepository()
            storage_mode = "memory"
        dependencies = default_workflow_dependencies(
            repository=repository,
            workspace_root=Path(".workspace").resolve(),
        )
        if config is not None:
            load_config_file(config)
            dependencies.default_config_path = config.resolve()
        try:
            ingestion = UploadedLogIngestionService(
                repository,
                dependencies.workspace_root,
            )
            run = await ingestion.create_run(
                request=UploadedRepairRequest(
                    repo_url=repo_url,
                    repo_branch=target_branch,
                    service_name=service_name,
                    jira_project=selected_jira_project,
                    jira_mode="real" if real_jira else None,
                    require_approval=False,
                    github_mode="real" if real_git else "fake",
                    execution_mode="real" if real_git else "fake",
                    allow_network=allow_network,
                    allow_api=allow_api,
                    planner_mode=planner_mode,
                    dry_run=dry_run,
                ),
                filename=log_file.name,
                content=log_file.read_bytes(),
            )
            graph = build_repair_graph(dependencies)
            await graph.ainvoke(await initial_workflow_state(repository, run))
            stored = await repository.get_run(run.run_id)
            if stored is None:
                raise RuntimeError("Repair run was not persisted")
            return await build_run_detail(repository, stored), storage_mode
        finally:
            close_github = getattr(dependencies.github, "aclose", None)
            if callable(close_github):
                await close_github()
            close_jira = getattr(dependencies.jira, "aclose", None)
            if callable(close_jira):
                await close_jira()
            close_repository = getattr(repository, "close", None)
            if callable(close_repository):
                close_repository()

    try:
        detail, storage_mode = asyncio.run(execute())
    except Exception as exc:
        typer.echo(f"repair-run failed: {redact_sensitive_text(str(exc))}")
        raise typer.Exit(code=1)
    latest_attempt = detail.attempts[-1] if detail.attempts else None
    changed_files = latest_attempt.changed_files if latest_attempt else []
    typer.echo(f"run ID: {detail.run.run_id}")
    typer.echo(f"status: {detail.run.status}")
    typer.echo(f"branch: {detail.run.branch_name or '(none)'}")
    typer.echo(
        f"tests passed: {latest_attempt.tests_passed if latest_attempt else False}"
    )
    typer.echo(
        "patch summary: "
        f"{redact_sensitive_text(latest_attempt.patch_summary) if latest_attempt else '(not run)'}"
    )
    typer.echo(
        "test command: "
        f"{redact_sensitive_text(latest_attempt.test_command) if latest_attempt else '(not run)'}"
    )
    typer.echo(
        "resolved executable: "
        f"{redact_sensitive_text(latest_attempt.resolved_test_executable) if latest_attempt and latest_attempt.resolved_test_executable else '(not available)'}"
    )
    typer.echo(
        "exit code: "
        f"{latest_attempt.test_exit_code if latest_attempt else '(not run)'}"
    )
    typer.echo("changed files: " + (", ".join(changed_files) or "(none)"))
    typer.echo("test output tail:")
    typer.echo(
        _last_lines(
            redact_sensitive_text(latest_attempt.test_output)
            if latest_attempt
            else "(no test output)",
            40,
        )
    )
    typer.echo(
        f"Jira URL: {detail.jira.jira_ticket_url if detail.jira else '(none)'}"
    )
    typer.echo(
        "PR URL: "
        f"{detail.pull_request.pull_request_url if detail.pull_request else '(none)'}"
    )
    typer.echo(f"storage mode: {storage_mode}")
    typer.echo(f"Jira mode: {detail.run.jira_mode}")
    typer.echo(f"model: {detail.run.model_name}")
    typer.echo(f"planner mode: {detail.run.planner_mode}")
    typer.echo("agents: " + ", ".join(detail.run.agent_names))
    typer.echo(f"OpenAI API invoked: {detail.run.openai_api_invoked}")
    typer.echo(f"LangChain used: {detail.run.langchain_used}")
    typer.echo(f"LangGraph used: {detail.run.langgraph_used}")
    typer.echo(f"LangSmith active: {detail.run.langsmith_enabled}")
    typer.echo(f"dry run: {detail.run.dry_run}")


@app.command("prompt-config")
def prompt_config(
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo-path",
            "--repo",
            exists=True,
            file_okay=False,
            help="Optional repository with a prompt override YAML.",
        ),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json."),
    ] = "text",
    sample: Annotated[
        bool,
        typer.Option(
            "--show-rendered-sample",
            "--sample",
            help="Render safe synthetic inputs through all three templates.",
        ),
    ] = False,
) -> None:
    """Inspect the effective three-agent prompt pack without making API calls."""

    from .platform_config import (
        DEFAULT_CONFIG_PATH,
        PROMPT_TEMPLATE_NAMES,
        find_project_config_path,
        load_config_file,
        merge_effective_config,
        render_agent_prompt,
    )

    if output_format not in {"text", "json"}:
        raise typer.BadParameter("--format must be text or json")
    default_config = load_config_file(DEFAULT_CONFIG_PATH)
    repo_path = find_project_config_path(repo) if repo is not None else None
    repo_config = load_config_file(repo_path) if repo_path is not None else None
    effective = merge_effective_config(
        None,
        repo_config,
        default_config,
        RepairBotSettings.from_env(),
    )
    prompt_data = effective.prompts.model_dump(mode="json")
    rendered: dict[str, dict[str, str]] = {}
    if sample:
        values = {
            "service_name": "user-service",
            "log_excerpt": "NullPointerException at UserService.java:42",
            "error_name": "NullPointerException",
            "analysis": '{"observation":"missing profile null guard"}',
            "repo_url": "https://github.com/example/user-service.git",
            "target_branch": "main",
            "build_system": "maven",
            "test_command": "mvn test",
            "supported_scenarios": "user-service missing profile null guard",
            "jira_key": "DEMO-1",
            "branch_name": "bugfix/DEMO-1-user-service-null-pointer",
            "patch_summary": "Add the reviewed null guard.",
            "changed_files": "src/main/java/example/UserService.java",
            "test_result": "exit_code=0; passed=true",
            "risk_notes": "Human review remains required.",
            "selected_hypothesis": "A missing profile crosses the service boundary.",
            "planning_output": '{"planned_edits":["add reviewed null guard"]}',
        }
        for name in PROMPT_TEMPLATE_NAMES:
            item = getattr(effective.prompts, name)
            rendered[name] = {
                "system": render_agent_prompt(item.system, values),
                "human": render_agent_prompt(item.human, values),
            }
    payload = {
        "planner_mode": effective.prompts.planner_mode,
        "prompt_version": effective.prompts.version,
        "model": effective.model.model,
        "agent_names": [
            "LogAnalysisAgent",
            "RepairPlanningAgent",
            "ReviewPrAgent",
        ],
        "prompt_names": list(PROMPT_TEMPLATE_NAMES),
        "config_precedence": (
            f"repo:{repo_path.name} > default:{DEFAULT_CONFIG_PATH.name} > environment/fallback"
            if repo_path
            else f"default:{DEFAULT_CONFIG_PATH.name} > environment/fallback"
        ),
        "prompts": prompt_data,
        "rendered_samples": rendered,
        "api_invoked": False,
        "openai_requires_allow_api": True,
    }
    if output_format == "json":
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"planner mode: {payload['planner_mode']}")
    typer.echo(f"model: {payload['model']}")
    typer.echo(f"prompt version: {payload['prompt_version']}")
    typer.echo("agent names: " + ", ".join(payload["agent_names"]))
    typer.echo(f"config precedence: {payload['config_precedence']}")
    for name in PROMPT_TEMPLATE_NAMES:
        item = getattr(effective.prompts, name)
        typer.echo(f"{name}.system: {item.system}")
        typer.echo(f"{name}.human: {item.human}")
        if sample:
            typer.echo(f"{name}.sample.system: {rendered[name]['system']}")
            typer.echo(f"{name}.sample.human: {rendered[name]['human']}")
    typer.echo("API invoked: false")
    typer.echo("real OpenAI calls require --allow-api: true")


@app.command("github-check")
def github_check(
    allow_network: Annotated[
        bool,
        typer.Option(
            "--allow-network",
            help="Explicitly call GitHub REST and run a read-only git ls-remote.",
        ),
    ] = False,
    repo_url: Annotated[
        str | None,
        typer.Option(
            "--repo-url",
            help="Credential-free GitHub HTTPS repository URL to verify.",
        ),
    ] = None,
) -> None:
    """Validate GitHub auth configuration and optionally verify live access."""

    import asyncio

    from .integrations.github import (
        GitHubIntegrationError,
        RealGitHubClient,
        build_github_client,
        parse_github_repo_url,
    )

    RepairBotSettings.from_env()
    if not os.getenv("GITHUB_TOKEN"):
        typer.echo("github mode: unavailable")
        typer.echo("missing settings: GITHUB_TOKEN")
        raise typer.Exit(code=1)
    if repo_url is not None:
        try:
            parse_github_repo_url(repo_url)
        except ValueError as exc:
            raise typer.BadParameter(redact_sensitive_text(str(exc))) from exc
    if allow_network and repo_url is None:
        raise typer.BadParameter("--allow-network requires --repo-url")
    try:
        client = build_github_client("real")
    except (RuntimeError, ValueError):
        typer.echo("github mode: invalid real configuration")
        raise typer.Exit(code=1)
    if not isinstance(client, RealGitHubClient):
        raise typer.Exit(code=1)

    typer.echo("github mode: real")
    typer.echo("REST authentication: Bearer")
    typer.echo("git authentication: HTTP Basic")
    if not allow_network:
        asyncio.run(client.aclose())
        typer.echo("network check: skipped; pass --allow-network with --repo-url")
        return

    async def execute():
        try:
            status = await client.check_repository_access(repo_url or "")
            git_ok = await client.check_git_remote(repo_url or "")
            return status, git_ok
        finally:
            await client.aclose()

    try:
        status, git_ok = asyncio.run(execute())
    except (GitHubIntegrationError, ValueError) as exc:
        typer.echo(f"GitHub check: failed; {redact_sensitive_text(str(exc))}")
        raise typer.Exit(code=1)
    permissions = ", ".join(
        f"{name}={'true' if allowed else 'false'}"
        for name, allowed in sorted(status.permissions.items())
    ) or "not reported"
    typer.echo(f"repo full name: {redact_sensitive_text(status.full_name)}")
    typer.echo(f"visibility: {'private' if status.private else 'public'}")
    typer.echo(f"permissions: {permissions}")
    typer.echo(f"git ls-remote: {'passed' if git_ok else 'failed'}")
    if not git_ok:
        raise typer.Exit(code=1)


@app.command("ai-planner-check")
def ai_planner_check(
    allow_api: Annotated[
        bool,
        typer.Option("--allow-api", help="Required to make one small OpenAI structured-output request."),
    ] = False,
) -> None:
    """Verify the configured real OpenAI structured planner path."""

    if not allow_api:
        typer.echo(
            "planner check: skipped; pass --allow-api to verify one small structured OpenAI request"
        )
        return
    if not os.getenv("OPENAI_API_KEY"):
        typer.echo("planner check: unavailable; OPENAI_API_KEY is not set")
        raise typer.Exit(code=1)
    import asyncio

    from .openai_planner_check import OpenAIPlannerCheckError, run_planner_check

    settings = RepairBotSettings.from_env()
    try:
        result = asyncio.run(run_planner_check(settings.model))
    except OpenAIPlannerCheckError as exc:
        typer.echo(f"planner check: failed; {redact_sensitive_text(str(exc))}")
        raise typer.Exit(code=1)
    typer.echo(
        f"planner check: passed; provider=openai model={result.model_name}"
    )
    typer.echo(f"API invoked: {result.api_invoked}")
    typer.echo(f"prompt templates rendered: {result.rendered_prompt_count}")
    typer.echo("prompt names: " + ", ".join(result.prompt_names))
    typer.echo(
        "parsed response: "
        + json.dumps(result.parsed_response.model_dump(mode="json"), sort_keys=True)
    )


@app.command("storage-check")
def storage_check() -> None:
    """Verify the selected storage backend and initialize MongoDB indexes."""

    import asyncio

    from .persistence import MongoRepairPlatformRepository

    RepairBotSettings.from_env()
    uri = os.getenv("MONGODB_URI")
    database_name = os.getenv("MONGODB_DB")
    if not uri and not database_name:
        typer.echo("storage mode: memory")
        typer.echo("healthy: true")
        typer.echo("MongoDB check: skipped because MONGODB_URI and MONGODB_DB are not set")
        return
    missing = [
        name
        for name, value in (("MONGODB_URI", uri), ("MONGODB_DB", database_name))
        if not value
    ]
    if missing:
        typer.echo("storage mode: invalid MongoDB configuration")
        typer.echo("missing settings: " + ", ".join(missing))
        raise typer.Exit(code=1)

    async def execute() -> tuple[str, int]:
        repository = MongoRepairPlatformRepository.from_env()
        try:
            if not await repository.ping():
                raise RuntimeError("MongoDB ping returned an unhealthy response")
            indexes = await repository.ensure_indexes()
            return repository.database_name, len(indexes)
        finally:
            repository.close()

    try:
        selected_database, index_count = asyncio.run(execute())
    except Exception:
        typer.echo("storage mode: mongodb")
        typer.echo("healthy: false")
        typer.echo("MongoDB check failed; verify connectivity and credentials")
        raise typer.Exit(code=1)
    typer.echo("storage mode: mongodb")
    typer.echo("healthy: true")
    typer.echo(f"database: {redact_sensitive_text(selected_database)}")
    typer.echo(f"indexes initialized: {index_count}")


@app.command("jira-check")
def jira_check(
    allow_network: Annotated[
        bool,
        typer.Option(
            "--allow-network",
            help="Explicitly call Jira Cloud /rest/api/3/myself.",
        ),
    ] = False,
) -> None:
    """Validate real Jira configuration and optionally test connectivity."""

    import asyncio

    from .integrations.jira import RealJiraClient, build_jira_client
    from .platform_config import DEFAULT_CONFIG_PATH, load_config_file

    RepairBotSettings.from_env()
    required = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        typer.echo("jira mode: unavailable")
        typer.echo("missing settings: " + ", ".join(missing))
        raise typer.Exit(code=1)
    try:
        client = build_jira_client("real")
    except (RuntimeError, ValueError):
        typer.echo("jira mode: invalid real configuration")
        raise typer.Exit(code=1)
    if not isinstance(client, RealJiraClient):
        raise typer.Exit(code=1)
    project_key = os.getenv("JIRA_PROJECT_KEY")
    if not project_key and DEFAULT_CONFIG_PATH.is_file():
        project_key = load_config_file(DEFAULT_CONFIG_PATH).jira.project_key
    project_key = project_key or "not set"
    typer.echo("jira mode: real")
    typer.echo(f"base URL: {client.base_url}")
    typer.echo(f"project key: {redact_sensitive_text(project_key)}")
    if not allow_network:
        asyncio.run(client.aclose())
        typer.echo("network check: skipped; pass --allow-network to call Jira")
        return

    async def execute() -> bool:
        try:
            return await client.health_check()
        finally:
            await client.aclose()

    try:
        healthy = asyncio.run(execute())
    except Exception:
        typer.echo("network check: failed; verify Jira URL, access, and credentials")
        raise typer.Exit(code=1)
    typer.echo(f"network check: {'passed' if healthy else 'failed'}")
    if not healthy:
        raise typer.Exit(code=1)


@app.command("safety-check")
def safety_check(
    path: Annotated[Path, typer.Option("--path", exists=True)] = Path("."),
) -> None:
    """Scan for common public-GitHub safety issues before pushing."""

    findings = scan_path(path)
    if not findings:
        typer.echo("PASS: no obvious public GitHub safety issues found")
        return
    typer.echo("FAIL: possible public GitHub safety issues found")
    for finding in findings:
        typer.echo(f"- {finding.path}: {finding.issue_type} ({finding.message})")
    raise typer.Exit(code=1)


@app.command("install-safety-hook")
def install_safety_hook(
    path: Annotated[Path, typer.Option("--path", exists=True, file_okay=False)] = Path("."),
) -> None:
    """Install an optional pre-commit hook that runs safety-check."""

    hook = install_pre_commit_hook(path)
    typer.echo(f"installed safety hook: {hook}")


@app.command("init-env")
def init_env(
    path: Annotated[Path, typer.Option("--path", help="Repository path where .env should be created.")] = Path("."),
) -> None:
    """Create a local .env from .env.example without adding secrets."""

    target_dir = path.resolve()
    example = target_dir / ".env.example"
    target = target_dir / ".env"
    if not example.exists():
        raise typer.BadParameter(f"No .env.example found at {example}")
    if target.exists():
        typer.echo(f"{target} already exists; leaving it unchanged.")
        return
    shutil.copyfile(example, target)
    typer.echo(f"Created {target} from .env.example with blank placeholders.")
    typer.echo("Paste your API keys manually into .env when needed.")
    typer.echo(".env is ignored by Git; never ask Codex or any assistant to store or reveal real keys.")


@app.command("ai-smoke-test")
def ai_smoke_test(
    allow_api: Annotated[
        bool,
        typer.Option("--allow-api", help="Required. This makes one tiny OpenAI API call and may spend a small amount of API credit."),
    ] = False,
) -> None:
    """Run one explicit tiny OpenAI API call to validate local configuration."""

    _require_api_allowed(allow_api, "ai-smoke-test")
    typer.echo("WARNING: ai-smoke-test makes one tiny OpenAI API call and may spend API credit.")
    try:
        typer.echo(run_openai_smoke_test())
    except OpenAISmokeError as exc:
        typer.echo(f"OpenAI smoke test could not run: {exc}")
        raise typer.Exit(code=1) from exc


def _resolve_jira_fix_service_config(
    *,
    service_name: str,
    repo_root: Path,
    services_config_path: Path | None,
) -> ServiceConfig | None:
    if services_config_path is None:
        return None
    config = load_services_config(services_config_path)
    service_config = get_service_config(config, service_name)
    resolve_service_repo_path(
        repo_root=repo_root,
        service=service_config,
        services_config_path=services_config_path,
    )
    return service_config


def _create_jira_fix_artifact_dir(repo_root: Path, ticket: str) -> Path:
    safe_ticket = re.sub(r"[^A-Za-z0-9._-]+", "-", ticket.strip()).strip("-._") or "ticket"
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    path = repo_root / ".tmp" / "microservice-ai-repair-bot" / "jira-fix" / safe_ticket / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _print_plan(plan) -> None:
    typer.echo("\n--- structured repair plan ---")
    typer.echo(plan_to_markdown(plan))


def _print_failed_attempt(attempt: FixAttempt, *, reflection_path: Path | None = None) -> None:
    typer.echo("\nfailed repair attempt")
    typer.echo(f"attempt: {attempt.attempt}")
    typer.echo("changed files:")
    if attempt.changed_files:
        for file in attempt.changed_files:
            typer.echo(f"- {file}")
    else:
        typer.echo("- none")
    typer.echo("git diff --stat:")
    typer.echo(attempt.diff_stat or "(empty)")
    typer.echo(f"test command: {attempt.test_cmd or '(not run)'}")
    typer.echo(
        "exit code: "
        f"{attempt.test_exit_code if attempt.test_exit_code is not None else '(not run)'}"
    )
    typer.echo("last 100 lines of test output:")
    typer.echo(_last_lines(attempt.test_output or "(no test output)", 100))
    typer.echo(f"artifacts: {attempt.artifact_path or '(none)'}")
    if reflection_path:
        typer.echo(f"reflection: {reflection_path}")
    if attempt.rejection_reason:
        typer.echo(f"reason: {attempt.rejection_reason}")


def _last_lines(text: str, count: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-count:]) if lines else text


def _restore_dry_run_changes(repo_root: Path, artifact_dir: Path) -> list[str]:
    changed = git_ops.changed_files(repo_root)
    final_dir = artifact_dir / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    (final_dir / "changed-files.txt").write_text(
        "\n".join(changed) + ("\n" if changed else ""),
        encoding="utf-8",
    )
    diff_stat = git_ops.diff_stat(repo_root)
    if diff_stat:
        (final_dir / "diff.stat").write_text(diff_stat, encoding="utf-8")
    diff_patch = git_ops.diff_patch(repo_root)
    if diff_patch:
        (final_dir / "diff.patch").write_text(redact_sensitive_text(diff_patch), encoding="utf-8")
    if changed:
        git_ops.restore_worktree(repo_root)
    return changed


def _print_dry_run_restore(restored: list[str], artifact_dir: Path) -> None:
    typer.echo("dry-run restore complete")
    typer.echo(f"artifacts: {artifact_dir}")
    if restored:
        typer.echo("restored files:")
        for file in restored:
            typer.echo(f"- {file}")
    else:
        typer.echo("restored files: none")


def _validate_repo(repo: Path, *, allow_dirty: bool) -> Path:
    repo_root = git_ops.ensure_git_repo(repo)
    git_ops.ensure_clean_worktree(repo_root, allow_dirty=allow_dirty)
    return repo_root


def _print_report(state: RepairState, *, include_diff: bool) -> None:
    typer.echo("\nrepairbot final report")
    typer.echo("======================")
    typer.echo(f"status: {state.status}")
    typer.echo(f"service: {state.service_name}")
    typer.echo("\ndiagnosis:")
    typer.echo(state.diagnosis or "(none)")
    typer.echo("\nfix plan:")
    typer.echo(state.fix_plan or "(none)")
    typer.echo("\nchanged files:")
    if state.changed_files:
        for file in state.changed_files:
            typer.echo(f"- {file}")
    else:
        typer.echo("- none")
    typer.echo("\ntests:")
    typer.echo(f"passed: {state.tests_passed}")
    typer.echo((state.test_output or "(no test output)")[-4000:])
    if include_diff:
        typer.echo("\ndiff:")
        typer.echo(state.diff or "(empty)")
    typer.echo(f"\nbranch: {state.branch_name or '(none)'}")
    typer.echo(f"commit: {state.commit_sha or '(none)'}")
    typer.echo(f"pr: {state.pr_url or '(none)'}")


def _require_api_allowed(allow_api: bool, command_name: str) -> None:
    if allow_api:
        typer.echo(f"API access explicitly allowed for {command_name}; provider calls may spend credits.")
        return
    typer.echo(
        f"{command_name} may call an AI provider and spend API credits. "
        "Re-run with --allow-api after confirming your .env/shell credentials."
    )
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
