"""Accès bornés aux données ERC-20 et à l'API applicative.

Cette version publique conserve la logique métier du worker historique tout en
centralisant les timeouts, la validation réseau et la préparation des payloads.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
import requests

from src.config import ARIA_API_KEY_HEADER, ARIA_WALLET_API_KEY_HEADER, ETHERSCAN_API_URL
from src.http_safety import safe_get, safe_post

BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0x000000000000000000000000000000000000deadbeef",
    "0xffffffffffffffffffffffffffffffffffffffff",
    "0xdeaddeaddeaddeaddeaddeaddeaddeaddead0000",
}
MAX_HOLDER_PAGES = 200
HOLDER_PAGE_SIZE = 10_000


def _json_object(response: requests.Response) -> dict[str, Any]:
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Réponse JSON inattendue")
    return data


def get_coin_list(api_url: str, api_key: str) -> pd.DataFrame:
    """Récupère la coin list applicative."""
    response = safe_get(
        api_url,
        headers={"Accept": "application/json", ARIA_API_KEY_HEADER: api_key},
        timeout=10,
    )
    response.raise_for_status()
    data = _json_object(response).get("data")
    if not isinstance(data, list):
        raise ValueError("Structure inattendue pour la coin list")
    return pd.DataFrame(data)


def get_wallet_holders(api_url: str, api_key: str) -> pd.DataFrame:
    """Récupère l'historique des métriques wallet holders."""
    response = safe_get(
        api_url,
        headers={"Accept": "application/json", ARIA_WALLET_API_KEY_HEADER: api_key},
        timeout=10,
    )
    response.raise_for_status()
    data = _json_object(response).get("data")
    if not isinstance(data, list):
        raise ValueError("Structure inattendue pour les wallet holders")
    frame = pd.DataFrame(data)
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return frame


def _etherscan_request(params: dict[str, Any], *, timeout: float = 10) -> dict[str, Any]:
    response = safe_get(ETHERSCAN_API_URL, params=params, timeout=timeout)
    response.raise_for_status()
    return _json_object(response)


def fetch_decimals(
    tokens: dict[str, dict[str, Any]],
    api_key: str,
    history: pd.DataFrame | None = None,
) -> dict[str, dict[str, Any]]:
    """Complète les décimales manquantes à partir d'Etherscan."""
    for ticker, item in tokens.items():
        current = item.get("decimals")
        if current is not None and not pd.isna(current):
            item["decimals"] = int(current)
            continue

        for contract_address in item.get("contract_address", []):
            time.sleep(random.uniform(0.5, 1.0))
            try:
                data = _etherscan_request(
                    {
                        "module": "token",
                        "action": "tokeninfo",
                        "contractaddress": contract_address,
                        "apikey": api_key,
                    }
                )
                result = data.get("result")
                if data.get("status") != "1" or not isinstance(result, list) or not result:
                    logging.warning("Decimals indisponibles pour %s (%s)", ticker, contract_address)
                    continue
                divisor = int(result[0]["divisor"])
                item["decimals"] = divisor
                _warn_if_decimals_changed(history, ticker, contract_address, divisor)
                break
            except (requests.RequestException, ValueError, KeyError, TypeError):
                logging.error("Échec de récupération des decimals pour %s", ticker)
    return tokens


def _warn_if_decimals_changed(
    history: pd.DataFrame | None,
    ticker: str,
    contract_address: str,
    decimals: int,
) -> None:
    if history is None or history.empty:
        return
    required = {"ticker", "contract_address", "decimals"}
    if not required.issubset(history.columns):
        return
    addresses = history["contract_address"].astype(str).str.lower()
    subset = history[(history["ticker"].astype(str) == ticker) & (addresses == contract_address.lower())]
    if subset.empty:
        return
    previous = subset.iloc[-1]["decimals"]
    if not pd.isna(previous) and int(previous) != decimals:
        logging.warning("Décalage de decimals pour %s : historique=%s, Etherscan=%s", ticker, previous, decimals)


def get_total_supply(
    ticker: str,
    contract_address: str,
    api_key: str,
    *,
    max_retries: int = 3,
    delay_seconds: float = 2,
) -> Decimal | None:
    """Récupère le total supply brut d'un contrat avec retry borné."""
    for attempt in range(1, max_retries + 1):
        try:
            data = _etherscan_request(
                {
                    "module": "stats",
                    "action": "tokensupply",
                    "contractaddress": contract_address,
                    "apikey": api_key,
                }
            )
            if data.get("status") == "1":
                return Decimal(str(data["result"]))
            logging.warning("Total supply indisponible pour %s, tentative %s/%s", ticker, attempt, max_retries)
        except (requests.RequestException, InvalidOperation, KeyError, ValueError):
            logging.error("Échec total supply pour %s, tentative %s/%s", ticker, attempt, max_retries)
        if attempt < max_retries:
            time.sleep(delay_seconds + random.uniform(0, 1.0))
    return None


def _holder_pages(contract_address: str, api_key: str, factor: Decimal) -> tuple[list[pd.DataFrame], int]:
    pages: list[pd.DataFrame] = []
    explored = 0
    for page in range(1, MAX_HOLDER_PAGES + 1):
        explored += 1
        time.sleep(random.uniform(0.55, 1.0))
        try:
            data = _etherscan_request(
                {
                    "module": "token",
                    "action": "tokenholderlist",
                    "contractaddress": contract_address,
                    "page": page,
                    "offset": HOLDER_PAGE_SIZE,
                    "apikey": api_key,
                }
            )
        except requests.RequestException:
            logging.error("Échec Etherscan holders sur page %s", page)
            break

        result = data.get("result")
        if data.get("status") != "1" or not isinstance(result, list) or not result:
            break
        frame = pd.DataFrame(result)
        required = {"TokenHolderAddress", "TokenHolderQuantity"}
        if not required.issubset(frame.columns):
            logging.warning("Structure Etherscan holders inattendue sur page %s", page)
            break
        try:
            frame["TokenHolderQuantity"] = frame["TokenHolderQuantity"].map(
                lambda value: Decimal(str(value)) / factor
            )
        except (InvalidOperation, TypeError, ValueError):
            logging.exception("Quantité holder invalide sur page %s", page)
            break
        pages.append(frame)
        if len(frame) < HOLDER_PAGE_SIZE:
            break
    return pages, explored


