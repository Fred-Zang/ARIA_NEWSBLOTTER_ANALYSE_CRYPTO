"""Génération d'un brief quotidien à partir des Hot News consolidées."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import signal
import sys
import threading
from pathlib import Path

from src.config import (
    DAILY_BRIEF_AFTER_UTC_HOUR,
    DAILY_BRIEF_AFTER_UTC_MINUTE,
    HOTNEWS_DIR,
    NEWS_CYCLE_SECONDS,
    OPENAI_MODEL,
    require_env,
)
from src.io_utils import atomic_write_text
from src.llm import get_openai_client
from src.prompts import build_daily_brief_prompt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)
STOP_EVENT = threading.Event()
SOURCE_FILE = HOTNEWS_DIR / "hotnews_TXT_today.txt"
OUTPUT_DIR = HOTNEWS_DIR / "daily_briefs"
MARKER_FILE = HOTNEWS_DIR / ".daily_brief_done"
MAX_SOURCE_BYTES = 5 * 1024 * 1024


def _stop(_signum: int, _frame: object) -> None:
    STOP_EVENT.set()


def modified_today_utc(path: Path) -> bool:
    if not path.exists():
        return False
    modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    return modified.date() == dt.datetime.now(dt.timezone.utc).date()


def marker_is_today() -> bool:
    return modified_today_utc(MARKER_FILE)


def due_now() -> bool:
    now = dt.datetime.now(dt.timezone.utc)
    return (now.hour, now.minute) >= (DAILY_BRIEF_AFTER_UTC_HOUR, DAILY_BRIEF_AFTER_UTC_MINUTE)


def generate_daily_brief() -> Path:
    require_env("OPENAI_API_KEY")
    if SOURCE_FILE.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Le fichier source du brief dépasse la limite autorisée")
    content = SOURCE_FILE.read_text(encoding="utf-8")
    article_numbers = [int(value) for value in re.findall(r"Article (\d+)", content)]
    article_count = max(article_numbers) if article_numbers else 0
    if article_count == 0 or not content.strip():
        raise ValueError("Aucun article exploitable dans le fichier Hot News du jour")

    prompt = build_daily_brief_prompt(content, article_count)
    max_tokens = min(1800, max(180, article_count * 45 + 90))
    response = get_openai_client().chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Return a concise factual crypto-news brief in plain English."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max_tokens,
    )
    text = response.choices[0].message.content
    if not text or not text.strip():
        raise ValueError("Le LLM a retourné un brief vide")

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output = OUTPUT_DIR / f"hotnews_brief_{timestamp}.txt"
    atomic_write_text(output, text.strip())
    atomic_write_text(MARKER_FILE, dt.datetime.now(dt.timezone.utc).isoformat())
    logger.info("Brief quotidien généré (%s article(s))", article_count)
    return output


def main() -> None:
    os.umask(0o077)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    while not STOP_EVENT.is_set():
        try:
            if SOURCE_FILE.exists() and modified_today_utc(SOURCE_FILE) and due_now() and not marker_is_today():
                generate_daily_brief()
        except Exception:
            logger.exception("Échec de génération du brief quotidien")
        STOP_EVENT.wait(NEWS_CYCLE_SECONDS)


if __name__ == "__main__":
    main()
