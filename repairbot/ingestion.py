"""Uploaded-log ingestion and initial incident/run creation."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from uuid import uuid4

from .langstack import parse_incident_candidate
from .persistence import RepairPlatformRepository
from .platform_models import (
    IncidentRecord,
    LogUploadRecord,
    RepairRunRecord,
    RepositoryRecord,
    UploadedRepairRequest,
)
from .redaction import redact_sensitive_text


MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class UploadedLogIngestionService:
    def __init__(self, repository: RepairPlatformRepository, workspace_root: Path) -> None:
        self.repository = repository
        self.workspace_root = workspace_root.resolve()

    async def create_run(
        self,
        *,
        request: UploadedRepairRequest,
        filename: str,
        content: bytes,
    ) -> RepairRunRecord:
        if not content:
            raise ValueError("Uploaded log file is empty")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError(f"Uploaded log file exceeds {MAX_UPLOAD_BYTES} bytes")

        run_id = f"run_{uuid4().hex}"
        repository_id = f"repo_{uuid4().hex}"
        uploaded_log_id = f"log_{uuid4().hex}"
        incident_id = f"inc_{uuid4().hex}"
        run_root = self.workspace_root / "runs" / run_id
        upload_dir = run_root / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=False)

        safe_name = _safe_filename(filename)
        decoded = content.decode("utf-8", errors="replace")
        redacted = redact_sensitive_text(decoded)
        storage_path = upload_dir / safe_name
        storage_path.write_text(redacted, encoding="utf-8")
        candidate = parse_incident_candidate(
            redacted,
            service_name=request.service_name,
            module_path=request.module_path,
        )
        stored_bytes = redacted.encode("utf-8")
        target_branch = request.repo_branch or "main"

        repository_record = RepositoryRecord(
            repository_id=repository_id,
            repo_url=request.repo_url,
            repo_branch=target_branch,
            service_name=request.service_name,
            module_path=request.module_path,
        )
        upload_record = LogUploadRecord(
            uploaded_log_id=uploaded_log_id,
            run_id=run_id,
            original_filename=safe_name,
            storage_path=str(storage_path.resolve()),
            content_sha256=hashlib.sha256(stored_bytes).hexdigest(),
            size_bytes=len(stored_bytes),
        )
        incident = IncidentRecord(
            incident_id=incident_id,
            run_id=run_id,
            error_name=candidate.error_name,
            description=candidate.description,
            repo_url=request.repo_url,
            github_repo_url=request.repo_url,
            repo_branch=target_branch,
            target_branch=target_branch,
            service_name=request.service_name,
            module_path=request.module_path,
            uploaded_log_id=uploaded_log_id,
            likely_files=candidate.likely_files,
        )
        run = RepairRunRecord(
            run_id=run_id,
            repository_id=repository_id,
            incident_id=incident_id,
            uploaded_log_id=uploaded_log_id,
            require_approval=bool(request.require_approval),
            approval_status="pending" if request.require_approval else "not_required",
            repo_url=request.repo_url,
            github_repo_url=request.repo_url,
            repo_branch=target_branch,
            target_branch=target_branch,
            service_name=request.service_name,
            module_path=request.module_path,
            language=request.language,
            build_system=request.build_system,
            test_cmd=request.test_cmd,
            jira_project=request.jira_project,
            operator_overrides=request.model_dump(
                mode="python", exclude_none=True, exclude={"repo_url"}
            ),
        )
        await self.repository.create_run_bundle(
            repository=repository_record,
            log_upload=upload_record,
            incident=incident,
            run=run,
        )
        return run


def _safe_filename(filename: str) -> str:
    leaf = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", leaf).strip("-.")
    return stem[:120] or "uploaded-error.log"
