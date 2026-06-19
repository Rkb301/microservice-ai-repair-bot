"""Async persistence ports with in-memory and MongoDB implementations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from .platform_models import (
    IncidentRecord,
    JiraTicketMetadata,
    LogUploadRecord,
    PlatformRepairPlan,
    PullRequestMetadata,
    RepairAttemptRecord,
    RepairRunRecord,
    RepositoryRecord,
    utc_now,
)


COLLECTION_NAMES = (
    "repositories",
    "log_uploads",
    "incidents",
    "jira_tickets",
    "repair_plans",
    "repair_runs",
    "repair_attempts",
    "pull_requests",
)


class RepairPlatformRepository(Protocol):
    async def create_run_bundle(
        self,
        *,
        repository: RepositoryRecord,
        log_upload: LogUploadRecord,
        incident: IncidentRecord,
        run: RepairRunRecord,
    ) -> None: ...

    async def get_repository(self, repository_id: str) -> RepositoryRecord | None: ...

    async def update_repository(self, repository_id: str, **changes: Any) -> RepositoryRecord: ...

    async def get_log_upload(self, uploaded_log_id: str) -> LogUploadRecord | None: ...

    async def get_run(self, run_id: str) -> RepairRunRecord | None: ...

    async def list_runs(self) -> list[RepairRunRecord]: ...

    async def update_run(self, run_id: str, **changes: Any) -> RepairRunRecord: ...

    async def get_incident(self, incident_id: str) -> IncidentRecord | None: ...

    async def list_incidents(self) -> list[IncidentRecord]: ...

    async def update_incident(self, incident_id: str, **changes: Any) -> IncidentRecord: ...

    async def save_jira_ticket(self, ticket: JiraTicketMetadata) -> None: ...

    async def get_jira_ticket(self, jira_key: str) -> JiraTicketMetadata | None: ...

    async def update_jira_ticket_with_pull_request(
        self, jira_key: str, pull_request_url: str
    ) -> JiraTicketMetadata: ...

    async def save_plan(self, plan: PlatformRepairPlan) -> None: ...

    async def get_plan_for_run(self, run_id: str) -> PlatformRepairPlan | None: ...

    async def save_attempt(self, attempt: RepairAttemptRecord) -> None: ...

    async def list_attempts(self, run_id: str) -> list[RepairAttemptRecord]: ...

    async def save_pull_request(self, pull_request: PullRequestMetadata) -> None: ...


class InMemoryRepairPlatformRepository:
    """Concurrency-safe fake used by the API default and all platform tests."""

    collection_names = COLLECTION_NAMES

    def __init__(self) -> None:
        self.repositories: dict[str, RepositoryRecord] = {}
        self.log_uploads: dict[str, LogUploadRecord] = {}
        self.incidents: dict[str, IncidentRecord] = {}
        self.jira_tickets: dict[str, JiraTicketMetadata] = {}
        self.repair_plans: dict[str, PlatformRepairPlan] = {}
        self.repair_runs: dict[str, RepairRunRecord] = {}
        self.repair_attempts: dict[str, RepairAttemptRecord] = {}
        self.pull_requests: dict[str, PullRequestMetadata] = {}
        self._lock = asyncio.Lock()

    async def create_run_bundle(
        self,
        *,
        repository: RepositoryRecord,
        log_upload: LogUploadRecord,
        incident: IncidentRecord,
        run: RepairRunRecord,
    ) -> None:
        async with self._lock:
            self.repositories[repository.repository_id] = repository.model_copy(deep=True)
            self.log_uploads[log_upload.uploaded_log_id] = log_upload.model_copy(deep=True)
            self.incidents[incident.incident_id] = incident.model_copy(deep=True)
            self.repair_runs[run.run_id] = run.model_copy(deep=True)

    async def get_repository(self, repository_id: str) -> RepositoryRecord | None:
        return _copy(self.repositories.get(repository_id))

    async def update_repository(self, repository_id: str, **changes: Any) -> RepositoryRecord:
        async with self._lock:
            current = self.repositories.get(repository_id)
            if current is None:
                raise KeyError(f"Unknown repository: {repository_id}")
            updated = current.model_copy(update={**changes, "updated_at": utc_now()})
            self.repositories[repository_id] = updated
            return updated.model_copy(deep=True)

    async def get_log_upload(self, uploaded_log_id: str) -> LogUploadRecord | None:
        return _copy(self.log_uploads.get(uploaded_log_id))

    async def get_run(self, run_id: str) -> RepairRunRecord | None:
        return _copy(self.repair_runs.get(run_id))

    async def list_runs(self) -> list[RepairRunRecord]:
        return sorted(
            (_copy(item) for item in self.repair_runs.values()),
            key=lambda item: item.created_at,
            reverse=True,
        )

    async def update_run(self, run_id: str, **changes: Any) -> RepairRunRecord:
        async with self._lock:
            current = self.repair_runs.get(run_id)
            if current is None:
                raise KeyError(f"Unknown repair run: {run_id}")
            updated = current.model_copy(update={**changes, "updated_at": utc_now()})
            self.repair_runs[run_id] = updated
            return updated.model_copy(deep=True)

    async def get_incident(self, incident_id: str) -> IncidentRecord | None:
        return _copy(self.incidents.get(incident_id))

    async def list_incidents(self) -> list[IncidentRecord]:
        return sorted(
            (_copy(item) for item in self.incidents.values()),
            key=lambda item: item.created_at,
            reverse=True,
        )

    async def update_incident(self, incident_id: str, **changes: Any) -> IncidentRecord:
        async with self._lock:
            current = self.incidents.get(incident_id)
            if current is None:
                raise KeyError(f"Unknown incident: {incident_id}")
            updated = current.model_copy(update={**changes, "updated_at": utc_now()})
            self.incidents[incident_id] = updated
            return updated.model_copy(deep=True)

    async def save_jira_ticket(self, ticket: JiraTicketMetadata) -> None:
        async with self._lock:
            self.jira_tickets[ticket.jira_ticket_id] = ticket.model_copy(deep=True)

    async def get_jira_ticket(self, jira_key: str) -> JiraTicketMetadata | None:
        return next(
            (
                _copy(ticket)
                for ticket in self.jira_tickets.values()
                if ticket.jira_key == jira_key
            ),
            None,
        )

    async def update_jira_ticket_with_pull_request(
        self, jira_key: str, pull_request_url: str
    ) -> JiraTicketMetadata:
        async with self._lock:
            for ticket_id, ticket in self.jira_tickets.items():
                if ticket.jira_key != jira_key:
                    continue
                updated = ticket.model_copy(
                    update={
                        "pull_request_url": pull_request_url,
                        "status": "pr_linked",
                        "updated_at": utc_now(),
                    }
                )
                self.jira_tickets[ticket_id] = updated
                return updated.model_copy(deep=True)
        raise KeyError(f"Unknown Jira ticket: {jira_key}")

    async def save_plan(self, plan: PlatformRepairPlan) -> None:
        async with self._lock:
            self.repair_plans[plan.plan_id] = plan.model_copy(deep=True)

    async def get_plan_for_run(self, run_id: str) -> PlatformRepairPlan | None:
        return next(
            (_copy(plan) for plan in self.repair_plans.values() if plan.run_id == run_id),
            None,
        )

    async def save_attempt(self, attempt: RepairAttemptRecord) -> None:
        async with self._lock:
            self.repair_attempts[attempt.repair_attempt_id] = attempt.model_copy(deep=True)

    async def list_attempts(self, run_id: str) -> list[RepairAttemptRecord]:
        items = [
            _copy(attempt)
            for attempt in self.repair_attempts.values()
            if attempt.run_id == run_id
        ]
        return sorted(items, key=lambda item: item.attempt_number)

    async def save_pull_request(self, pull_request: PullRequestMetadata) -> None:
        async with self._lock:
            self.pull_requests[pull_request.pull_request_id] = pull_request.model_copy(deep=True)


class MongoRepairPlatformRepository:
    """Motor-backed implementation; importing the project does not require Motor."""

    collection_names = COLLECTION_NAMES

    def __init__(self, database: Any, *, client: Any | None = None) -> None:
        self.database = database
        self.client = client

    @classmethod
    def from_uri(cls, uri: str, database_name: str) -> "MongoRepairPlatformRepository":
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except ImportError as exc:
            raise RuntimeError(
                "MongoDB mode requires Motor. Install the project dependencies first."
            ) from exc
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=3000)
        return cls(client[database_name], client=client)

    async def create_run_bundle(
        self,
        *,
        repository: RepositoryRecord,
        log_upload: LogUploadRecord,
        incident: IncidentRecord,
        run: RepairRunRecord,
    ) -> None:
        await self._upsert("repositories", "repository_id", repository)
        await self._upsert("log_uploads", "uploaded_log_id", log_upload)
        await self._upsert("incidents", "incident_id", incident)
        await self._upsert("repair_runs", "run_id", run)

    async def get_repository(self, repository_id: str) -> RepositoryRecord | None:
        return await self._get("repositories", "repository_id", repository_id, RepositoryRecord)

    async def update_repository(self, repository_id: str, **changes: Any) -> RepositoryRecord:
        changes["updated_at"] = utc_now()
        result = await self.database["repositories"].find_one_and_update(
            {"repository_id": repository_id},
            {"$set": _mongo_values(changes)},
            projection={"_id": 0},
            return_document=True,
        )
        if result is None:
            raise KeyError(f"Unknown repository: {repository_id}")
        return RepositoryRecord.model_validate(result)

    async def get_log_upload(self, uploaded_log_id: str) -> LogUploadRecord | None:
        return await self._get("log_uploads", "uploaded_log_id", uploaded_log_id, LogUploadRecord)

    async def get_run(self, run_id: str) -> RepairRunRecord | None:
        return await self._get("repair_runs", "run_id", run_id, RepairRunRecord)

    async def list_runs(self) -> list[RepairRunRecord]:
        cursor = self.database["repair_runs"].find({}, {"_id": 0}).sort("created_at", -1)
        return [RepairRunRecord.model_validate(item) async for item in cursor]

    async def update_run(self, run_id: str, **changes: Any) -> RepairRunRecord:
        changes["updated_at"] = utc_now()
        result = await self.database["repair_runs"].find_one_and_update(
            {"run_id": run_id},
            {"$set": _mongo_values(changes)},
            projection={"_id": 0},
            return_document=True,
        )
        if result is None:
            raise KeyError(f"Unknown repair run: {run_id}")
        return RepairRunRecord.model_validate(result)

    async def get_incident(self, incident_id: str) -> IncidentRecord | None:
        return await self._get("incidents", "incident_id", incident_id, IncidentRecord)

    async def list_incidents(self) -> list[IncidentRecord]:
        cursor = self.database["incidents"].find({}, {"_id": 0}).sort("created_at", -1)
        return [IncidentRecord.model_validate(item) async for item in cursor]

    async def update_incident(self, incident_id: str, **changes: Any) -> IncidentRecord:
        changes["updated_at"] = utc_now()
        result = await self.database["incidents"].find_one_and_update(
            {"incident_id": incident_id},
            {"$set": _mongo_values(changes)},
            projection={"_id": 0},
            return_document=True,
        )
        if result is None:
            raise KeyError(f"Unknown incident: {incident_id}")
        return IncidentRecord.model_validate(result)

    async def save_jira_ticket(self, ticket: JiraTicketMetadata) -> None:
        await self._upsert("jira_tickets", "jira_ticket_id", ticket)

    async def get_jira_ticket(self, jira_key: str) -> JiraTicketMetadata | None:
        return await self._get("jira_tickets", "jira_key", jira_key, JiraTicketMetadata)

    async def update_jira_ticket_with_pull_request(
        self, jira_key: str, pull_request_url: str
    ) -> JiraTicketMetadata:
        result = await self.database["jira_tickets"].find_one_and_update(
            {"jira_key": jira_key},
            {
                "$set": {
                    "pull_request_url": pull_request_url,
                    "status": "pr_linked",
                    "updated_at": utc_now(),
                }
            },
            projection={"_id": 0},
            return_document=True,
        )
        if result is None:
            raise KeyError(f"Unknown Jira ticket: {jira_key}")
        return JiraTicketMetadata.model_validate(result)

    async def save_plan(self, plan: PlatformRepairPlan) -> None:
        await self._upsert("repair_plans", "plan_id", plan)

    async def get_plan_for_run(self, run_id: str) -> PlatformRepairPlan | None:
        document = await self.database["repair_plans"].find_one({"run_id": run_id}, {"_id": 0})
        return PlatformRepairPlan.model_validate(document) if document else None

    async def save_attempt(self, attempt: RepairAttemptRecord) -> None:
        await self._upsert("repair_attempts", "repair_attempt_id", attempt)

    async def list_attempts(self, run_id: str) -> list[RepairAttemptRecord]:
        cursor = self.database["repair_attempts"].find(
            {"run_id": run_id}, {"_id": 0}
        ).sort("attempt_number", 1)
        return [RepairAttemptRecord.model_validate(item) async for item in cursor]

    async def save_pull_request(self, pull_request: PullRequestMetadata) -> None:
        await self._upsert("pull_requests", "pull_request_id", pull_request)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    async def _upsert(self, collection: str, key: str, model: Any) -> None:
        document = model.model_dump(mode="json")
        await self.database[collection].replace_one(
            {key: document[key]}, document, upsert=True
        )

    async def _get(self, collection: str, key: str, value: str, model: Any) -> Any:
        document = await self.database[collection].find_one({key: value}, {"_id": 0})
        return model.model_validate(document) if document else None


def _copy(value: Any) -> Any:
    return value.model_copy(deep=True) if value is not None else None


def _mongo_values(values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values.items():
        if hasattr(value, "model_dump"):
            result[key] = value.model_dump(mode="json")
        else:
            result[key] = value
    return result
