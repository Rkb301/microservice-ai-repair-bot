"""Jira ticket abstraction with a network-free fake."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ..redaction import redact_sensitive_text


@dataclass(frozen=True)
class JiraTicketResult:
    jira_key: str
    jira_ticket_url: str


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
    ) -> JiraTicketResult:
        resolved_title = title or summary
        if not resolved_title:
            raise ValueError("A Jira title or summary is required")
        resolved_solution = solution or proposed_solution or "Proposed solution pending repair planning."
        safe_project = "".join(ch for ch in project_key.upper() if ch.isalnum()) or "DEMO"
        number = int(hashlib.sha256(incident_id.encode("utf-8")).hexdigest()[:6], 16) % 90000 + 10000
        key = f"{safe_project}-{number}"
        result = JiraTicketResult(
            jira_key=key,
            jira_ticket_url=f"https://example.invalid/jira/{key}",
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
        )
        return result

    async def update_ticket_with_pull_request(
        self, jira_key: str, pull_request_url: str
    ) -> None:
        ticket = self.tickets.get(jira_key)
        if ticket is None:
            raise KeyError(f"Unknown fake Jira ticket: {jira_key}")
        ticket.pull_request_url = pull_request_url


class RealJiraClient:
    def __init__(self, *, base_url: str, email: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self._api_token = api_token

    async def create_or_record_ticket(self, **_: str) -> JiraTicketResult:
        raise NotImplementedError(
            "Configure a reviewed Jira REST adapter before enabling real ticket creation"
        )

    async def update_ticket_with_pull_request(
        self, jira_key: str, pull_request_url: str
    ) -> None:
        del jira_key, pull_request_url
        raise NotImplementedError(
            "Configure a reviewed Jira REST adapter before enabling real ticket updates"
        )


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
