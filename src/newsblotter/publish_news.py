"""Publication fiable des news enrichies vers l'API applicative."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.config import (
    ADD_NEWS_API_PATH,
    APP_ROOT,
    ARIA_API_KEY_HEADER,
    HOTNEWS_DIR,
    LATEST_NEWS_API_PATH,
    OUTBOUND_DIR,
    UPDATE_FULL_TEXT_API_PATH,
    api_url,
    require_env,
    validate_api_base_url,
)
from src.http_safety import safe_get, safe_post
from src.io_utils import atomic_write_json, atomic_write_text, resolve_within

logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)
HOTNEWS_FLAG = HOTNEWS_DIR / ".hot_news_detected.flag"
MAX_INPUT_JSON_BYTES = 5 * 1024 * 1024


def _headers(api_key: str) -> dict[str, str]:
    return {"Content-Type": "application/json", ARIA_API_KEY_HEADER: api_key}


def load_outbound_json(path_value: str) -> tuple[Path, list[dict]]:
    path = resolve_within(OUTBOUND_DIR, Path(path_value).name)
    if not path.is_file():
        return path, []
    if path.stat().st_size > MAX_INPUT_JSON_BYTES:
        raise ValueError("Le JSON à publier dépasse la taille maximale autorisée")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("Le JSON attendu doit être une liste d'objets")
    return path, payload


def get_last_news_id(api_key: str) -> int:
    response = safe_get(api_url(LATEST_NEWS_API_PATH), headers=_headers(api_key))
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or "id" not in data:
        raise ValueError("Réponse inattendue pour le dernier ID news")
    return int(data["id"])


def _utc_iso(value: object) -> str:
    if value is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def send_full_text(news_id: int, relative_path: str, api_key: str) -> None:
    if not relative_path.strip():
        return
    full_path = resolve_within(APP_ROOT, relative_path.lstrip("/"))
    if not full_path.is_file():
        logger.warning("Texte complet absent pour la news %s", news_id)
        return
    if full_path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError(f"Texte complet anormalement volumineux pour la news {news_id}")

    full_text = full_path.read_text(encoding="utf-8")
    if len(full_text.strip()) < 500:
        logger.info("Texte complet trop court pour publication séparée (news %s)", news_id)
        return

    response = safe_post(
        api_url(UPDATE_FULL_TEXT_API_PATH),
        json={"id": news_id, "full_text": full_text},
        headers=_headers(api_key),
    )
    response.raise_for_status()


def _mark_hotnews() -> None:
    atomic_write_text(HOTNEWS_FLAG, "True")


def publish_news(row: dict, api_key: str) -> None:
    news_id = int(row["id_news"])
    payload = {
        "id": news_id,
        "real_links": row.get("real_links"),
        "titles": row.get("titles"),
        "list_cryptos": row.get("list_cryptos"),
        "tot_cat_words": row.get("TOT_cat_words"),
        "cat_risks": row.get("cat_risks"),
        "cat_governance": row.get("cat_governance"),
        "cat_tokenomics": row.get("cat_tokenomics"),
        "cat_adoption": row.get("cat_adoption"),
        "cat_investment_analytics": row.get("cat_investment_analytics"),
        "resum_news": row.get("resum_news"),
        "utc_datetime_news": _utc_iso(row.get("UTC_datetime_news")),
        "source_domain": row.get("source_domain"),
        "path_text_files": row.get("path_TEXT_files"),
        "hot_news": bool(row.get("hot_news")),
        "actor": row.get("Actor"),
        "tradfi": row.get("TradFi"),
        "action": row.get("Action"),
        "crypto": row.get("Crypto"),
        "llm_resum": bool(row.get("llm_resum")),
        "full_text": row.get("full_text"),
    }
    logger.info("Publication de la news %s", news_id)
    headers = _headers(api_key)
    headers["Idempotency-Key"] = f"news-{news_id}"
    response = safe_post(api_url(ADD_NEWS_API_PATH), json=payload, headers=headers)
    response.raise_for_status()
    send_full_text(news_id, str(row.get("path_TEXT_files", "")), api_key)
    if row.get("hot_news"):
        _mark_hotnews()


def retry_post_steps_for_existing_news(row: dict, api_key: str) -> None:
    """Rend idempotentes les étapes postérieures au POST lors d'un retry."""
    news_id = int(row["id_news"])
    send_full_text(news_id, str(row.get("path_TEXT_files", "")), api_key)
    if row.get("hot_news"):
        _mark_hotnews()


def main(json_file: str) -> None:
    os.umask(0o077)
    validate_api_base_url()
    api_key = require_env("ARIA_API_KEY")
    queue_path, data = load_outbound_json(json_file)
    if not data:
        logger.info("Aucune news en attente de publication")
        return

    last_news_id = get_last_news_id(api_key)
    for news in data:
        news_id = int(news["id_news"])
        if news_id <= last_news_id:
            retry_post_steps_for_existing_news(news, api_key)
            continue
        publish_news(news, api_key)
        last_news_id = news_id

    atomic_write_json(queue_path, [])
    logger.info("File de publication vidée après succès complet")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        logger.error("Usage: python -m src.newsblotter.publish_news <json_file>")
        raise SystemExit(2)
    try:
        main(sys.argv[1])
    except (requests.RequestException, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        logger.exception("Échec de la publication Newsblotter : la file est conservée pour retry")
        raise SystemExit(1)
