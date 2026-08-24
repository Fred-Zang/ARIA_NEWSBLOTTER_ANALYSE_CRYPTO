"""Client LLM centralisé pour les traitements éditoriaux publics."""
from __future__ import annotations

from functools import lru_cache

from openai import OpenAI

from src.config import OPENAI_MAX_RETRIES, OPENAI_TIMEOUT_SECONDS, require_env


@lru_cache(maxsize=1)
def get_openai_client() -> OpenAI:
    """Construit un client OpenAI avec timeout et retries explicites."""
    return OpenAI(
        api_key=require_env("OPENAI_API_KEY"),
        timeout=OPENAI_TIMEOUT_SECONDS,
        max_retries=OPENAI_MAX_RETRIES,
    )
