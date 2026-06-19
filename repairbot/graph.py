"""LangGraph workflow for diagnose, repair, test, and PR preparation."""

from __future__ import annotations

from typing import Any, Literal

from . import agents
from .config import RepairBotSettings
from .state import RepairState
from .tools import filesystem, git_ops, logs, safety, shell


RouteName = Literal["plan_fix", "create_branch_commit", "final_report"]


def build_graph(settings: RepairBotSettings | None = None) -> Any:
    settings = settings or RepairBotSettings.from_env()
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return LocalRepairGraph(settings)

    builder = StateGraph(dict)
    builder.add_node("load_logs", _wrap(load_logs, settings))
    builder.add_node("analyze_logs", _wrap(analyze_logs, settings))
    builder.add_node("inspect_repo", _wrap(inspect_repo, settings))
    builder.add_node("plan_fix", _wrap(plan_fix, settings))
    builder.add_node("apply_fix", _wrap(apply_fix, settings))
    builder.add_node("get_diff", _wrap(get_diff, settings))
    builder.add_node("run_tests", _wrap(run_tests, settings))
    builder.add_node("create_branch_commit", _wrap(create_branch_commit, settings))
    builder.add_node("open_pull_request", _wrap(open_pull_request, settings))
    builder.add_node("final_report", _wrap(final_report, settings))

    builder.add_edge(START, "load_logs")
    builder.add_edge("load_logs", "analyze_logs")
    builder.add_edge("analyze_logs", "inspect_repo")
    builder.add_edge("inspect_repo", "plan_fix")
    builder.add_edge("plan_fix", "apply_fix")
    builder.add_edge("apply_fix", "get_diff")
    builder.add_edge("get_diff", "run_tests")
    builder.add_conditional_edges(
        "run_tests",
        route_after_tests,
        {
            "plan_fix": "plan_fix",
            "create_branch_commit": "create_branch_commit",
            "final_report": "final_report",
        },
    )
    builder.add_edge("create_branch_commit", "open_pull_request")
    builder.add_edge("open_pull_request", "final_report")
    builder.add_edge("final_report", END)
    return builder.compile()


