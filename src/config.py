"""Configuration publique du pipeline ARIA News.

Les valeurs sensibles et les URL de l'infrastructure de production ne sont jamais
stockées dans le dépôt. Elles sont injectées via l'environnement au runtime.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv

load_dotenv()

APP_ROOT = Path(os.getenv("ARIA_WORKDIR", "/app")).resolve()
DATA_MAIN_DIR = APP_ROOT / "data_main"
DATA_NEWS_DIR = APP_ROOT / "data_news"
OUTBOUND_DIR = APP_ROOT / "json_rss_to_send"
LOG_DIR = APP_ROOT / "data_logs"
HOTNEWS_DIR = APP_ROOT / "RESUM_Hot_News"
MARKET_DIR = APP_ROOT / "data_coinmarketcap"
BLOCKCHAIN_DIR = APP_ROOT / "data_scan_explorer_tokens"

ARIA_API_BASE_URL = os.getenv("ARIA_API_BASE_URL", "https://example.invalid").rstrip("/")
ARIA_WEB_BASE_URL = os.getenv("ARIA_WEB_BASE_URL", "https://example.invalid").rstrip("/")
ARIA_API_KEY_HEADER = os.getenv("ARIA_API_KEY_HEADER", "X-Api-Key")
ARIA_WALLET_API_KEY_HEADER = os.getenv("ARIA_WALLET_API_KEY_HEADER", "X-Api-Key")
PLATFORM_SOURCE_DOMAIN = os.getenv("ARIA_PLATFORM_SOURCE_DOMAIN", "platform.example")
HOTNEWS_EXCLUDED_SOURCE_TOKENS = tuple(
    token.strip().lower()
    for token in os.getenv("ARIA_HOTNEWS_EXCLUDED_SOURCE_TOKENS", "").split(",")
    if token.strip()
)
SOURCE_DOMAIN_ALIASES = {
    source.strip().lower(): target.strip().lower()
    for item in os.getenv("ARIA_SOURCE_DOMAIN_ALIASES", "").split(",")
    if "=" in item
    for source, target in [item.split("=", 1)]
    if source.strip() and target.strip()
}

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))
OPENAI_MAX_RETRIES = int(os.getenv("OPENAI_MAX_RETRIES", "2"))

HTTP_TIMEOUT_SECONDS = float(os.getenv("ARIA_HTTP_TIMEOUT_SECONDS", "15"))
MAX_HTTP_RESPONSE_BYTES = int(os.getenv("ARIA_MAX_HTTP_RESPONSE_BYTES", str(10 * 1024 * 1024)))
MAX_REDIRECTS = int(os.getenv("ARIA_MAX_REDIRECTS", "5"))
ALLOW_PRIVATE_API_HOSTS = os.getenv("ARIA_ALLOW_PRIVATE_API_HOSTS", "false").lower() in {"1", "true", "yes"}

NEWS_CYCLE_SECONDS = int(os.getenv("ARIA_NEWS_CYCLE_SECONDS", "300"))
SUBPROCESS_TIMEOUT_SECONDS = int(os.getenv("ARIA_SUBPROCESS_TIMEOUT_SECONDS", "1800"))
MARKET_RUN_AFTER_UTC_HOUR = int(os.getenv("ARIA_MARKET_RUN_AFTER_UTC_HOUR", "7"))
DAILY_BRIEF_AFTER_UTC_HOUR = int(os.getenv("ARIA_DAILY_BRIEF_AFTER_UTC_HOUR", "7"))
DAILY_BRIEF_AFTER_UTC_MINUTE = int(os.getenv("ARIA_DAILY_BRIEF_AFTER_UTC_MINUTE", "10"))
BLOCKCHAIN_RUN_AFTER_UTC_HOUR = int(os.getenv("ARIA_BLOCKCHAIN_RUN_AFTER_UTC_HOUR", "3"))
BLOCKCHAIN_POLL_SECONDS = int(os.getenv("ARIA_BLOCKCHAIN_POLL_SECONDS", "60"))
BLOCKCHAIN_RETRY_SECONDS = int(os.getenv("ARIA_BLOCKCHAIN_RETRY_SECONDS", "900"))

# Routes publiques génériques. Les routes de production ne sont pas publiées.
NEWS_API_PATH = os.getenv("ARIA_NEWS_API_PATH", "/api/v1/news")
WORDLIST_API_PATH = os.getenv("ARIA_WORDLIST_API_PATH", "/api/v1/news/wordlist")
LATEST_NEWS_API_PATH = os.getenv("ARIA_LATEST_NEWS_API_PATH", "/api/v1/news/latest")
ADD_NEWS_API_PATH = os.getenv("ARIA_ADD_NEWS_API_PATH", "/api/v1/news/add")
UPDATE_NEWS_API_PATH = os.getenv("ARIA_UPDATE_NEWS_API_PATH", "/api/v1/news/update")
UPDATE_FULL_TEXT_API_PATH = os.getenv("ARIA_UPDATE_FULL_TEXT_API_PATH", "/api/v1/news/update/full-text")
UPDATE_LLM_STATUS_API_PATH = os.getenv("ARIA_UPDATE_LLM_STATUS_API_PATH", "/api/v1/news/update/llm-summary")
COINLIST_API_PATH = os.getenv("ARIA_COINLIST_API_PATH", "/api/v1/coinlist")
WALLET_HOLDERS_API_PATH = os.getenv("ARIA_WALLET_HOLDERS_API_PATH", "/api/v1/wallet-holders")
WALLET_HOLDERS_PUSH_API_PATH = os.getenv("ARIA_WALLET_HOLDERS_PUSH_API_PATH", "/api/v1/wallet-holders/push")
SOURCE_SCORE_PUSH_API_PATH = os.getenv("ARIA_SOURCE_SCORE_PUSH_API_PATH", "/api/v1/source-score/push")

ETHERSCAN_API_URL = os.getenv("ETHERSCAN_API_URL", "https://api.etherscan.io/api")
COINMARKETCAP_EXCHANGES_URL = os.getenv(
    "COINMARKETCAP_EXCHANGES_URL", "https://coinmarketcap.com/rankings/exchanges/"
)


def require_env(name: str) -> str:
    """Retourne une variable obligatoire sans jamais en journaliser la valeur."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Variable d'environnement obligatoire absente : {name}")
    return value


def api_url(path: str) -> str:
    """Construit une URL d'API en refusant toute route absolue ou cross-origin."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("La route API doit être une chaîne relative non vide")
    parsed_path = urlparse(path)
    if parsed_path.scheme or parsed_path.netloc or path.startswith("//"):
        raise ValueError("Les routes API absolues ou cross-origin sont interdites")

    base = f"{ARIA_API_BASE_URL}/"
    result = urljoin(base, path.lstrip("/"))
    if urlparse(result).hostname != urlparse(ARIA_API_BASE_URL).hostname:
        raise ValueError("La route API doit rester sur l'hôte configuré")
    return result


def public_news_url(news_id: object) -> str:
    """Construit le lien public d'une news sans exposer l'URL de production."""
    return urljoin(f"{ARIA_WEB_BASE_URL}/", f"news/{news_id}")


def validate_api_base_url() -> None:
    """Refuse une configuration API accidentellement en HTTP clair."""
    parsed = urlparse(ARIA_API_BASE_URL)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError("ARIA_API_BASE_URL doit être une URL HTTPS valide")
