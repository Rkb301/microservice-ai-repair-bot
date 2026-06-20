"""Explicit, opt-in verification for the real OpenAI structured planner path."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OpenAIPlannerCheckError(RuntimeError):
    """Safe planner verification failure."""


class PlannerCheckOutput(BaseModel):
    hypothesis: str = Field(min_length=1, max_length=240)
    proposed_change: str = Field(min_length=1, max_length=240)
    test_command: str = Field(min_length=1, max_length=120)


async def run_planner_check(configured_model: str) -> str:
    if not configured_model.startswith("openai:"):
        raise OpenAIPlannerCheckError(
            "Configured model must use the openai provider prefix"
        )
    model_name = configured_model.split(":", 1)[1]
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise OpenAIPlannerCheckError(
            "Install project dependencies to enable the OpenAI planner check"
        ) from exc
    try:
        model = ChatOpenAI(model=model_name, max_retries=0)
        structured = model.with_structured_output(PlannerCheckOutput)
        result = await structured.ainvoke(
            "Return a tiny repair plan for a missing null guard. "
            "Use 'mvn test' as the test command. Do not include credentials."
        )
    except Exception:
        raise OpenAIPlannerCheckError(
            "The structured OpenAI planner request did not complete"
        ) from None
    PlannerCheckOutput.model_validate(result)
    return configured_model


__all__ = ["OpenAIPlannerCheckError", "PlannerCheckOutput", "run_planner_check"]
