"""LangChain structured planning and tool wrappers for the platform workflow."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

from .integrations.execution import PatchAgent, TestRunner
from .integrations.github import GitHubClient
from .integrations.jira import JiraClient
from .platform_config import PromptsConfig, render_agent_prompt
from .platform_models import IncidentCandidate, PlatformRepairPlan
from .redaction import redact_sensitive_text
from .tracing import LangSmithTracingConfig


SUPPORTED_SCENARIOS = (
    "user-service missing profile null guard",
    "order-service null customer validation",
    "payment-service invalid charge validation",
)
AGENT_NAMES = ("LogAnalysisAgent", "RepairPlanningAgent", "ReviewPrAgent")


class LangStackAgentError(RuntimeError):
    """Safe failure from an explicitly enabled model-backed agent call."""


class LogAnalysisOutput(BaseModel):
    error_name: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=4_000)
    service_name: str | None = Field(default=None, max_length=120)
    likely_files: list[str] = Field(default_factory=list, max_length=20)
    observations: list[str] = Field(default_factory=list, max_length=20)
    direct_evidence: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("error_name", "description", "service_name")
    @classmethod
    def redacted_text(cls, value: str | None) -> str | None:
        return redact_sensitive_text(value) if value is not None else None

    @field_validator("likely_files", "observations", "direct_evidence")
    @classmethod
    def bounded_redacted_lists(cls, values: list[str]) -> list[str]:
        return [redact_sensitive_text(str(value))[:2_000] for value in values]


class StructuredPlanOutput(BaseModel):
    observations: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    hypotheses: list[str] = Field(default_factory=list, max_length=20)
    selected_hypothesis: str = Field(min_length=1, max_length=4_000)
    selection_reason: str = Field(min_length=1, max_length=4_000)
    planned_edits: list[str] = Field(default_factory=list, max_length=20)
    planned_tests: list[str] = Field(default_factory=list, max_length=20)
    validation_plan: list[str] = Field(default_factory=list, max_length=20)
    risk_notes: list[str] = Field(default_factory=list, max_length=20)
    jira_solution: str = Field(min_length=1, max_length=20_000)

    @field_validator("selected_hypothesis", "selection_reason", "jira_solution")
    @classmethod
    def redacted_text(cls, value: str) -> str:
        return redact_sensitive_text(value)

    @field_validator(
        "observations",
        "assumptions",
        "hypotheses",
        "planned_edits",
        "planned_tests",
        "validation_plan",
        "risk_notes",
    )
    @classmethod
    def bounded_redacted_lists(cls, values: list[str]) -> list[str]:
        return [redact_sensitive_text(str(value))[:4_000] for value in values]


class ReviewPrOutput(BaseModel):
    pr_title: str = Field(min_length=1, max_length=256)
    pr_summary: str = Field(min_length=1, max_length=20_000)
    review_checklist: list[str] = Field(default_factory=list, max_length=20)
    risk_notes: list[str] = Field(default_factory=list, max_length=20)
    jira_comment: str = Field(min_length=1, max_length=4_000)

    @field_validator("pr_title", "pr_summary", "jira_comment")
    @classmethod
    def redacted_text(cls, value: str) -> str:
        return redact_sensitive_text(value)

    @field_validator("review_checklist", "risk_notes")
    @classmethod
    def bounded_redacted_lists(cls, values: list[str]) -> list[str]:
        return [redact_sensitive_text(str(value))[:2_000] for value in values]


class _OpenAIStructuredAgent:
    name: str
    schema: type[BaseModel]

    def __init__(self, model: Any) -> None:
        self.model = model
        self.invocation_count = 0
        self.last_rendered_input = ""

    async def invoke(
        self,
        *,
        prompt: Any,
        values: dict[str, Any],
        run_id: str,
        tracing: LangSmithTracingConfig,
    ) -> tuple[BaseModel, str]:
        from langchain_core.messages import HumanMessage, SystemMessage

        rendered_system = render_agent_prompt(prompt.system, values)
        rendered_human = render_agent_prompt(prompt.human, values)
        self.last_rendered_input = rendered_human
        structured = self.model.with_structured_output(self.schema)
        self.invocation_count += 1
        try:
            result = await structured.ainvoke(
                [
                    SystemMessage(content=rendered_system),
                    HumanMessage(content=rendered_human),
                ],
                config=tracing.invocation_config(run_id=run_id, step=self.name),
            )
            validated = self.schema.model_validate(result)
        except Exception:
            raise LangStackAgentError(
                f"{self.name} OpenAI request did not complete"
            ) from None
        return validated, rendered_human


class LogAnalysisAgent(_OpenAIStructuredAgent):
    name = "LogAnalysisAgent"
    schema = LogAnalysisOutput


class RepairPlanningAgent(_OpenAIStructuredAgent):
    name = "RepairPlanningAgent"
    schema = StructuredPlanOutput


class ReviewPrAgent(_OpenAIStructuredAgent):
    name = "ReviewPrAgent"
    schema = ReviewPrOutput


class StructuredPlanner(Protocol):
    planner_mode: str
    openai_invocations: int
    langchain_used: bool
    last_rendered_inputs: dict[str, str]
    last_outputs: dict[str, dict[str, Any]]

    async def analyze_log(
        self,
        *,
        run_id: str,
        log_text: str,
        service_name: str | None,
        module_path: str | None,
        tracing: LangSmithTracingConfig,
    ) -> LogAnalysisOutput: ...

    async def create_plan(
        self,
        *,
        run_id: str,
        incident_id: str,
        incident: IncidentCandidate,
        repo_path: Path,
        test_command: str,
        analysis: LogAnalysisOutput,
        repo_url: str,
        target_branch: str,
        build_system: str,
        tracing: LangSmithTracingConfig,
    ) -> PlatformRepairPlan: ...

    async def review_pr(
        self,
        *,
        run_id: str,
        service_name: str,
        jira_key: str,
        branch_name: str,
        patch_summary: str,
        changed_files: list[str],
        test_command: str,
        test_result: str,
        risk_notes: list[str],
        selected_hypothesis: str,
        planning_output: dict[str, Any],
        tracing: LangSmithTracingConfig,
    ) -> ReviewPrOutput: ...


class HeuristicStructuredPlanner:
    """Network-free reason-before-execute planner used by default."""

    def __init__(self) -> None:
        self.provider = "openai"
        self.model_name = "openai:gpt-5-mini"
        self.max_hypotheses = 5
        self.prompts = PromptsConfig()
        self.planner_mode = "heuristic"
        self.openai_invocations = 0
        self.langchain_used = False
        self.last_rendered_inputs: dict[str, str] = {}
        self.last_outputs: dict[str, dict[str, Any]] = {}

    def configure(
        self,
        *,
        provider: str,
        model_name: str,
        max_hypotheses: int,
        prompts: PromptsConfig | None = None,
    ) -> None:
        self.provider = provider
        self.model_name = model_name
        self.max_hypotheses = max_hypotheses
        if prompts is not None:
            self.prompts = prompts

    async def analyze_log(
        self,
        *,
        run_id: str,
        log_text: str,
        service_name: str | None,
        module_path: str | None,
        tracing: LangSmithTracingConfig,
    ) -> LogAnalysisOutput:
        del run_id, tracing
        excerpt = redact_sensitive_text(log_text)[-8_000:]
        candidate = parse_incident_candidate(
            excerpt, service_name=service_name, module_path=module_path
        )
        evidence = [line.strip() for line in excerpt.splitlines() if line.strip()][-8:]
        output = LogAnalysisOutput(
            error_name=candidate.error_name,
            description=candidate.description,
            service_name=candidate.service_name,
            likely_files=candidate.likely_files,
            observations=[f"Observed {candidate.error_name}: {candidate.description}"],
            direct_evidence=evidence,
        )
        self._record(
            "LogAnalysisAgent",
            self.prompts.log_analysis_agent.human,
            {"service_name": service_name or "service", "log_excerpt": excerpt},
            output,
        )
        return output

    async def create_plan(
        self,
        *,
        run_id: str,
        incident_id: str,
        incident: IncidentCandidate,
        repo_path: Path,
        test_command: str,
        analysis: LogAnalysisOutput,
        repo_url: str,
        target_branch: str,
        build_system: str,
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
        plan = PlatformRepairPlan(
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
        structured = StructuredPlanOutput(
            observations=plan.observations,
            assumptions=plan.assumptions,
            hypotheses=plan.hypotheses,
            selected_hypothesis=plan.selected_hypothesis,
            selection_reason=plan.selection_reason,
            planned_edits=plan.planned_edits,
            planned_tests=plan.planned_tests,
            validation_plan=[test_command],
            risk_notes=["Deterministic patch support is limited to the reviewed MVP scenarios."],
            jira_solution=(
                f"Address {plan.selected_hypothesis}. Validate with {test_command}."
            ),
        )
        self._record(
            "RepairPlanningAgent",
            self.prompts.repair_planning_agent.human,
            {
                "service_name": incident.service_name or "service",
                "error_name": incident.error_name,
                "analysis": json.dumps(analysis.model_dump(mode="json")),
                "repo_url": repo_url,
                "target_branch": target_branch,
                "build_system": build_system,
                "test_command": test_command,
                "supported_scenarios": ", ".join(SUPPORTED_SCENARIOS),
            },
            structured,
        )
        return plan

    async def review_pr(
        self,
        *,
        run_id: str,
        service_name: str,
        jira_key: str,
        branch_name: str,
        patch_summary: str,
        changed_files: list[str],
        test_command: str,
        test_result: str,
        risk_notes: list[str],
        selected_hypothesis: str,
        planning_output: dict[str, Any],
        tracing: LangSmithTracingConfig,
    ) -> ReviewPrOutput:
        del run_id, tracing
        output = ReviewPrOutput(
            pr_title=f"Repair {service_name} reviewed failure",
            pr_summary=(
                f"Root cause and deterministic patch: {patch_summary}. "
                f"Validation: {test_command}: {test_result}."
            ),
            review_checklist=[
                "Confirm the patch matches the reported failure.",
                "Review test evidence before merge.",
            ],
            risk_notes=risk_notes,
            jira_comment=f"Repair review prepared for {jira_key}; no merge is claimed.",
        )
        self._record(
            "ReviewPrAgent",
            self.prompts.review_pr_agent.human,
            {
                "service_name": service_name,
                "jira_key": jira_key,
                "branch_name": branch_name,
                "patch_summary": patch_summary,
                "changed_files": ", ".join(changed_files) or "none",
                "test_command": test_command,
                "test_result": test_result,
                "risk_notes": "; ".join(risk_notes) or "none",
                "selected_hypothesis": selected_hypothesis,
                "planning_output": json.dumps(planning_output),
            },
            output,
        )
        return output

    def _record(
        self,
        name: str,
        template: str,
        values: dict[str, Any],
        output: BaseModel,
    ) -> None:
        self.last_rendered_inputs[name] = render_agent_prompt(template, values)
        self.last_outputs[name] = _safe_output(output)


class LangChainStructuredPlanner(HeuristicStructuredPlanner):
    """Three-call OpenAI suite using LangChain messages and structured outputs."""

    def __init__(self, model: Any, prompts: PromptsConfig | None = None) -> None:
        super().__init__()
        self._model = model
        self.prompts = prompts or PromptsConfig(planner_mode="openai")
        self.planner_mode = "openai"
        self.langchain_used = True
        self.agents = {
            "LogAnalysisAgent": LogAnalysisAgent(model),
            "RepairPlanningAgent": RepairPlanningAgent(model),
            "ReviewPrAgent": ReviewPrAgent(model),
        }

    def configure(
        self,
        *,
        provider: str,
        model_name: str,
        max_hypotheses: int,
        prompts: PromptsConfig | None = None,
    ) -> None:
        self.provider = provider
        self.model_name = model_name
        self.max_hypotheses = max_hypotheses
        if prompts is not None:
            self.prompts = prompts

    async def analyze_log(self, **kwargs: Any) -> LogAnalysisOutput:
        values = {
            "service_name": kwargs.get("service_name") or "service",
            "log_excerpt": redact_sensitive_text(kwargs["log_text"])[-8_000:],
        }
        return await self._invoke(
            name="LogAnalysisAgent",
            prompt=self.prompts.log_analysis_agent,
            schema=LogAnalysisOutput,
            values=values,
            run_id=kwargs["run_id"],
            tracing=kwargs["tracing"],
        )

    async def create_plan(
        self,
        *,
        run_id: str,
        incident_id: str,
        incident: IncidentCandidate,
        repo_path: Path,
        test_command: str,
        analysis: LogAnalysisOutput,
        repo_url: str,
        target_branch: str,
        build_system: str,
        tracing: LangSmithTracingConfig,
    ) -> PlatformRepairPlan:
        del repo_path
        output = await self._invoke(
            name="RepairPlanningAgent",
            prompt=self.prompts.repair_planning_agent,
            schema=StructuredPlanOutput,
            values={
                "service_name": incident.service_name or "service",
                "error_name": incident.error_name,
                "analysis": json.dumps(analysis.model_dump(mode="json")),
                "repo_url": repo_url,
                "target_branch": target_branch,
                "build_system": build_system,
                "test_command": test_command,
                "supported_scenarios": ", ".join(SUPPORTED_SCENARIOS),
            },
            run_id=run_id,
            tracing=tracing,
        )
        plan_id = hashlib.sha256(
            f"{run_id}|{incident_id}|{output.selected_hypothesis}".encode("utf-8")
        ).hexdigest()[:16]
        output_data = output.model_dump(
            exclude={"validation_plan", "risk_notes", "jira_solution"}
        )
        output_data["hypotheses"] = output_data["hypotheses"][: self.max_hypotheses]
        return PlatformRepairPlan(
            plan_id=plan_id,
            run_id=run_id,
            incident_id=incident_id,
            **output_data,
        )

    async def review_pr(self, **kwargs: Any) -> ReviewPrOutput:
        values = {
            "service_name": kwargs["service_name"],
            "jira_key": kwargs["jira_key"],
            "branch_name": kwargs["branch_name"],
            "patch_summary": kwargs["patch_summary"],
            "changed_files": ", ".join(kwargs["changed_files"]) or "none",
            "test_command": kwargs["test_command"],
            "test_result": kwargs["test_result"],
            "risk_notes": "; ".join(kwargs["risk_notes"]) or "none",
            "selected_hypothesis": kwargs["selected_hypothesis"],
            "planning_output": json.dumps(kwargs["planning_output"]),
        }
        return await self._invoke(
            name="ReviewPrAgent",
            prompt=self.prompts.review_pr_agent,
            schema=ReviewPrOutput,
            values=values,
            run_id=kwargs["run_id"],
            tracing=kwargs["tracing"],
        )

    async def _invoke(
        self,
        *,
        name: str,
        prompt: Any,
        schema: type[BaseModel],
        values: dict[str, Any],
        run_id: str,
        tracing: LangSmithTracingConfig,
    ) -> Any:
        agent = self.agents[name]
        if agent.schema is not schema:
            raise LangStackAgentError(f"{name} structured schema is misconfigured")
        self.openai_invocations += 1
        try:
            validated, rendered_human = await agent.invoke(
                prompt=prompt,
                values=values,
                run_id=run_id,
                tracing=tracing,
            )
        finally:
            self.last_rendered_inputs[name] = agent.last_rendered_input
        self.last_outputs[name] = _safe_output(validated)
        return validated


def _safe_output(output: BaseModel) -> dict[str, Any]:
    value = output.model_dump(mode="json")
    return _redact_structure(value)


def _redact_structure(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_structure(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


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
        error_name=error_name,
        description=description,
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
