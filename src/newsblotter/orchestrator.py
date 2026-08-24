"""Orchestrateur du Newsblotter.

Le cycle collecte les RSS classiques puis spéciaux, publie les nouvelles entrées,
déclenche la consolidation Hot News lorsqu'un flag est présent et lance la collecte
marché au plus une fois par jour.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from src.config import (
    HOTNEWS_DIR,
    LOG_DIR,
    MARKET_DIR,
    OUTBOUND_DIR,
    MARKET_RUN_AFTER_UTC_HOUR,
    NEWS_CYCLE_SECONDS,
    SUBPROCESS_TIMEOUT_SECONDS,
)

HOTNEWS_FLAG = HOTNEWS_DIR / ".hot_news_detected.flag"
MARKET_OUTPUT = MARKET_DIR / "exchanges_scores.json"
STOP_EVENT = threading.Event()

SOURCE_WORKERS = (
    "src.newsblotter.rss_classic",
    "src.newsblotter.rss_special",
)


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"newsblotter_{datetime.now(timezone.utc):%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
        force=True,
    )


def _handle_stop(signum: int, _frame: object) -> None:
    logging.getLogger(__name__).info("Signal %s reçu, arrêt demandé", signum)
    STOP_EVENT.set()


def run_module(module: str, *args: str) -> bool:
    """Exécute un sous-worker avec héritage stdout/stderr et timeout borné."""
    command = [sys.executable, "-m", module, *args]
    logger = logging.getLogger(__name__)
    try:
        result = subprocess.run(command, check=False, timeout=SUBPROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.error("Timeout du worker %s après %s s", module, SUBPROCESS_TIMEOUT_SECONDS)
        return False
    except OSError:
        logger.exception("Impossible de lancer le worker %s", module)
        return False

    if result.returncode != 0:
        logger.error("Le worker %s a échoué (code %s)", module, result.returncode)
        return False
    logger.info("Worker %s terminé avec succès", module)
    return True


def is_modified_today_utc(path: Path) -> bool:
    if not path.exists():
        return False
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return modified.date() == datetime.now(timezone.utc).date()


def maybe_run_hotnews(publish_succeeded: bool) -> None:
    if not publish_succeeded or not HOTNEWS_FLAG.exists():
        return
    logger = logging.getLogger(__name__)
    logger.info("Hot News détectée : lancement de la consolidation LLM")
    if run_module("src.hotnews.hotnews_worker"):
        try:
            HOTNEWS_FLAG.unlink(missing_ok=True)
        except OSError:
            logger.exception("Impossible de supprimer le flag Hot News après succès")
    else:
        logger.error("Consolidation Hot News en échec : le flag est conservé pour un nouvel essai")


def maybe_run_market_data() -> None:
    now = datetime.now(timezone.utc)
    if now.hour < MARKET_RUN_AFTER_UTC_HOUR or is_modified_today_utc(MARKET_OUTPUT):
        return
    logger = logging.getLogger(__name__)
    for attempt in range(1, 4):
        logger.info("Collecte Market Data, tentative %s/3", attempt)
        if run_module("src.market_data.coinmarketcap_worker") and is_modified_today_utc(MARKET_OUTPUT):
            return
        if attempt < 3 and not STOP_EVENT.wait(60):
            continue
        break
    logger.error("La collecte Market Data n'a pas produit d'artefact du jour")


def run_cycle() -> None:
    publish_path = OUTBOUND_DIR / "last_RSS_news_found.json"
    logger = logging.getLogger(__name__)

    # Un payload resté sur disque provient d'une publication incomplète : il est
    # toujours vidé avant de collecter de nouvelles entrées afin de ne pas l'écraser.
    if publish_path.exists():
        if not run_module("src.newsblotter.publish_news", str(publish_path)):
            logger.error("File de publication encore en échec : nouvelle collecte reportée")
            maybe_run_hotnews(True)
            maybe_run_market_data()
            return
        maybe_run_hotnews(True)

    # Un flag Hot News persistant est également rejoué après redémarrage du conteneur.
    maybe_run_hotnews(True)

    for source_module in SOURCE_WORKERS:
        if not run_module(source_module):
            logger.error("Collecte %s en échec : publication ignorée", source_module)
            continue
        if not run_module("src.newsblotter.publish_news", str(publish_path)):
            logger.error("Publication en échec : arrêt du cycle pour conserver la file de retry")
            break
        maybe_run_hotnews(True)

    maybe_run_market_data()
    memory_usage = psutil.virtual_memory().percent
    cpu_usage = psutil.cpu_percent(interval=0.2)
    logger.info("Fin de cycle : mémoire %.1f%%, CPU %.1f%%", memory_usage, cpu_usage)


def main() -> None:
    os.umask(0o077)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    while not STOP_EVENT.is_set():
        configure_logging()
        try:
            run_cycle()
        except Exception:
            logging.getLogger(__name__).exception("Erreur non prévue pendant un cycle Newsblotter")
        logging.getLogger(__name__).info("Attente de %s secondes avant le prochain cycle", NEWS_CYCLE_SECONDS)
        STOP_EVENT.wait(NEWS_CYCLE_SECONDS)


if __name__ == "__main__":
    main()