class LocalRepairGraph:
    """Tiny fallback runner used when LangGraph is not installed locally."""

    def __init__(self, settings: RepairBotSettings) -> None:
        self.settings = settings

    def invoke(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        state = RepairState.model_validate(initial_state)
        for node in (load_logs, analyze_logs, inspect_repo, plan_fix, apply_fix, get_diff, run_tests):
            state = node(state, self.settings)
        while route_after_tests(state.as_dict()) == "plan_fix":
            state = plan_fix(state, self.settings)
            state = apply_fix(state, self.settings)
            state = get_diff(state, self.settings)
            state = run_tests(state, self.settings)
        if route_after_tests(state.as_dict()) == "create_branch_commit":
            state = create_branch_commit(state, self.settings)
            state = open_pull_request(state, self.settings)
        state = final_report(state, self.settings)
        return state.as_dict()


def route_after_tests(state: dict[str, Any]) -> RouteName:
    repair_state = RepairState.model_validate(state)
    if repair_state.tests_passed:
        if repair_state.open_pr and not repair_state.dry_run:
            return "create_branch_commit"
        return "final_report"
    if repair_state.retry_count < repair_state.max_retries:
        return "plan_fix"
    return "final_report"


def load_logs(state: RepairState, settings: RepairBotSettings) -> RepairState:
    del settings
    state.raw_logs = logs.read_logs(state.logs_path)
    return state


def analyze_logs(state: RepairState, settings: RepairBotSettings) -> RepairState:
    state.log_summary = logs.summarize_logs_heuristic(state.raw_logs)
    hints = logs.extract_log_hints(state.raw_logs)
    state.diagnosis = (
        f"Log hints for service `{state.service_name}`:\n"
        f"- errors: {', '.join(hints['errors']) or 'none found'}\n"
        f"- files: {', '.join(hints['files']) or 'none found'}\n"
        f"- endpoints: {', '.join(hints['endpoints']) or 'none found'}\n\n"
        f"Summary:\n{state.log_summary}"
    )
    state.status = "diagnosed"
    return state


def inspect_repo(state: RepairState, settings: RepairBotSettings) -> RepairState:
    del settings
    hints = logs.extract_log_hints(state.raw_logs)
    suspected: list[str] = []
    for file_hint in hints["files"]:
        name = file_hint.rsplit("/", 1)[-1]
        suspected.extend(filesystem.search_repo(state.repo_path, name, limit=10))
    for token in [*hints["errors"], *hints["endpoints"], state.service_name]:
        suspected.extend(filesystem.search_repo(state.repo_path, token, limit=10))
    state.suspected_files = _dedupe(suspected)[:25]
    return state


def plan_fix(state: RepairState, settings: RepairBotSettings) -> RepairState:
    if state.status == "tests_failed":
        state.retry_count += 1
    suspected = "\n".join(f"- {file}" for file in state.suspected_files) or "- None"
    retry_note = (
        f"\nPrevious test output:\n{state.test_output[-3000:]}"
        if state.test_output and state.retry_count
        else ""
    )
    state.fix_plan = (
        "Make the smallest safe code change that addresses the production log signal.\n"
        f"Suspected files:\n{suspected}\n"
        f"Diagnosis:\n{state.diagnosis or 'No diagnosis available.'}"
        f"{retry_note}"
    )
    return state


def apply_fix(state: RepairState, settings: RepairBotSettings) -> RepairState:
    if state.diagnose_only:
        state.fix_plan = (
            f"{state.fix_plan}\n\nDiagnose-only run: no file edits were attempted."
        )
        state.status = "diagnosed"
        return state

    context = agents.RepoToolContext(
        repo_path=state.repo_path,
        allow_sensitive_edits=state.allow_sensitive_edits,
        allow_deploy_edits=state.allow_deploy_edits,
    )
    task = (
        "Apply a minimal production bug fix using only the provided tools.\n"
        "Do not edit blocked files. Do not create broad rewrites.\n\n"
        f"Service: {state.service_name}\n"
        f"Diagnosis:\n{state.diagnosis}\n\n"
        f"Fix plan:\n{state.fix_plan}\n"
    )
    try:
        agent_result = agents.invoke_fix_agent(settings=settings, context=context, task=task)
        state.fix_plan = f"{state.fix_plan}\n\nAgent result:\n{agent_result}"
        state.status = "patched"
    except agents.AgentUnavailableError as exc:
        state.status = "failed"
        state.fix_plan = (
            f"{state.fix_plan}\n\nAI repair was not applied: {exc}\n"
            "Install LangChain/LangGraph dependencies and configure a provider API key, "
            "or use `diagnose` for local-only analysis."
        )
    return state


def get_diff(state: RepairState, settings: RepairBotSettings) -> RepairState:
    del settings
    state.diff = git_ops.diff(state.repo_path)
    safety.assert_small_diff(state.diff or "")
    state.changed_files = git_ops.changed_files(state.repo_path)
    return state


def run_tests(state: RepairState, settings: RepairBotSettings) -> RepairState:
    del settings
    if not state.test_cmd:
        state.tests_passed = False
        state.status = "failed"
        state.test_output = "No test command was provided."
        return state
    result = shell.run_test_command(state.repo_path, state.test_cmd)
    state.test_output = result.combined_output or f"Command exited with {result.returncode}"
    state.tests_passed = result.returncode == 0
    state.status = "tests_passed" if state.tests_passed else "tests_failed"
    return state


def create_branch_commit(state: RepairState, settings: RepairBotSettings) -> RepairState:
    del settings
    if not state.tests_passed:
        raise RuntimeError("Refusing to commit before tests pass")
    if not state.changed_files:
        raise RuntimeError("No changed files to commit")
    branch = git_ops.make_branch_name(state.service_name)
    state.branch_name = branch
    print("\n--- repairbot diff before commit ---\n")
    print(state.diff or "(empty diff)")
    git_ops.create_branch(state.repo_path, branch)
    state.commit_sha = git_ops.commit_changes(
        state.repo_path,
        state.changed_files,
        f"AI repair for {state.service_name}",
    )
    git_ops.push_branch(state.repo_path, branch)
    return state


def open_pull_request(state: RepairState, settings: RepairBotSettings) -> RepairState:
    if not state.open_pr:
        return state
    if not state.github_repo:
        raise RuntimeError("--github-repo is required with --open-pr")
    if not state.branch_name:
        raise RuntimeError("No branch was created for the pull request")
    state.pr_url = agents.open_pull_request_from_state(
        github_repo=state.github_repo,
        branch_name=state.branch_name,
        base_branch=state.base_branch,
        service_name=state.service_name,
        diagnosis=state.diagnosis,
        fix_plan=state.fix_plan,
        changed_files=state.changed_files,
        test_cmd=state.test_cmd,
        test_output=state.test_output,
        token=settings.github_token,
    )
    state.status = "pr_opened"
    return state


def final_report(state: RepairState, settings: RepairBotSettings) -> RepairState:
    del settings
    if state.status == "tests_failed" and state.retry_count >= state.max_retries:
        state.status = "failed"
    return state


def _wrap(func: Any, settings: RepairBotSettings) -> Any:
    def wrapped(raw_state: dict[str, Any]) -> dict[str, Any]:
        state = RepairState.model_validate(raw_state)
        return func(state, settings).as_dict()

    return wrapped


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
