"""Jira Cloud REST helpers."""

from __future__ import annotations

import base64
import os

import requests

from .config import RepairBotSettings
from .models import ErrorCluster, JiraTicketRecord
from .storage import RepairBotStorage


class JiraError(RuntimeError):
    """Raised when Jira configuration or API calls fail."""


def build_jira_issue_payload(cluster: ErrorCluster, project_key: str) -> dict:
    summary = jira_summary(cluster)
    body = jira_description_text(cluster)
    return {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": "Bug"},
            "description": _adf_doc(body),
            "labels": ["ai-repairbot", cluster.service_name],
        }
    }


def jira_summary(cluster: ErrorCluster) -> str:
    location = ".".join(
        part for part in [cluster.class_name, cluster.method_name] if part
    )
    if cluster.line_number:
        location = f"{location}:{cluster.line_number}" if location else str(cluster.line_number)
    return f"[{cluster.service_name}] {cluster.error_type or 'Error'} in {location or cluster.file_name or 'unknown location'}"


def jira_description_text(cluster: ErrorCluster) -> str:
    hypotheses = "\n".join(
        f"{item.rank}. {item.hypothesis} (confidence={item.confidence:.2f})\n   Evidence: {item.evidence}"
        for item in cluster.hypotheses
    ) or "No hypotheses generated."
    rejected = "\n".join(
        f"- {item.hypothesis}: {item.reason}" for item in cluster.rejected_hypotheses
    ) or "- None"
    return f"""AI-generated repair bot notice: this ticket was generated from synthetic/local log analysis and requires human review.

Service/application: {cluster.service_name} / {cluster.application_name or 'unknown'}
Suspected root service: {cluster.suspected_root_service}
Symptom services: {', '.join(cluster.symptom_services) or cluster.service_name}
Error: {cluster.error_type or 'unknown'}
Location: {cluster.class_name}.{cluster.method_name}:{cluster.line_number}
Endpoint: {cluster.endpoint or 'unknown'}
Occurrences: {cluster.occurrence_count}
Severity: {cluster.severity}

Log excerpt:
{cluster.raw_log_excerpt or '(none)'}

Stack trace excerpt:
{cluster.stack_trace_excerpt or '(none)'}

Ranked hypotheses:
{hypotheses}

Rejected weak hypotheses:
{rejected}

Recommended solution:
{cluster.recommended_solution or 'Inspect the failing Java frame and apply the smallest safe fix.'}

Acceptance criteria:
- Bug is reproduced or explained.
- Minimal fix is applied.
- Relevant Maven/Gradle tests pass.
- Pull request is opened for human review.
- No secrets, deployment files, or GitHub workflow files are changed.
"""


def create_jira_issue(
    *,
    cluster: ErrorCluster,
    settings: RepairBotSettings,
    storage: RepairBotStorage | None = None,
) -> JiraTicketRecord:
    if storage:
        existing = storage.get_ticket_by_cluster(cluster.cluster_id)
        if existing:
            return existing
    base_url = settings.jira_base_url or os.getenv("JIRA_BASE_URL")
    email = settings.jira_email or os.getenv("JIRA_EMAIL")
    token = settings.jira_api_token or os.getenv("JIRA_API_TOKEN")
    project_key = settings.jira_project_key or os.getenv("JIRA_PROJECT_KEY")
    if not all([base_url, email, token, project_key]):
        raise JiraError("JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, and JIRA_PROJECT_KEY are required")

    payload = build_jira_issue_payload(cluster, project_key)
    auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    response = requests.post(
        f"{base_url.rstrip('/')}/rest/api/3/issue",
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if response.status_code not in {200, 201}:
        raise JiraError(f"Jira issue creation failed ({response.status_code}): {response.text}")
    data = response.json()
    key = data["key"]
    record = JiraTicketRecord(
        jira_key=key,
        jira_url=f"{base_url.rstrip('/')}/browse/{key}",
        cluster_id=cluster.cluster_id,
        service_name=cluster.service_name,
        summary=payload["fields"]["summary"],
        payload=payload,
    )
    if storage:
        storage.store_jira_ticket(record)
    return record


def create_dry_run_ticket(
    *,
    cluster: ErrorCluster,
    project_key: str = "BUG",
    storage: RepairBotStorage | None = None,
) -> JiraTicketRecord:
    if storage:
        existing = storage.get_ticket_by_cluster(cluster.cluster_id)
        if existing:
            return existing
    suffix = cluster.cluster_id.rsplit("-", 1)[-1][:6].upper()
    key = f"{project_key}-{int(suffix, 16) % 9000 + 1000 if _is_hex(suffix) else 1000}"
    payload = build_jira_issue_payload(cluster, project_key)
    record = JiraTicketRecord(
        jira_key=key,
        jira_url=None,
        cluster_id=cluster.cluster_id,
        service_name=cluster.service_name,
        summary=payload["fields"]["summary"],
        status="dry-run",
        payload=payload,
    )
    if storage:
        storage.store_jira_ticket(record)
    return record


def _adf_doc(text: str) -> dict:
    paragraphs = []
    for block in text.split("\n\n"):
        lines = block.splitlines() or [""]
        content = []
        for index, line in enumerate(lines):
            if index:
                content.append({"type": "hardBreak"})
            content.append({"type": "text", "text": line or " "})
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
