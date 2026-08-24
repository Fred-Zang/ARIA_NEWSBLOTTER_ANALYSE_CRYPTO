"""Worker quotidien de métriques ERC-20.

Le worker récupère la coin list applicative, complète les décimales et holders via
Etherscan, calcule les métriques synthétiques puis les publie vers l'API configurée.
"""
from __future__ import annotations

import logging
import math
import os
import re
import signal
import sys
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.blockchain.etherscan_methods import (
    fetch_decimals,
    fetch_holders,
    get_coin_list,
    get_wallet_holders,
    post_wallet_holders,
)
from src.config import (
    BLOCKCHAIN_DIR,
    BLOCKCHAIN_POLL_SECONDS,
    BLOCKCHAIN_RETRY_SECONDS,
    BLOCKCHAIN_RUN_AFTER_UTC_HOUR,
    COINLIST_API_PATH,
    WALLET_HOLDERS_API_PATH,
    WALLET_HOLDERS_PUSH_API_PATH,
    api_url,
    require_env,
    validate_api_base_url,
)
from src.io_utils import atomic_write_json

logger = logging.getLogger(__name__)
STOP_EVENT = threading.Event()
CONTRACT_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _stop(_signum: int, _frame: object) -> None:
    STOP_EVENT.set()


def configure_logging() -> None:
    BLOCKCHAIN_DIR.mkdir(parents=True, exist_ok=True)
    log_file = BLOCKCHAIN_DIR / f"blockchain_{datetime.now(timezone.utc):%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
        force=True,
    )


def _normalize_contract(value: object) -> str | None:
    address = str(value).strip()
    return address if CONTRACT_RE.fullmatch(address) else None


def _build_token_map(coin_list: pd.DataFrame, history: pd.DataFrame) -> dict[str, dict[str, Any]]:
    required = {"symbol", "network", "contract_adress"}
    missing = required - set(coin_list.columns)
    if missing:
        raise ValueError(f"Coin list incomplète : {sorted(missing)}")

    erc20 = coin_list[coin_list["network"].astype(str).str.upper() == "ERC-20"].copy()
    token_map: dict[str, dict[str, Any]] = {}
    history_lookup: dict[str, int] = {}
    if not history.empty and {"contract_address", "decimals"}.issubset(history.columns):
        for _, row in history.dropna(subset=["contract_address", "decimals"]).iterrows():
            try:
                history_lookup[str(row["contract_address"]).lower()] = int(row["decimals"])
            except (TypeError, ValueError):
                continue

    for _, row in erc20.iterrows():
        ticker = str(row["symbol"]).strip().upper()
        address = _normalize_contract(row["contract_adress"])
        if not ticker or not address:
            logger.warning("Entrée ERC-20 ignorée : ticker ou contract address invalide")
            continue
        item = token_map.setdefault(ticker, {"contract_address": [], "decimals": np.nan})
        if address not in item["contract_address"]:
            item["contract_address"].append(address)
        known = history_lookup.get(address.lower())
        if known is not None:
            item["decimals"] = known

    if not token_map:
        raise RuntimeError("Aucun contrat ERC-20 valide à traiter")
    return token_map


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if math.isnan(float(value)) else float(value)
    if pd.isna(value):
        return None
    return value


def _prepare_payload(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        raise RuntimeError("Aucune métrique ERC-20 n'a été calculée")
    renamed = frame.rename(
        columns={
            "quantity": "significant_holders_quantity",
            "percentage": "significant_holders_perc",
            "symbol": "ticker",
            "Contract Address": "contract_address",
            "datetime": "date",
            "total_token_holders": "unique_holders",
            "holders_total_supply": "circulating_supply",
        }
    ).copy()
    for column in renamed.columns:
        renamed[column] = renamed[column].map(_json_safe)
    return renamed


def run_once() -> int:
    validate_api_base_url()
    aria_api_key = require_env("ARIA_API_KEY")
    wallet_api_key = require_env("ARIA_WALLET_API_KEY")
    etherscan_key = require_env("ETHERSCAN_API_KEY")

    coin_list = get_coin_list(api_url(COINLIST_API_PATH), aria_api_key)
    history = get_wallet_holders(api_url(WALLET_HOLDERS_API_PATH), wallet_api_key)
    token_map = _build_token_map(coin_list, history)
    token_map = fetch_decimals(token_map, etherscan_key, history)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    atomic_write_json(BLOCKCHAIN_DIR / f"tokens_{timestamp}.json", token_map)

    summaries: list[pd.DataFrame] = []
    failed_tokens: list[str] = []
    for ticker, item in token_map.items():
        decimals = item.get("decimals")
        if decimals is None or pd.isna(decimals):
            logger.warning("%s ignoré : decimals indisponibles", ticker)
            failed_tokens.append(ticker)
            continue
        try:
            summary, _holders, anomaly = fetch_holders(
                token_map,
                ticker,
                item["contract_address"],
                etherscan_key,
            )
            if summary.empty:
                failed_tokens.append(ticker)
                continue
            if anomaly:
                logger.warning("Anomalie de cohérence détectée pour %s", ticker)
            summaries.append(summary)
        except Exception:
            logger.exception("Échec de traitement du token %s", ticker)
            failed_tokens.append(ticker)

    if not summaries:
        raise RuntimeError("Aucun token ERC-20 n'a pu être analysé")

    payload_frame = _prepare_payload(pd.concat(summaries, ignore_index=True))
    atomic_write_json(
        BLOCKCHAIN_DIR / f"wallet_holders_{timestamp}.json",
        payload_frame.to_dict(orient="records"),
    )
    post_wallet_holders(payload_frame, api_url(WALLET_HOLDERS_PUSH_API_PATH), wallet_api_key)
    logger.info(
        "Traitement ERC-20 terminé : %s token(s) publiés, %s en échec",
        len(payload_frame),
        len(failed_tokens),
    )
    return len(payload_frame)


def main() -> None:
    os.umask(0o077)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    last_success_date = None

    while not STOP_EVENT.is_set():
        configure_logging()
        now = datetime.now(timezone.utc)
        if now.hour >= BLOCKCHAIN_RUN_AFTER_UTC_HOUR and last_success_date != now.date():
            try:
                run_once()
                last_success_date = now.date()
            except Exception:
                logger.exception("Échec du traitement ERC-20 quotidien")
                if STOP_EVENT.wait(BLOCKCHAIN_RETRY_SECONDS):
                    break
                continue
        STOP_EVENT.wait(BLOCKCHAIN_POLL_SECONDS)


if __name__ == "__main__":
    main()
