"""Structured reason-before-execute planning for Jira repair."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import RepairBotSettings
from .models import (
    ErrorCluster,
    JiraTicketRecord,
    RepairHypothesis,
    RepairObservation,
    RepairPlan,
    RepairReflection,
    ServiceConfig,
)
from .redaction import redact_sensitive_text
from .tools import shell


def build_repair_plan(
    *,
    ticket: JiraTicketRecord,
    cluster: ErrorCluster | None,
    selected_service: str,
    service_root: str | Path,
    service: ServiceConfig | None,
    test_cmd: str,
    settings: RepairBotSettings,
    max_hypotheses: int = 3,
) -> RepairPlan:
    root = Path(service_root).resolve()
    source_files = _java_files(root / "src" / "main" / "java", settings.max_files_to_read)
    test_files = _java_files(root / "src" / "test" / "java", settings.max_files_to_read)
    relevant_source = _prioritize_files(source_files, cluster)
    relevant_tests = _prioritize_files(test_files, cluster)
    observations = _build_observations(
        ticket=ticket,
        cluster=cluster,
        service_root=root,
        source_files=relevant_source,
        test_files=relevant_tests,
        test_cmd=test_cmd,
    )
    hypotheses = _build_hypotheses(
        cluster=cluster,
        service_name=selected_service,
        source_files=relevant_source,
        test_files=relevant_tests,
        max_hypotheses=max_hypotheses,
    )
    selected = _select_hypothesis(hypotheses)
    plan_id = _plan_id(ticket.jira_key, selected_service, cluster.cluster_id if cluster else "", selected.hypothesis_id if selected else "")
    display_test_cmd = redact_sensitive_text(test_cmd)
    planned_edits = [
        f"Inspect and minimally edit {path}" for path in (selected.files_likely_to_change if selected else relevant_source[:1])
    ] or ["Inspect the selected service source files before editing."]
    planned_tests = [display_test_cmd]
    assumptions = [
        "The stored Jira ticket and local error cluster represent the same production failure.",
        "The fix should be limited to the selected service unless the plan evidence proves otherwise.",
        "Tests must not be weakened to make a repair pass.",
    ]
    safety_notes = [
        "Do not edit .env, credentials, deployment, CI, or unrelated service files.",
        "Prefer the smallest code change with direct evidence and a regression-friendly test surface.",
        "Structured artifacts are summaries for review, not raw private chain-of-thought.",
    ]
    return RepairPlan(
        plan_id=plan_id,
        ticket_id=ticket.jira_key,
        selected_service=selected_service,
        service_root=str(root),
        cluster_id=cluster.cluster_id if cluster else None,
        observations=observations,
        assumptions=assumptions,
        hypotheses=hypotheses,
        selected_hypothesis=selected,
        selection_reason=(
            "Selected for strongest local evidence, smallest service-scoped change, "
            "low blast radius, and testability without weakening tests."
            if selected
            else "No hypothesis could be selected from available evidence."
        ),
        planned_edits=planned_edits,
        planned_tests=planned_tests,
        success_criteria=[
            "Selected service tests pass.",
            "Patch remains scoped to the selected service unless explicitly justified.",
            "No secrets, deployment files, generated artifacts, or unrelated services are modified.",
        ],
        safety_notes=safety_notes,
        relevant_source_files=relevant_source,
        relevant_test_files=relevant_tests,
        baseline_test_status=_baseline_test_status(root, test_cmd),
    )


def write_repair_plan_artifacts(plan: RepairPlan, artifact_dir: str | Path) -> tuple[Path, Path]:
    root = Path(artifact_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "plan.json"
    markdown_path = root / "plan.md"
    json_path.write_text(
        json.dumps(plan.model_dump(), indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(plan_to_markdown(plan), encoding="utf-8")
    return json_path, markdown_path


def plan_to_markdown(plan: RepairPlan) -> str:
    selected = plan.selected_hypothesis
    lines = [
        f"# Repair Plan {plan.plan_id}",
        "",
        f"- Ticket: `{plan.ticket_id}`",
        f"- Service: `{plan.selected_service}`",
        f"- Service root: `{plan.service_root}`",
        f"- Cluster: `{plan.cluster_id or 'none'}`",
        f"- Baseline test status: {plan.baseline_test_status}",
        "",
        "## Observations",
        *_bullets(f"{item.category}: {item.summary}" + (f" Evidence: {item.evidence}" if item.evidence else "") for item in plan.observations),
        "",
        "## Assumptions",
        *_bullets(plan.assumptions),
        "",
        "## Hypotheses",
    ]
    for hypothesis in plan.hypotheses:
        lines.extend(
            [
                f"- `{hypothesis.hypothesis_id}` {hypothesis.suspected_root_cause}",
                f"  Evidence: {hypothesis.evidence}",
                f"  Proposed change: {hypothesis.proposed_code_change}",
                f"  Expected test impact: {hypothesis.expected_test_impact}",
                f"  Risk: {hypothesis.risk_level}; confidence: {hypothesis.confidence_score:.2f}",
                f"  Files: {', '.join(hypothesis.files_likely_to_change) or 'none identified'}",
            ]
        )
    lines.extend(
        [
            "",
            "## Selected Hypothesis",
            f"`{selected.hypothesis_id}` {selected.suspected_root_cause}" if selected else "None",
            plan.selection_reason or "",
            "",
            "## Planned Edits",
            *_bullets(plan.planned_edits),
            "",
            "## Planned Tests",
            *_bullets(plan.planned_tests),
            "",
            "## Success Criteria",
            *_bullets(plan.success_criteria),
            "",
            "## Safety Notes",
            *_bullets(plan.safety_notes),
            "",
        ]
    )
    return redact_sensitive_text("\n".join(lines))


def build_repair_reflection(
    *,
    plan: RepairPlan,
    attempt_number: int,
    hypothesis_id: str | None,
    tests_passed: bool,
    test_exit_code: int | None,
    test_output: str | None,
) -> RepairReflection:
    failed_summary = "\n".join(redact_sensitive_text(test_output).splitlines()[-40:])
    next_hypothesis = _next_hypothesis_id(plan, hypothesis_id)
    if tests_passed:
        decision = "stop"
        next_id = None
        rationale = "Tests passed, so no retry is needed."
        post = "The selected repair satisfied the configured test command."
    elif next_hypothesis:
        decision = "try_next_hypothesis"
        next_id = next_hypothesis
        rationale = "The failed test output did not validate the current hypothesis; try the next lower-blast-radius candidate."
        post = "Use the failure output as new evidence and avoid repeating the same patch shape."
    else:
        decision = "adjust_same_hypothesis"
        next_id = hypothesis_id
        rationale = "No unused hypothesis remains; adjust the selected hypothesis using the captured failure output."
        post = "Keep the change service-scoped and do not weaken tests."
    reflection_id = _plan_id(plan.plan_id, str(attempt_number), hypothesis_id or "", str(test_exit_code))
    return RepairReflection(
        reflection_id=reflection_id,
        plan_id=plan.plan_id,
        ticket_id=plan.ticket_id,
        attempt=attempt_number,
        hypothesis_id=hypothesis_id,
        tests_passed=tests_passed,
        test_exit_code=test_exit_code,
        failed_test_summary=failed_summary,
        decision=decision,
        next_hypothesis_id=next_id,
        rationale=rationale,
        post_test_reflection=post,
    )


def write_reflection_artifacts(reflection: RepairReflection, attempt_dir: str | Path) -> tuple[Path, Path]:
    root = Path(attempt_dir)
    root.mkdir(parents=True, exist_ok=True)
    reflection.artifact_path = str(root.resolve())
    json_path = root / "reflection.json"
    markdown_path = root / "reflection.md"
    json_path.write_text(
        json.dumps(reflection.model_dump(), indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(reflection_to_markdown(reflection), encoding="utf-8")
    return json_path, markdown_path


def reflection_to_markdown(reflection: RepairReflection) -> str:
    return redact_sensitive_text(
        "\n".join(
            [
                f"# Repair Reflection {reflection.reflection_id}",
                "",
                f"- Ticket: `{reflection.ticket_id}`",
                f"- Plan: `{reflection.plan_id}`",
                f"- Attempt: {reflection.attempt}",
                f"- Hypothesis: `{reflection.hypothesis_id or 'none'}`",
                f"- Tests passed: {reflection.tests_passed}",
                f"- Exit code: {reflection.test_exit_code if reflection.test_exit_code is not None else 'not run'}",
                f"- Decision: {reflection.decision}",
                f"- Next hypothesis: `{reflection.next_hypothesis_id or 'none'}`",
                "",
                "## Failure Summary",
                reflection.failed_test_summary or "No failure output captured.",
                "",
                "## Rationale",
                reflection.rationale,
                "",
                "## Post-Test Reflection",
                reflection.post_test_reflection,
                "",
            ]
        )
    )


def _build_observations(
    *,
    ticket: JiraTicketRecord,
    cluster: ErrorCluster | None,
    service_root: Path,
    source_files: list[str],
    test_files: list[str],
    test_cmd: str,
) -> list[RepairObservation]:
    observations = [
        RepairObservation(
            category="ticket",
            summary=redact_sensitive_text(ticket.summary),
            evidence=f"local storage key {ticket.jira_key}",
        ),
        RepairObservation(
            category="service",
            summary=f"Selected service root is {service_root}",
            evidence="resolved from --services-config" if service_root.exists() else "provided repo root fallback",
        ),
        RepairObservation(
            category="tests",
            summary=f"Configured test command is `{redact_sensitive_text(test_cmd)}`",
            evidence="provided --test-cmd or service config/default detection",
        ),
    ]
    if cluster:
        location = ".".join(part for part in [cluster.class_name, cluster.method_name] if part)
        observations.append(
            RepairObservation(
                category="cluster",
                summary=redact_sensitive_text(
                    f"{cluster.error_type or 'Error'} in {location or cluster.file_name or 'unknown location'}"
                ),
                evidence=redact_sensitive_text(
                    cluster.stack_trace_excerpt or cluster.raw_log_excerpt or cluster.normalized_error_message or ""
                ),
            )
        )
    observations.append(
        RepairObservation(
            category="files",
            summary=f"{len(source_files)} source files and {len(test_files)} test files are relevant candidates.",
            evidence=", ".join([*source_files[:3], *test_files[:3]]),
        )
    )
    return observations


def _build_hypotheses(
    *,
    cluster: ErrorCluster | None,
    service_name: str,
    source_files: list[str],
    test_files: list[str],
    max_hypotheses: int,
) -> list[RepairHypothesis]:
    candidates: list[RepairHypothesis] = []
    likely_files = _likely_files(source_files, test_files)
    if cluster and cluster.hypotheses:
        for index, item in enumerate(cluster.hypotheses, start=1):
            candidates.append(
                RepairHypothesis(
                    hypothesis_id=f"H{index}",
                    suspected_root_cause=redact_sensitive_text(item.hypothesis),
                    evidence=redact_sensitive_text(item.evidence),
                    proposed_code_change="Apply the smallest service-local code change that directly addresses this hypothesis.",
                    expected_test_impact="Existing service tests should pass; add or preserve regression coverage where present.",
                    risk_level="LOW" if item.confidence >= 0.7 else "MEDIUM",
                    confidence_score=item.confidence,
                    files_likely_to_change=likely_files,
                )
            )
    if cluster:
        location = cluster.class_name or cluster.file_name or service_name
        candidates.append(
            RepairHypothesis(
                hypothesis_id=f"H{len(candidates) + 1}",
                suspected_root_cause=redact_sensitive_text(
                    f"{cluster.error_type or 'Runtime error'} is not handled defensively near {location}."
                ),
                evidence=redact_sensitive_text(
                    cluster.stack_trace_excerpt or cluster.raw_log_excerpt or cluster.normalized_error_message or "Stored cluster points at the selected service."
                ),
                proposed_code_change="Add a narrow validation or null/edge-case guard at the failing service boundary.",
                expected_test_impact="The failing scenario should pass without changing test assertions.",
                risk_level="LOW",
                confidence_score=0.76,
                files_likely_to_change=likely_files,
            )
        )
    candidates.extend(
        [
            RepairHypothesis(
                hypothesis_id=f"H{len(candidates) + 1}",
                suspected_root_cause=f"{service_name} has an unhandled edge case in request/domain validation.",
                evidence="Selected service owns the stored ticket and candidate source files.",
                proposed_code_change="Tighten input/state validation in the smallest relevant source file.",
                expected_test_impact="Service tests should continue to pass and cover the edge case if a test exists.",
                risk_level="MEDIUM",
                confidence_score=0.58,
                files_likely_to_change=likely_files,
            ),
            RepairHypothesis(
                hypothesis_id=f"H{len(candidates) + 2}",
                suspected_root_cause="The implementation and regression test expectations disagree on an edge case.",
                evidence="Relevant test files are available for inspection, but tests must not be weakened.",
                proposed_code_change="Prefer production-code correction; add regression coverage only if it strengthens assertions.",
                expected_test_impact="Tests should pass because behavior is corrected, not because assertions are relaxed.",
                risk_level="MEDIUM",
                confidence_score=0.45,
                files_likely_to_change=likely_files,
            ),
        ]
    )
    unique: list[RepairHypothesis] = []
    seen = set()
    for item in candidates:
        key = item.suspected_root_cause
        if key in seen:
            continue
        seen.add(key)
        item.hypothesis_id = f"H{len(unique) + 1}"
        unique.append(item)
        if len(unique) >= max(1, max_hypotheses):
            break
    return unique


def _select_hypothesis(hypotheses: list[RepairHypothesis]) -> RepairHypothesis | None:
    if not hypotheses:
        return None
    risk_weight = {"LOW": 0.2, "MEDIUM": 0.1, "HIGH": -0.2}
    return max(
        hypotheses,
        key=lambda item: (
            item.confidence_score + risk_weight[item.risk_level],
            len(item.files_likely_to_change) <= 2,
            -len(item.files_likely_to_change),
        ),
    )


def _java_files(root: Path, limit: int) -> list[str]:
    if not root.exists():
        return []
    base = root.parents[2] if len(root.parents) > 2 else root
    return [
        path.relative_to(base).as_posix()
        for path in sorted(root.rglob("*.java"))[:limit]
        if path.is_file()
    ]


def _prioritize_files(files: list[str], cluster: ErrorCluster | None) -> list[str]:
    if not cluster or not cluster.file_name:
        return files
    preferred = [path for path in files if path.endswith(cluster.file_name)]
    rest = [path for path in files if path not in preferred]
    return [*preferred, *rest]


def _likely_files(source_files: list[str], test_files: list[str]) -> list[str]:
    return [*source_files[:1], *test_files[:1]]


def _baseline_test_status(service_root: Path, test_cmd: str) -> str:
    if not _is_cheap_test_command(test_cmd):
        return "skipped: command is not in the cheap local planning allowlist"
    try:
        result = shell.run_test_command(service_root, test_cmd, timeout=120)
    except Exception as exc:
        return f"error: {redact_sensitive_text(str(exc))}"
    return f"exit_code={result.returncode}"


def _is_cheap_test_command(test_cmd: str) -> bool:
    normalized = test_cmd.strip().lower()
    return (
        normalized.startswith("python -c ")
        or normalized.startswith("python -m pytest")
        or normalized.startswith("pytest ")
        or normalized == "pytest"
    )


def _next_hypothesis_id(plan: RepairPlan, current: str | None) -> str | None:
    ids = [item.hypothesis_id for item in plan.hypotheses]
    if not ids:
        return None
    if current not in ids:
        return ids[0]
    index = ids.index(current)
    return ids[index + 1] if index + 1 < len(ids) else None


def _plan_id(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _bullets(values) -> list[str]:
    items = list(values)
    return [f"- {redact_sensitive_text(str(item))}" for item in items] or ["- none"]
