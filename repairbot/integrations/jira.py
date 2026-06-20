"""Jira ticket abstraction with a network-free fake."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from ..redaction import redact_sensitive_text


@dataclass(frozen=True)
class JiraTicketResult:
    jira_key: str
    jira_ticket_url: str


class JiraIntegrationError(RuntimeError):
    """Safe Jira adapter failure that never includes credentials or response bodies."""


@dataclass
class FakeJiraTicket:
    jira_key: str
    jira_ticket_url: str
    project_key: str
    issue_type: str
    incident_id: str
    title: str
    description: str
    solution: str
    service_name: str | None = None
    repo_url: str | None = None
    run_id: str | None = None
    pull_request_url: str | None = None


class JiraClient(Protocol):
    async def create_or_record_ticket(
        self,
        *,
        project_key: str,
        issue_type: str = "Bug",
        title: str | None = None,
        summary: str | None = None,
        description: str,
        solution: str | None = None,
        proposed_solution: str | None = None,
        incident_id: str,
        service_name: str | None = None,
        repo_url: str | None = None,
        run_id: str | None = None,
    ) -> JiraTicketResult: ...

    async def update_ticket_with_pull_request(
        self, jira_key: str, pull_request_url: str
    ) -> None: ...


class FakeJiraClient:
    """Deterministic Jira metadata generator used in local and test flows."""

    def __init__(self) -> None:
        self.tickets: dict[str, FakeJiraTicket] = {}

    async def create_or_record_ticket(
        self,
        *,
        project_key: str,
        issue_type: str = "Bug",
        title: str | None = None,
        summary: str | None = None,
        description: str,
        solution: str | None = None,
        proposed_solution: str | None = None,
        incident_id: str,
        service_name: str | None = None,
        repo_url: str | None = None,
        run_id: str | None = None,
    ) -> JiraTicketResult:
        resolved_title = title or summary
        if not resolved_title:
            raise ValueError("A Jira title or summary is required")
        resolved_solution = solution or proposed_solution or "Proposed solution pending repair planning."
        safe_project = "".join(ch for ch in project_key.upper() if ch.isalnum()) or "DEMO"
        existing = next(
            (
                ticket
                for ticket in self.tickets.values()
                if ticket.incident_id == incident_id
            ),
            None,
        )
        if existing is not None:
            return JiraTicketResult(
                jira_key=existing.jira_key,
                jira_ticket_url=existing.jira_ticket_url,
            )
        number = 1001 + len(self.tickets)
        key = f"{safe_project}-{number}"
        result = JiraTicketResult(
            jira_key=key,
            jira_ticket_url=f"https://jira.local/browse/{key}",
        )
        self.tickets[key] = FakeJiraTicket(
            jira_key=key,
            jira_ticket_url=result.jira_ticket_url,
            project_key=safe_project,
            issue_type=issue_type,
            incident_id=incident_id,
            title=redact_sensitive_text(resolved_title),
            description=redact_sensitive_text(description),
            solution=redact_sensitive_text(resolved_solution),
            service_name=service_name,
            repo_url=repo_url,
            run_id=run_id,
        )
        return result

    async def update_ticket_with_pull_request(
        self, jira_key: str, pull_request_url: str
    ) -> None:
        ticket = self.tickets.get(jira_key)
        if ticket is None:
            raise KeyError(f"Unknown fake Jira ticket: {jira_key}")
        ticket.pull_request_url = _validate_http_url(
            pull_request_url, field_name="pull_request_url"
        )


class RealJiraClient:
    """Credential-gated Jira Cloud v3 client with non-secret failures."""

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        if not email.strip() or not api_token:
            raise ValueError("Jira email and API token are required")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self.base_url,
            auth=httpx.BasicAuth(email.strip(), api_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=httpx.Timeout(15.0),
        )
        self._linked_pull_requests: set[tuple[str, str]] = set()

    async def create_or_record_ticket(
        self,
        *,
        project_key: str,
        issue_type: str = "Bug",
        title: str | None = None,
        summary: str | None = None,
        description: str,
        solution: str | None = None,
        proposed_solution: str | None = None,
        incident_id: str,
        service_name: str | None = None,
        repo_url: str | None = None,
        run_id: str | None = None,
    ) -> JiraTicketResult:
        resolved_title = title or summary
        if not resolved_title:
            raise ValueError("A Jira title or summary is required")
        payload = build_jira_issue_payload(
            project_key=project_key,
            issue_type=issue_type,
            title=resolved_title,
            description=description,
            solution=solution or proposed_solution or "Proposed solution pending repair planning.",
            incident_id=incident_id,
            service_name=service_name,
            repo_url=repo_url,
            run_id=run_id,
        )
        response = await self._request(
            "POST",
            "/rest/api/3/issue",
            json=payload,
            operation="issue creation",
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise JiraIntegrationError(
                "Jira issue creation returned an invalid JSON response"
            ) from exc
        jira_key = str(body.get("key", ""))
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", jira_key):
            raise JiraIntegrationError("Jira issue creation response did not include a valid key")
        return JiraTicketResult(
            jira_key=jira_key,
            jira_ticket_url=f"{self.base_url}/browse/{quote(jira_key)}",
        )

    async def update_ticket_with_pull_request(
        self, jira_key: str, pull_request_url: str
    ) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", jira_key):
            raise ValueError("Jira key is invalid")
        safe_url = _validate_http_url(
            pull_request_url, field_name="pull_request_url"
        )
        link = (jira_key, safe_url)
        if link in self._linked_pull_requests:
            return
        await self._request(
            "POST",
            f"/rest/api/3/issue/{quote(jira_key)}/comment",
            json=build_jira_backlink_comment(safe_url),
            operation="pull-request backlink update",
        )
        self._linked_pull_requests.add(link)

    async def health_check(self) -> bool:
        await self._request(
            "GET",
            "/rest/api/3/myself",
            operation="health check",
        )
        return True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise JiraIntegrationError(
                f"Jira {operation} failed before a response was received"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise JiraIntegrationError(
                f"Jira {operation} failed with HTTP {response.status_code}"
            )
        return response


def build_jira_issue_payload(
    *,
    project_key: str,
    issue_type: str,
    title: str,
    description: str,
    solution: str,
    incident_id: str,
    service_name: str | None = None,
    repo_url: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    safe_project = project_key.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", safe_project):
        raise ValueError("Jira project key is invalid")
    safe_issue_type = issue_type.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,39}", safe_issue_type):
        raise ValueError("Jira issue type is invalid")
    safe_title = redact_sensitive_text(title).strip()[:255]
    if not safe_title:
        raise ValueError("Jira title must not be empty")
    lines = [
        f"Error description: {redact_sensitive_text(description)}",
        f"Proposed solution: {redact_sensitive_text(solution)}",
        f"Incident ID: {redact_sensitive_text(incident_id)}",
    ]
    if service_name:
        lines.append(f"Affected service: {redact_sensitive_text(service_name)}")
    if repo_url:
        lines.append(
            f"Repository: {_validate_http_url(repo_url, field_name='repo_url')}"
        )
    if run_id:
        lines.append(f"Run ID: {redact_sensitive_text(run_id)}")
    return {
        "fields": {
            "project": {"key": safe_project},
            "issuetype": {"name": safe_issue_type},
            "summary": safe_title,
            "description": _adf_document(lines),
        }
    }


def build_jira_backlink_comment(pull_request_url: str) -> dict[str, Any]:
    safe_url = _validate_http_url(
        pull_request_url, field_name="pull_request_url"
    )
    return {"body": _adf_document([f"Pull request: {safe_url}"])}


def _adf_document(paragraphs: list[str]) -> dict[str, Any]:
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": redact_sensitive_text(text)}],
            }
            for text in paragraphs
        ],
    }


def _validate_base_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Jira base URL must be a credential-free HTTPS URL")
    return candidate


def _validate_http_url(value: str, *, field_name: str) -> str:
    candidate = redact_sensitive_text(value).strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"{field_name} must be a credential-free HTTP(S) URL")
    return candidate


def build_jira_client(
    mode: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> JiraClient:
    if mode == "fake":
        return FakeJiraClient()
    if mode != "real":
        raise ValueError("Jira mode must be 'fake' or 'real'")
    values = environ if environ is not None else os.environ
    required = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
    missing = [name for name in required if not values.get(name)]
    if missing:
        raise RuntimeError(
            "Real Jira mode requires environment credentials: " + ", ".join(missing)
        )
    return RealJiraClient(
        base_url=values["JIRA_BASE_URL"],
        email=values["JIRA_EMAIL"],
        api_token=values["JIRA_API_TOKEN"],
    )
