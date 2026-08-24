"""Collecte des sources nécessitant un scraping spécifique ou dynamique."""
from __future__ import annotations

import logging
import sys
from urllib.parse import urlparse
from time import monotonic

import pandas as pd

from src.config import (
    APP_ROOT,
    DATA_MAIN_DIR,
    LATEST_NEWS_API_PATH,
    NEWS_API_PATH,
    WORDLIST_API_PATH,
    api_url,
    require_env,
    validate_api_base_url,
)
from src.newsblotter.rss_special_methods import (
    SpecialSource,
    create_final_df_news,
    prepare_tokens_dataframe,
    process_news_updates,
    read_db_tokens_tags,
)

logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)


def main() -> None:
    started = monotonic()
    validate_api_base_url()
    aria_api_key = require_env("ARIA_API_KEY")
    rss_sources_path = DATA_MAIN_DIR / "RSS_list_url_balises" / "RSS_special_updated.xlsx"
    categories_path = DATA_MAIN_DIR / "updated_categories_files" / "list_TAGS_5_categories_updated.xlsx"
    crypto_list_path = DATA_MAIN_DIR / "updated_crypto_list_files" / "list_crypto_acronymes_updated.xlsx"

    logger.info("Début de la collecte RSS spéciale")
    try:
        df_sources = pd.read_excel(rss_sources_path)
        df_categories = pd.read_excel(categories_path)
        df_coin_list = pd.read_excel(crypto_list_path)
    except Exception:
        logger.exception("Impossible de charger les référentiels nécessaires au RSS spécial")
        raise

    final_df_news = create_final_df_news(api_url(NEWS_API_PATH), aria_api_key)
    df_tokens_tags = read_db_tokens_tags(api_url(WORDLIST_API_PATH), aria_api_key)
    logger.info("Référentiel de mots-clés chargé (%s lignes)", len(df_tokens_tags))
    df_tokens_tags_reshape = prepare_tokens_dataframe(df_tokens_tags)

    df_updated_news: pd.DataFrame | None = None
    source_updates: list[tuple[object, list]] = []
    for index in range(df_sources.shape[0]):
        source = SpecialSource(
            df_sources["exception"][index],
            df_sources["URL_concerned"][index],
            df_sources["link_RSS_url"][index],
            df_sources["last_datetime_sav"][index],
            df_sources["sav_path_news"][index],
            df_sources["sav_name_news"][index],
            df_sources["balise_last_build_date"][index],
            df_sources["balise_item"][index],
            df_sources["balise_title"][index],
            df_sources["balise_link"][index],
            df_sources["balise_datetime"][index],
            df_sources["balise_text"][index],
            df_sources["start_to_clear"][index],
            df_sources["type_stat_dyn"][index],
            str(APP_ROOT),
            final_df_news,
        )
        logger.info("Analyse du domaine source %s", urlparse(source.link_RSS_url).hostname)
        try:
            news_items = source.scrap_special()
            if not news_items:
                continue
            df_news = source.save_to_df(news_items)
            source_updates.append((source, news_items))
            df_updated_news = pd.concat([df_updated_news, df_news], ignore_index=True, sort=False)
        except Exception:
            logger.exception("Échec du scraping d'une source RSS spéciale")

    if df_updated_news is not None:
        df_updated_news = df_updated_news[
            df_updated_news["titles"].apply(lambda value: isinstance(value, str) and value.strip() != "")
        ]
        process_news_updates(
            df_updated_news,
            df_categories,
            df_tokens_tags_reshape,
            df_coin_list,
            str(APP_ROOT),
            final_df_news,
            api_url(LATEST_NEWS_API_PATH),
            aria_api_key,
        )
        for source, news_items in source_updates:
            source.update_last_datetime_sav(news_items, df_sources, str(rss_sources_path))
    else:
        logger.info("Aucune nouvelle entrée RSS spéciale")

    logger.info("Collecte RSS spéciale terminée en %.2f s", monotonic() - started)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Échec fatal du worker RSS spécial")
        raise SystemExit(1)
