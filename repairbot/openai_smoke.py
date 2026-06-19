"""Tiny explicit OpenAI API smoke test helper."""

from __future__ import annotations

import os

import requests

from .config import RepairBotSettings, load_local_env


class OpenAISmokeError(RuntimeError):
    """Raised when the explicit OpenAI smoke test cannot run."""


def run_openai_smoke_test(settings: RepairBotSettings | None = None) -> str:
    """Make one tiny Responses API call and return a short result string."""

    load_local_env()
    settings = settings or RepairBotSettings.from_env()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise OpenAISmokeError("OPENAI_API_KEY is not set. Add it to your shell or local .env first.")
    model = _openai_model_name(settings.model)
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": "Reply with exactly: ok",
            "max_output_tokens": 32,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise OpenAISmokeError(f"OpenAI smoke test failed ({response.status_code}): {response.text}")
    data = response.json()
    text = data.get("output_text") or _extract_output_text(data)
    return f"OpenAI smoke test succeeded with model {model}: {text.strip() or '(no text returned)'}"


def _openai_model_name(model: str) -> str:
    return model.split(":", 1)[1] if model.startswith("openai:") else model


def _extract_output_text(data: dict) -> str:
    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                chunks.append(text)
    return " ".join(chunks)
