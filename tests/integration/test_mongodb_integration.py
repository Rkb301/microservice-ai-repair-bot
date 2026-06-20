from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from repairbot.persistence import MongoRepairPlatformRepository
from repairbot.platform_models import (
    IncidentRecord,
    JiraTicketMetadata,
    LogUploadRecord,
    PlatformRepairPlan,
    PullRequestMetadata,
    RepairAttemptRecord,
    RepairRunRecord,
    RepositoryRecord,
)
from repairbot.run_detail import build_run_detail


ENABLED = os.getenv("RUN_MONGODB_INTEGRATION_TESTS") == "1" and all(
    os.getenv(name) for name in ("MONGODB_URI", "MONGODB_DB")
)

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="MongoDB integration requires an explicit flag and environment settings",
)


def test_real_mongodb_round_trip_and_cleanup() -> None:
    async def scenario() -> None:
        suffix = uuid4().hex[:10]
        base_name = os.environ["MONGODB_DB"][:40]
        database_name = f"{base_name}_it_{suffix}"
        repository = MongoRepairPlatformRepository.from_uri(
            os.environ["MONGODB_URI"],
            database_name,
        )
        run_id = f"run_{suffix}"
        incident_id = f"inc_{suffix}"
        try:
            assert await repository.ping() is True
            assert len(await repository.ensure_indexes()) == 19
            repository_record = RepositoryRecord(
                repository_id=f"repo_{suffix}",
                repo_url="https://github.com/Rkb301/user-service",
            )
            upload = LogUploadRecord(
                uploaded_log_id=f"log_{suffix}",
                run_id=run_id,
                original_filename="integration.log",
                storage_path="/tmp/integration.log",
                content_sha256="0" * 64,
                size_bytes=1,
            )
            incident = IncidentRecord(
                incident_id=incident_id,
                run_id=run_id,
                error_name="IntegrationError",
                description="MongoDB integration verification",
                proposed_solution="Verify the persistence boundary",
                repo_url=repository_record.repo_url,
                uploaded_log_id=upload.uploaded_log_id,
            )
            run = RepairRunRecord(
                run_id=run_id,
                repository_id=repository_record.repository_id,
                incident_id=incident_id,
                uploaded_log_id=upload.uploaded_log_id,
                repo_url=repository_record.repo_url,
                status="completed",
                proposed_solution=incident.proposed_solution,
            )
            await repository.create_run_bundle(
                repository=repository_record,
                log_upload=upload,
                incident=incident,
                run=run,
            )
            ticket = JiraTicketMetadata(
                jira_ticket_id=f"jira_{suffix}",
                incident_id=incident_id,
                run_id=run_id,
                project_key="DEMO",
                jira_key=f"DEMO-{int(suffix[:4], 16)}",
                jira_ticket_url="https://jira.example.com/browse/DEMO-1",
                title="MongoDB integration test",
                description="Persistence round trip",
                solution="Verify storage",
            )
            await repository.save_jira_ticket(ticket)
            await repository.save_plan(
                PlatformRepairPlan(
                    plan_id=f"plan_{suffix}",
                    run_id=run_id,
                    incident_id=incident_id,
                    selected_hypothesis="Storage works",
                    selection_reason="Integration evidence",
                )
            )
            await repository.save_attempt(
                RepairAttemptRecord(
                    repair_attempt_id=f"attempt_{suffix}",
                    run_id=run_id,
                    attempt_number=1,
                    hypothesis="Storage works",
                    patch_summary="No source change",
                    test_command="python -m pytest",
                    test_exit_code=0,
                    test_output="passed",
                    tests_passed=True,
                )
            )
            pull_request = PullRequestMetadata(
                pull_request_id=f"pr_{suffix}",
                incident_id=incident_id,
                run_id=run_id,
                repo_url=repository_record.repo_url,
                branch_name=f"repair/integration-{suffix}",
                base_branch="main",
                title="MongoDB integration test",
                commit_sha="1" * 40,
                pull_request_url="https://github.example/pull/1",
            )
            await repository.save_pull_request(pull_request)
            await repository.update_jira_ticket_with_pull_request(
                ticket.jira_key,
                pull_request.pull_request_url,
            )
            stored_run = await repository.get_run(run_id)
            assert stored_run is not None
            detail = await build_run_detail(repository, stored_run)
            assert detail.jira is not None
            assert detail.jira.run_id == run_id
            assert detail.jira.pull_request_url == pull_request.pull_request_url
            assert detail.pull_request == pull_request
            assert any(item.run_id == run_id for item in await repository.list_runs())
        finally:
            if repository.client is not None:
                await repository.client.drop_database(database_name)
            repository.close()

    asyncio.run(scenario())
