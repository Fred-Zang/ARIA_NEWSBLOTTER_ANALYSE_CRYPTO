"""Collecte quotidienne d'indicateurs publics d'exchanges CoinMarketCap."""
from __future__ import annotations

import logging
import os
import sys
import time

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src.config import (
    ARIA_API_KEY_HEADER,
    COINMARKETCAP_EXCHANGES_URL,
    MARKET_DIR,
    SOURCE_SCORE_PUSH_API_PATH,
    api_url,
    require_env,
    validate_api_base_url,
)
from src.http_safety import safe_post, validate_https_url
from src.io_utils import atomic_write_json

logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)
OUTPUT_FILE = MARKET_DIR / "exchanges_scores.json"


def create_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1600,1200")
    service = Service(os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver"))
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(45)
    driver.set_script_timeout(30)
    return driver


def fetch_exchange_scores(url: str) -> dict[str, str]:
    validate_https_url(url)
    driver = create_driver()
    scores: dict[str, str] = {}
    try:
        driver.get(url)
        try:
            cookie_banner = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.ID, "onetrust-accept-btn-handler"))
            )
            cookie_banner.click()
        except Exception:
            logger.debug("Aucune bannière cookie à fermer")

        # Le sélecteur du bouton est volontairement générique : CoinMarketCap peut modifier ses classes CSS.
        for _ in range(12):
            buttons = driver.find_elements(By.XPATH, "//button[contains(., 'Show More')]")
            if not buttons:
                break
            try:
                buttons[0].click()
                time.sleep(1)
            except Exception:
                break

        WebDriverWait(driver, 20).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "table tbody tr")))
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 2:
                continue
            rank = cells[0].text.strip()
            name = cells[1].text.strip().split("\n", 1)[0]
            if rank and name:
                scores[name] = rank
    finally:
        driver.quit()
    return scores


def publish_scores(scores: dict[str, str], api_key: str) -> None:
    response = safe_post(
        api_url(SOURCE_SCORE_PUSH_API_PATH),
        json={"data": scores},
        headers={"Content-Type": "application/json", ARIA_API_KEY_HEADER: api_key},
        timeout=20,
    )
    response.raise_for_status()


def main() -> None:
    os.umask(0o077)
    validate_api_base_url()
    api_key = require_env("ARIA_API_KEY")
    scores = fetch_exchange_scores(COINMARKETCAP_EXCHANGES_URL)
    if not scores:
        raise RuntimeError("Aucune donnée CoinMarketCap récupérée")
    MARKET_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(OUTPUT_FILE, scores)
    publish_scores(scores, api_key)
    logger.info("Market Data mise à jour : %s exchange(s)", len(scores))


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, OSError, ValueError, RuntimeError):
        logger.exception("Échec fatal du worker Market Data")
        raise SystemExit(1)