def _clean_holder_frame(frame: pd.DataFrame) -> pd.DataFrame:
    addresses = frame["TokenHolderAddress"].astype(str)
    lowered = addresses.str.lower()
    mask = ~lowered.isin(BURN_ADDRESSES) & ~lowered.str.contains(r"dead|null", regex=True, na=False)
    cleaned = frame.loc[mask].copy()
    return cleaned[cleaned["TokenHolderQuantity"] > 0]


def fetch_holders(
    tokens: dict[str, dict[str, Any]],
    ticker: str,
    contract_addresses: list[str],
    api_key: str,
) -> tuple[pd.DataFrame, pd.DataFrame, bool]:
    """Agrège les holders et calcule les métriques synthétiques d'un token."""
    decimals_value = tokens.get(ticker, {}).get("decimals")
    if decimals_value is None or pd.isna(decimals_value):
        raise ValueError(f"Decimals absentes pour {ticker}")
    decimals = int(decimals_value)
    factor = Decimal(10) ** decimals

    frames: list[pd.DataFrame] = []
    raw_total_supply = Decimal(0)
    known_total_supply = False
    explored_pages = 0

    for contract_address in contract_addresses:
        supply = get_total_supply(ticker, contract_address, api_key)
        if supply is not None:
            raw_total_supply += supply
            known_total_supply = True

        pages, explored = _holder_pages(contract_address, api_key, factor)
        explored_pages += explored
        if not pages:
            continue
        contract_frame = _clean_holder_frame(pd.concat(pages, ignore_index=True))
        if contract_frame.empty:
            continue
        contract_frame["token"] = ticker
        contract_frame["Contract Address"] = contract_address
        contract_frame["datetime"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        contract_frame["network"] = "ERC-20"
        frames.append(contract_frame)

    if not frames:
        return pd.DataFrame(), pd.DataFrame(), True

    holders = pd.concat(frames, ignore_index=True)
    holder_supply = sum(holders["TokenHolderQuantity"], Decimal(0))
    total_supply = raw_total_supply / factor if known_total_supply else None
    total_quantity = holder_supply
    if total_quantity <= 0:
        return pd.DataFrame(), holders, True

    holders["percentage"] = holders["TokenHolderQuantity"].map(
        lambda value: (value / total_quantity) * Decimal(100)
    )
    top = holders[holders["percentage"].map(lambda value: value >= Decimal(1))]
    significant_quantity = sum(top["TokenHolderQuantity"], Decimal(0))
    significant_percentage = (
        (significant_quantity / total_quantity) * Decimal(100) if significant_quantity else Decimal(0)
    )

    anomaly = False
    diff_absolute: Decimal | None = None
    diff_percentage: Decimal | None = None
    if total_supply is not None and total_supply > 0:
        diff_absolute = total_supply - holder_supply
        diff_percentage = (diff_absolute / total_supply) * Decimal(100)
        if abs(diff_absolute) > total_supply * Decimal("0.01"):
            anomaly = True
            logging.warning("Écart supérieur à 1%% entre supply holders et total supply pour %s", ticker)
        if holder_supply > total_supply * Decimal("1.5"):
            anomaly = True
            logging.warning("Supply holders supérieur à 150%% du total supply pour %s", ticker)

    summary = pd.DataFrame(
        [{
            "wallet": "SUM_HOLDERS_>=1%",
            "quantity": significant_quantity,
            "percentage": significant_percentage,
            "symbol": ticker,
            "Contract Address": ", ".join(contract_addresses),
            "datetime": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "network": "ERC-20",
            "total_token_holders": int(len(holders)),
            "holders_total_supply": holder_supply,
            "total_supply": total_supply,
            "diff_absolute": diff_absolute,
            "diff_percentage": diff_percentage,
            "decimals": decimals,
            "explored_pages": explored_pages,
        }]
    )
    return summary, holders, anomaly


def post_wallet_holders(frame: pd.DataFrame, api_url: str, api_key: str) -> dict[str, Any]:
    """Publie les métriques ERC-20 en ne transmettant que le schéma attendu."""
    required = [
        "ticker",
        "date",
        "circulating_supply",
        "total_supply",
        "unique_holders",
        "decimals",
        "significant_holders_quantity",
        "significant_holders_perc",
        "network",
        "contract_address",
    ]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"Colonnes manquantes dans le payload wallet holders : {missing}")

    payload_frame = frame[required].copy()
    payload_frame["date"] = pd.to_datetime(payload_frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
    payload_frame = payload_frame.where(pd.notna(payload_frame), None)
    payload = {"data": payload_frame.to_dict(orient="records")}
    headers = {"Content-Type": "application/json", ARIA_WALLET_API_KEY_HEADER: api_key}

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = safe_post(api_url, json=payload, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {"status": "ok"}
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logging.warning("Publication wallet holders en échec, tentative %s/3", attempt)
            if attempt < 3:
                time.sleep(30)
    raise RuntimeError("Échec de publication des wallet holders") from last_error
