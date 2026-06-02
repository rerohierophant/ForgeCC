from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"


@dataclass(frozen=True)
class AgentConfig:
    api_key: str
    base_url: str
    model: str
    max_turns: int = 8
    temperature: float = 0.2

    @classmethod
    def from_env(
        cls,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        temperature: float | None = None,
    ) -> "AgentConfig":
        resolved_api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        resolved_base_url = base_url or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL
        resolved_model = model or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL
        resolved_max_turns = max_turns or int(os.getenv("CCA_MAX_TURNS", "8"))
        resolved_temperature = temperature
        if resolved_temperature is None:
            resolved_temperature = float(os.getenv("CCA_TEMPERATURE", "0.2"))

        return cls(
            api_key=resolved_api_key,
            base_url=resolved_base_url.rstrip("/"),
            model=resolved_model,
            max_turns=resolved_max_turns,
            temperature=resolved_temperature,
        )

    def validate(self) -> None:
        if not self.api_key:
            raise ValueError(
                "Missing OPENAI_API_KEY. Set it in PowerShell with "
                '$env:OPENAI_API_KEY="your-key".'
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("OPENAI_BASE_URL must start with http:// or https://.")
        if not self.model:
            raise ValueError("Model name cannot be empty.")
