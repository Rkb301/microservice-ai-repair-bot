"""Project-aligned MVP product boundaries exposed to docs and API consumers."""

from __future__ import annotations

from typing import Final


MAX_TOTAL_REPOSITORIES: Final = 4
MAX_MICROSERVICE_REPOSITORIES: Final = 3
CONCEPTUAL_AGENTS: Final = (
    "Log Analysis Agent",
    "Repair Planning Agent",
    "Code Repair Agent",
)
MVP_MODEL: Final = "openai:gpt-5-mini"
ENTERPRISE_FUTURE_MODEL_CANDIDATES: Final = 5


def project_mvp_metadata() -> dict[str, object]:
    return {
        "max_total_repositories": MAX_TOTAL_REPOSITORIES,
        "max_microservice_repositories": MAX_MICROSERVICE_REPOSITORIES,
        "conceptual_agents": list(CONCEPTUAL_AGENTS),
        "mvp_model": MVP_MODEL,
        "multi_model_selection_implemented": False,
        "enterprise_future_model_candidates": ENTERPRISE_FUTURE_MODEL_CANDIDATES,
    }


