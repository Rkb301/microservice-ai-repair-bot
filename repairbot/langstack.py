"""LangChain structured planning and tool wrappers for the platform workflow."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .integrations.execution import PatchAgent, TestRunner
from .integrations.github import GitHubClient
from .integrations.jira import JiraClient
from .platform_models import IncidentCandidate, PlatformRepairPlan
from .redaction import redact_sensitive_text
from .tracing import LangSmithTracingConfig


class StructuredPlanOutput(BaseModel):
    observations: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    selected_hypothesis: str
    selection_reason: str
    planned_edits: list[str] = Field(default_factory=list)
    planned_tests: list[str] = Field(default_factory=list)


class StructuredPlanner(Protocol):
    async def create_plan(
        self,
        *,
        run_id: str,
        incident_id: str,
        incident: IncidentCandidate,
        repo_path: Path,
        test_command: str,
        tracing: LangSmithTracingConfig,
    ) -> PlatformRepairPlan: ...


class HeuristicStructuredPlanner:
    """Network-free reason-before-execute planner used by default."""

    def __init__(self) -> None:
        self.provider = "openai"
        self.model_name = "openai:gpt-5-mini"
        self.max_hypotheses = 5

    def configure(
        self, *, provider: str, model_name: str, max_hypotheses: int
    ) -> None:
        self.provider = provider
        self.model_name = model_name
        self.max_hypotheses = max_hypotheses

    async def create_plan(
        self,
        *,
        run_id: str,
        incident_id: str,
        incident: IncidentCandidate,
        repo_path: Path,
        test_command: str,
        tracing: LangSmithTracingConfig,
    ) -> PlatformRepairPlan:
        del tracing
        likely_files = incident.likely_files[:5]
        primary = (
            f"{incident.error_name} originates in {likely_files[0]}"
            if likely_files
            else f"{incident.error_name} is an unhandled service edge case"
        )
        hypotheses = [
            primary,
            "The failing input crosses a service boundary without sufficient validation",
            "Implementation behavior and regression expectations disagree on the error path",
        ][: self.max_hypotheses]
        plan_id = hashlib.sha256(
            f"{run_id}|{incident_id}|{primary}".encode("utf-8")
        ).hexdigest()[:16]
        return PlatformRepairPlan(
            plan_id=plan_id,
            run_id=run_id,
            incident_id=incident_id,
            observations=[
                redact_sensitive_text(f"Observed {incident.error_name}: {incident.description}"),
                f"Repository prepared at an isolated run workspace: {repo_path.name}",
                f"Likely files: {', '.join(likely_files) if likely_files else 'not yet mapped'}",
            ],
            assumptions=[
                "The uploaded log is representative of the failing revision.",
                "The smallest service-local fix is preferred over broad refactoring.",
            ],
            hypotheses=hypotheses,
            selected_hypothesis=primary,
            selection_reason="It best matches the parsed exception and nearest stack-frame evidence.",
            planned_edits=[
                f"Inspect and minimally patch {path}" for path in likely_files[:2]
            ]
            or ["Inspect repository symbols matching the exception before selecting a file."],
            planned_tests=[test_command],
        )


class LangChainStructuredPlanner:
    """Optional real-model adapter; callers must explicitly supply a LangChain model."""

    def __init__(self, model: Any) -> None:
        from langchain_core.prompts import ChatPromptTemplate

        self._prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Create a small, evidence-led repair plan. Never include credentials or raw secrets.",
                ),
                (
                    "human",
                    "Error: {error_name}\nDescription: {description}\nLikely files: {likely_files}\n"
                    "Repository: {repo_path}\nTest command: {test_command}",
                ),
            ]
        )
        self._model = model.with_structured_output(StructuredPlanOutput)
        self.provider = "openai"
        self.model_name = "configured-langchain-model"
        self.max_hypotheses = 5

    def configure(
        self, *, provider: str, model_name: str, max_hypotheses: int
    ) -> None:
        self.provider = provider
        self.model_name = model_name
        self.max_hypotheses = max_hypotheses

    async def create_plan(
        self,
        *,
        run_id: str,
        incident_id: str,
        incident: IncidentCandidate,
        repo_path: Path,
        test_command: str,
        tracing: LangSmithTracingConfig,
    ) -> PlatformRepairPlan:
        chain = self._prompt | self._model
        output = await chain.ainvoke(
            {
                "error_name": incident.error_name,
                "description": redact_sensitive_text(incident.description),
                "likely_files": incident.likely_files,
                "repo_path": str(repo_path),
                "test_command": test_command,
            },
            config=tracing.invocation_config(run_id=run_id, step="plan_repair"),
        )
        plan_id = hashlib.sha256(
            f"{run_id}|{incident_id}|{output.selected_hypothesis}".encode("utf-8")
        ).hexdigest()[:16]
        output_data = output.model_dump()
        output_data["hypotheses"] = output_data["hypotheses"][: self.max_hypotheses]
        return PlatformRepairPlan(
            plan_id=plan_id,
            run_id=run_id,
            incident_id=incident_id,
            **output_data,
        )


def parse_incident_candidate(
    text: str,
    *,
    service_name: str | None = None,
    module_path: str | None = None,
) -> IncidentCandidate:
    redacted = redact_sensitive_text(text)
    error_match = re.search(
        r"(?:Caused by:\s*)?([A-Za-z_$][\w.$]*(?:Exception|Error))(?::\s*([^\r\n]+))?",
        redacted,
    )
    error_name = error_match.group(1).split(".")[-1] if error_match else "UnclassifiedError"
    description = (
        error_match.group(2).strip()
        if error_match and error_match.group(2)
        else next((line.strip() for line in redacted.splitlines() if line.strip()), "Uploaded error log")
    )
    files: list[str] = []
    for file_name in re.findall(r"\(([A-Za-z0-9_$.-]+\.(?:java|py|js|ts)):\d+\)", redacted):
        if file_name not in files:
            files.append(file_name)
    return IncidentCandidate(
        error_name=error_name[:160],
        description=description[:1000],
        likely_files=files[:20],
        service_name=service_name,
        module_path=module_path,
    )


def build_langchain_tools(
    *,
    github: GitHubClient,
    jira: JiraClient,
    patch_agent: PatchAgent,
    test_runner: TestRunner,
    repo_path: Path,
    repo_url: str,
    incident_id: str,
) -> list[Any]:
    """Expose platform capabilities as LangChain tools for future agent wiring."""

    from langchain_core.tools import tool

    @tool
    def inspect_repository(glob_pattern: str = "**/*") -> list[str]:
        """List candidate files in the isolated cloned repository."""
        return [
            path.relative_to(repo_path).as_posix()
            for path in sorted(repo_path.glob(glob_pattern))
            if path.is_file()
        ][:200]

    @tool
    def analyze_uploaded_log(log_text: str) -> dict[str, Any]:
        """Parse redacted uploaded-log text into a structured incident candidate."""
        return parse_incident_candidate(log_text).model_dump()

    @tool
    async def run_repository_tests(command: str, module_path: str = "") -> dict[str, Any]:
        """Run the configured test command through the injected test-runner port."""
        result = await test_runner.run_tests(
            repo_path=repo_path,
            module_path=module_path or None,
            command=command,
        )
        return {"exit_code": result.exit_code, "output": redact_sensitive_text(result.output)}

    @tool
    async def record_jira_ticket(
        project_key: str,
        title: str,
        description: str,
        solution: str,
        issue_type: str = "Bug",
    ) -> str:
        """Create Jira metadata with title, description, and proposed solution."""
        result = await jira.create_or_record_ticket(
            project_key=project_key,
            issue_type=issue_type,
            title=title,
            description=redact_sensitive_text(description),
            solution=redact_sensitive_text(solution),
            incident_id=incident_id,
        )
        return result.jira_ticket_url

    @tool
    async def apply_repair_patch(
        branch_name: str, selected_hypothesis: str, module_path: str = ""
    ) -> dict[str, Any]:
        """Apply a patch through the injected patch-agent port."""
        result = await patch_agent.apply_patch(
            repo_path=repo_path,
            module_path=module_path or None,
            branch_name=branch_name,
            selected_hypothesis=selected_hypothesis,
        )
        return {
            "branch_name": result.branch_name,
            "summary": redact_sensitive_text(result.summary),
            "changed_files": result.changed_files,
        }

    @tool
    async def open_github_pull_request(
        branch_name: str,
        base_branch: str,
        title: str,
        body: str,
        changed_files: list[str],
    ) -> str:
        """Commit and open a PR through the injected GitHub client."""
        result = await github.commit_and_open_pull_request(
            repo_url=repo_url,
            repo_path=repo_path,
            base_branch=base_branch,
            branch_name=branch_name,
            title=title,
            body=redact_sensitive_text(body),
            changed_files=changed_files,
        )
        return result.pull_request_url

    return [
        inspect_repository,
        analyze_uploaded_log,
        run_repository_tests,
        record_jira_ticket,
        apply_repair_patch,
        open_github_pull_request,
    ]
