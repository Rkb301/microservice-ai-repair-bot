"""Streaming Java log ingestion, summarization, and clustering."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Iterator

from .config import RepairBotSettings
from .models import (
    ErrorCluster,
    Hypothesis,
    LogSummary,
    ParsedLogEntry,
    RejectedHypothesis,
    ServiceConfig,
)


LOG_RE = re.compile(
    r"^(?P<timestamp>\S+)\s+"
    r"(?P<level>INFO|WARN|ERROR|DEBUG)\s+"
    r"\[(?P<service>[^\]]+)\]\s+"
    r"\[(?P<application>[^\]]+)\]\s+"
    r"\[traceId=(?P<trace_id>[^\]]+)\]\s+"
    r"(?:\[spanId=(?P<span_id>[^\]]+)\]\s+)?"
    r"\[(?:requestId|correlationId)=(?P<request_id>[^\]]+)\]\s+"
    r"\[(?P<method>GET|POST|PUT|PATCH|DELETE)\s+(?P<endpoint>[^\]]+)\]\s+"
    r"\[status=(?P<status>\d{3})\]\s+"
    r"\[durationMs=(?P<duration>\d+)\]\s+"
    r"(?:(?P<package_class>[A-Za-z_][\w.]*?)\.(?P<method_name>[A-Za-z_][\w]*)"
    r"\((?P<file_name>[\w.-]+):(?P<line_number>\d+)\)\s+-\s+)?"
    r"(?P<message>.*)$"
)

ERROR_RE = re.compile(r"(?P<error_type>[A-Za-z_][\w.]*?(?:Exception|Error)):\s*(?P<message>.*)")
STACK_AT_RE = re.compile(
    r"\s+at\s+(?P<package_class>[A-Za-z_][\w.]*?)\.(?P<method_name>[A-Za-z_][\w]*)"
    r"\((?P<file_name>[\w.-]+):(?P<line_number>\d+)\)"
)


def stream_log_blocks(path: str | Path) -> Iterator[tuple[int, int, list[str]]]:
    current: list[str] = []
    start_line = 0
    path = Path(path)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.rstrip("\n")
            if LOG_RE.match(line):
                if current:
                    yield start_line, line_number - 1, current
                current = [line]
                start_line = line_number
            elif current and _is_stack_line(line):
                current.append(line)
            elif current:
                yield start_line, line_number - 1, current
                current = []
                start_line = 0
        if current:
            yield start_line, start_line + len(current) - 1, current


def parse_log_block(
    *,
    run_id: str,
    source_file: str | Path,
    line_start: int,
    line_end: int,
    lines: list[str],
    fallback_service: str | None = None,
) -> ParsedLogEntry | None:
    if not lines:
        return None
    match = LOG_RE.match(lines[0])
    if not match:
        return None
    groups = match.groupdict()
    message = groups.get("message") or ""
    error_match = ERROR_RE.search(message)
    package_class = groups.get("package_class")
    class_name = _short_class(package_class)
    package_name = _package_name(package_class)
    method_name = groups.get("method_name")
    file_name = groups.get("file_name")
    line_number = _int_or_none(groups.get("line_number"))

    stack_trace = "\n".join(lines[1:]) or None
    if stack_trace:
        first_stack = next((STACK_AT_RE.match(line) for line in lines[1:] if STACK_AT_RE.match(line)), None)
        if first_stack and (not class_name or groups["level"] == "ERROR"):
            stack_groups = first_stack.groupdict()
            package_class = stack_groups.get("package_class")
            class_name = _short_class(package_class)
            package_name = _package_name(package_class)
            method_name = stack_groups.get("method_name")
            file_name = stack_groups.get("file_name")
            line_number = _int_or_none(stack_groups.get("line_number"))

    return ParsedLogEntry(
        run_id=run_id,
        source_file=str(Path(source_file)),
        line_start=line_start,
        line_end=line_end,
        timestamp=groups.get("timestamp"),
        level=groups["level"],
        service_name=groups.get("service") or fallback_service or "unknown-service",
        application_name=groups.get("application"),
        trace_id=groups.get("trace_id"),
        span_id=groups.get("span_id"),
        request_id=groups.get("request_id"),
        correlation_id=groups.get("request_id"),
        http_method=groups.get("method"),
        endpoint=groups.get("endpoint"),
        status_code=_int_or_none(groups.get("status")),
        duration_ms=_int_or_none(groups.get("duration")),
        package_name=package_name,
        class_name=class_name,
        method_name=method_name,
        file_name=file_name,
        line_number=line_number,
        error_type=_short_class(error_match.group("error_type")) if error_match else None,
        error_message=error_match.group("message") if error_match else message,
        stack_trace=stack_trace,
        raw_log_excerpt="\n".join(lines)[:2000],
    )


def ingest_log_file(
    *,
    run_id: str,
    service_name: str,
    path: str | Path,
) -> Iterator[ParsedLogEntry]:
    for line_start, line_end, lines in stream_log_blocks(path):
        entry = parse_log_block(
            run_id=run_id,
            source_file=path,
            line_start=line_start,
            line_end=line_end,
            lines=lines,
            fallback_service=service_name,
        )
        if entry:
            yield entry


def summarize_entries(run_id: str, service_name: str, entries: Iterable[ParsedLogEntry]) -> LogSummary:
    rows = list(entries)
    error_classes = Counter(entry.error_type for entry in rows if entry.error_type)
    endpoint_errors = Counter(entry.endpoint for entry in rows if entry.level == "ERROR" and entry.endpoint)
    trace_ids = sorted({entry.trace_id for entry in rows if entry.level == "ERROR" and entry.trace_id})
    return LogSummary(
        run_id=run_id,
        service_name=service_name,
        lines_scanned=sum(max(1, entry.line_end - entry.line_start + 1) for entry in rows),
        request_count=sum(1 for entry in rows if entry.http_method and entry.endpoint),
        info_count=sum(1 for entry in rows if entry.level == "INFO"),
        warn_count=sum(1 for entry in rows if entry.level == "WARN"),
        error_count=sum(1 for entry in rows if entry.level == "ERROR"),
        top_error_classes=dict(error_classes.most_common(10)),
        top_endpoints_by_error=dict(endpoint_errors.most_common(10)),
        affected_trace_ids=trace_ids[:50],
    )


def cluster_errors_deterministically(
    *,
    run_id: str,
    entries_by_service: dict[str, list[ParsedLogEntry]],
    summaries: dict[str, LogSummary],
    services: dict[str, ServiceConfig] | None = None,
    settings: RepairBotSettings | None = None,
) -> list[ErrorCluster]:
    settings = settings or RepairBotSettings.from_env()
    services = services or {}
    all_entries = [entry for entries in entries_by_service.values() for entry in entries]
    errors = [entry for entry in all_entries if entry.level == "ERROR"]
    by_key: dict[tuple, list[ParsedLogEntry]] = defaultdict(list)
    for entry in errors:
        key = (
            entry.service_name,
            entry.error_type,
            entry.class_name,
            entry.method_name,
            entry.line_number,
            normalize_error_message(entry.error_message or ""),
        )
        by_key[key].append(entry)

    trace_services = _trace_error_services(errors)
    clusters: list[ErrorCluster] = []
    for index, (key, group) in enumerate(sorted(by_key.items(), key=lambda item: item[0]), start=1):
        representative = group[0]
        affected_traces = sorted({entry.trace_id for entry in group if entry.trace_id})
        root_service = infer_root_service(representative, affected_traces, trace_services)
        symptom_services = sorted(
            {svc for trace in affected_traces for svc in trace_services.get(trace, [])}
            or {representative.service_name}
        )
        summary = summaries.get(representative.service_name)
        cluster_id = _cluster_id(key)
        hypotheses, rejected = generate_hypotheses(representative, settings=settings)
        clusters.append(
            ErrorCluster(
                cluster_id=f"cluster-{index:03d}-{cluster_id}",
                service_name=representative.service_name,
                application_name=representative.application_name,
                suspected_root_service=root_service,
                symptom_services=symptom_services,
                downstream_services=_infer_downstream_services(group, services),
                error_type=representative.error_type,
                class_name=representative.class_name,
                method_name=representative.method_name,
                file_name=representative.file_name,
                line_number=representative.line_number,
                endpoint=_most_common(entry.endpoint for entry in group),
                normalized_error_message=normalize_error_message(representative.error_message or ""),
                trace_id_examples=affected_traces[: settings.max_log_examples_per_cluster],
                affected_trace_ids=affected_traces,
                occurrence_count=len(group),
                severity=_severity(len(group), representative.status_code),
                total_lines_scanned=summary.lines_scanned if summary else 0,
                total_requests_detected=summary.request_count if summary else 0,
                normal_log_count=summary.info_count if summary else 0,
                warn_count=summary.warn_count if summary else 0,
                error_count=summary.error_count if summary else len(group),
                raw_log_excerpt=representative.raw_log_excerpt,
                stack_trace_excerpt=(representative.stack_trace or "")[:2000],
                representative_success_logs=_examples(entries_by_service, representative.service_name, "INFO", settings),
                representative_warning_logs=_examples(entries_by_service, representative.service_name, "WARN", settings),
                representative_error_logs=[entry.raw_log_excerpt for entry in group[: settings.max_log_examples_per_cluster]],
                hypotheses=hypotheses[: settings.top_hypotheses],
                rejected_hypotheses=rejected,
                selected_hypothesis=hypotheses[0] if hypotheses else None,
                recommended_solution=_recommended_solution(representative),
            )
        )
    return clusters


def build_agent1_payload(
    clusters: list[ErrorCluster],
    summaries: list[LogSummary],
    settings: RepairBotSettings,
) -> dict:
    return {
        "budget": {
            "max_hypotheses": settings.max_hypotheses,
            "top_hypotheses": settings.top_hypotheses,
            "max_log_examples_per_cluster": settings.max_log_examples_per_cluster,
        },
        "summaries": [summary.model_dump() for summary in summaries],
        "clusters": [
            {
                "cluster_id": cluster.cluster_id,
                "service_name": cluster.service_name,
                "suspected_root_service": cluster.suspected_root_service,
                "error_type": cluster.error_type,
                "class_name": cluster.class_name,
                "method_name": cluster.method_name,
                "line_number": cluster.line_number,
                "endpoint": cluster.endpoint,
                "occurrence_count": cluster.occurrence_count,
                "severity": cluster.severity,
                "stack_trace_excerpt": cluster.stack_trace_excerpt,
                "representative_error_logs": cluster.representative_error_logs,
                "representative_warning_logs": cluster.representative_warning_logs,
            }
            for cluster in clusters
        ],
    }


def normalize_error_message(message: str) -> str:
    text = re.sub(r'"[^"]+"', '"<value>"', message)
    text = re.sub(r"\b\d+\b", "<num>", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def infer_root_service(
    representative: ParsedLogEntry,
    affected_traces: list[str],
    trace_services: dict[str, list[str]],
) -> str:
    candidate_counts: Counter[str] = Counter()
    for trace in affected_traces:
        for service in trace_services.get(trace, []):
            candidate_counts[service] += 1
    if candidate_counts:
        return candidate_counts.most_common(1)[0][0]
    return representative.service_name


def generate_hypotheses(
    entry: ParsedLogEntry,
    *,
    settings: RepairBotSettings,
) -> tuple[list[Hypothesis], list[RejectedHypothesis]]:
    candidates = [
        (
            f"{entry.class_name or 'The failing class'} may not validate null or missing values before use",
            f"{entry.error_type or 'Error'} at {entry.file_name}:{entry.line_number} with message `{entry.error_message}`",
            0.82 if entry.error_type == "NullPointerException" else 0.68,
        ),
        (
            "A downstream service response may be empty, delayed, or malformed and is not handled defensively",
            f"Endpoint `{entry.http_method} {entry.endpoint}` failed for repeated trace/request ids",
            0.65,
        ),
        (
            "Input validation may allow an invalid edge case that later reaches business logic",
            f"Status {entry.status_code} and warning context should be compared across the same trace id",
            0.58,
        ),
    ]
    hypotheses = [
        Hypothesis(rank=index, hypothesis=text, evidence=evidence, confidence=confidence)
        for index, (text, evidence, confidence) in enumerate(candidates[: settings.max_hypotheses], start=1)
    ]
    rejected = [
        RejectedHypothesis(
            hypothesis="Broad rewrite or deployment/configuration change",
            reason="The cluster points to a localized Java stack frame and prototype safety rules prefer a minimal code fix.",
        )
    ]
    return hypotheses, rejected


def _is_stack_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("at ") or stripped.startswith("Caused by:") or stripped.startswith("... ")


def _short_class(package_class: str | None) -> str | None:
    return package_class.rsplit(".", 1)[-1] if package_class else None


def _package_name(package_class: str | None) -> str | None:
    return package_class.rsplit(".", 1)[0] if package_class and "." in package_class else None


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value else None


def _cluster_id(key: tuple) -> str:
    digest = hashlib.sha1("|".join(str(part) for part in key).encode("utf-8")).hexdigest()
    return digest[:10]


def _trace_error_services(errors: list[ParsedLogEntry]) -> dict[str, list[str]]:
    traces: dict[str, list[str]] = defaultdict(list)
    for entry in errors:
        if entry.trace_id and entry.service_name not in traces[entry.trace_id]:
            traces[entry.trace_id].append(entry.service_name)
    return traces


def _infer_downstream_services(
    group: list[ParsedLogEntry],
    services: dict[str, ServiceConfig],
) -> list[str]:
    names = set(services)
    found: set[str] = set()
    for entry in group:
        text = f"{entry.error_message or ''}\n{entry.raw_log_excerpt}".lower()
        for name in names:
            if name != entry.service_name and name.lower() in text:
                found.add(name)
    return sorted(found)


def _severity(count: int, status_code: int | None) -> str:
    if status_code and status_code >= 500 and count >= 10:
        return "HIGH"
    if status_code and status_code >= 500:
        return "MEDIUM"
    return "LOW"


def _most_common(values: Iterable[str | None]) -> str | None:
    counter = Counter(value for value in values if value)
    return counter.most_common(1)[0][0] if counter else None


def _examples(
    entries_by_service: dict[str, list[ParsedLogEntry]],
    service_name: str,
    level: str,
    settings: RepairBotSettings,
) -> list[str]:
    return [
        entry.raw_log_excerpt
        for entry in entries_by_service.get(service_name, [])
        if entry.level == level
    ][: settings.max_log_examples_per_cluster]


def _recommended_solution(entry: ParsedLogEntry) -> str:
    if entry.error_type == "NullPointerException":
        return "Add a null/missing-value guard near the failing Java frame and cover it with a focused test."
    if entry.error_type and "Timeout" in entry.error_type:
        return "Add bounded timeout handling/retry safeguards and return a controlled error response."
    if entry.error_type and "IllegalArgument" in entry.error_type:
        return "Tighten validation at the API/service boundary and add edge-case tests."
    return "Inspect the failing Java frame, apply the smallest defensive fix, and add or update a regression test."
