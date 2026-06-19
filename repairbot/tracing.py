"""LangSmith configuration hooks without retaining or printing credentials."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LangSmithTracingConfig:
    requested: bool
    enabled: bool
    project: str | None
    tags: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "LangSmithTracingConfig":
        requested = os.getenv("LANGSMITH_TRACING", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        # The key is checked but deliberately not retained in this object.
        enabled = requested and bool(os.getenv("LANGSMITH_API_KEY"))
        return cls(
            requested=requested,
            enabled=enabled,
            project=os.getenv("LANGSMITH_PROJECT") or None,
        )

    def invocation_config(self, *, run_id: str, step: str) -> dict[str, Any]:
        return {
            "run_name": f"repairbot.{step}",
            "tags": ["uploaded-log-repair", step, *self.tags],
            "metadata": {
                "repair_run_id": run_id,
                "workflow_step": step,
                "langsmith_project": self.project,
            },
        }
