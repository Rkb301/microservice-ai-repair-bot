"""Explicit, opt-in verification for the real OpenAI structured planner path."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .platform_config import PROMPT_TEMPLATE_NAMES, PromptsConfig, render_agent_prompt
from .redaction import redact_sensitive_text


class OpenAIPlannerCheckError(RuntimeError):
    """Safe planner verification failure."""


class PlannerCheckOutput(BaseModel):
    hypothesis: str = Field(min_length=1, max_length=240)
    proposed_change: str = Field(min_length=1, max_length=240)
    test_command: str = Field(min_length=1, max_length=120)

    @field_validator("hypothesis", "proposed_change", "test_command")
    @classmethod
    def redacted_text(cls, value: str) -> str:
        return redact_sensitive_text(value)


class PlannerCheckResult(BaseModel):
    model_name: str
    api_invoked: bool
    prompt_names: list[str]
    rendered_prompt_count: int
    parsed_response: PlannerCheckOutput


async def run_planner_check(
    configured_model: str,
    prompts: PromptsConfig | None = None,
) -> PlannerCheckResult:
    if not configured_model.startswith("openai:"):
        raise OpenAIPlannerCheckError(
            "Configured model must use the openai provider prefix"
        )
    model_name = configured_model.split(":", 1)[1]
    prompt_pack = prompts or PromptsConfig(planner_mode="openai")
    values = {
        "service_name": "user-service",
        "log_excerpt": "NullPointerException at UserService.java:42",
        "error_name": "NullPointerException",
        "analysis": "Missing profile null guard supported by the stack frame.",
        "repo_url": "https://github.com/example/user-service.git",
        "target_branch": "main",
        "build_system": "maven",
        "test_command": "mvn test",
        "supported_scenarios": "user-service missing profile null guard",
        "jira_key": "DEMO-1",
        "branch_name": "bugfix/DEMO-1-user-service-null-pointer",
        "patch_summary": "Add the reviewed null guard.",
        "changed_files": "src/main/java/example/UserService.java",
        "test_result": "exit_code=0; passed=true",
        "risk_notes": "Human review remains required.",
        "selected_hypothesis": "A missing profile crosses the service boundary.",
        "planning_output": '{"planned_edits":["add reviewed null guard"]}',
    }
    rendered = {
        name: {
            "system": render_agent_prompt(getattr(prompt_pack, name).system, values),
            "human": render_agent_prompt(getattr(prompt_pack, name).human, values),
        }
        for name in PROMPT_TEMPLATE_NAMES
    }
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise OpenAIPlannerCheckError(
            "Install project dependencies to enable the OpenAI planner check"
        ) from exc
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        model = ChatOpenAI(model=model_name, max_retries=0)
        structured = model.with_structured_output(PlannerCheckOutput)
        result = await structured.ainvoke(
            [
                SystemMessage(
                    content=rendered["repair_planning_agent"]["system"]
                ),
                HumanMessage(
                    content=(
                        rendered["repair_planning_agent"]["human"]
                        + "\nReturn a tiny check response with one hypothesis, one proposed change, and the test command."
                    )
                ),
            ]
        )
    except Exception:
        raise OpenAIPlannerCheckError(
            "The structured OpenAI planner request did not complete"
        ) from None
    parsed = PlannerCheckOutput.model_validate(result)
    return PlannerCheckResult(
        model_name=configured_model,
        api_invoked=True,
        prompt_names=list(PROMPT_TEMPLATE_NAMES),
        rendered_prompt_count=len(rendered),
        parsed_response=parsed,
    )


__all__ = [
    "OpenAIPlannerCheckError",
    "PlannerCheckOutput",
    "PlannerCheckResult",
    "run_planner_check",
]
