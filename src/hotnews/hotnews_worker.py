"""Consolidation des Hot News détectées par le Newsblotter."""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from src.config import (
    APP_ROOT,
    DATA_MAIN_DIR,
    HOTNEWS_DIR,
    NEWS_API_PATH,
    WORDLIST_API_PATH,
    api_url,
    require_env,
    validate_api_base_url,
)
from src.io_utils import sanitize_dataframe_for_excel
from src.hotnews.hotnews_methods import (
    analyser_et_mettre_a_jour_similarites,
    fetch_and_prepare_data,
    final_data_ARIA_to_send,
    find_all_tokens_from_titles,
    generate_resum_df,
    post_hotnews,
    prepare_final_df_for_api,
    mark_llm_summary,
    update_hotnews,
    read_data_between_dates,
    read_db_tokens_tags,
)

logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)


def main() -> None:
    os.umask(0o077)
    validate_api_base_url()
    aria_api_key = require_env("ARIA_API_KEY")
    require_env("OPENAI_API_KEY")

    generated_dir = HOTNEWS_DIR / "generated"
    compiled_dir = HOTNEWS_DIR / "compil_similar_TXT"
    audit_dir = HOTNEWS_DIR / "archives_df_llm_build"
    for directory in (generated_dir, compiled_dir, audit_dir):
        directory.mkdir(parents=True, exist_ok=True)

    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    output_df = audit_dir / f"hotnews_llm_{current_time}.xlsx"
    similarity_df = audit_dir / f"hotnews_similarity_{current_time}.xlsx"
    crypto_list = DATA_MAIN_DIR / "updated_crypto_list_files" / "list_crypto_acronymes_updated.xlsx"

    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    source_df = read_data_between_dates(api_url(NEWS_API_PATH), start_date, end_date, aria_api_key)
    prepared_df = fetch_and_prepare_data(source_df, hour_limit=12)

    token_df = read_db_tokens_tags(api_url(WORDLIST_API_PATH), aria_api_key)
    token_df = token_df[token_df["category"] != "Action"].reset_index(drop=True)
    tokenized_df = find_all_tokens_from_titles(prepared_df, token_df)
    result_df = analyser_et_mettre_a_jour_similarites(tokenized_df)

    # Les fonctions héritées attendent des chemins relatifs à /app.
    relative_compiled = str(compiled_dir.relative_to(APP_ROOT))
    relative_generated = str(generated_dir.relative_to(APP_ROOT))
    summaries_df, _processed_ids = generate_resum_df(
        result_df,
        str(APP_ROOT),
        relative_compiled,
        relative_generated,
    )

    if summaries_df.empty:
        logger.info("Aucune Hot News à consolider")
        return

    final_df = final_data_ARIA_to_send(summaries_df, str(crypto_list))
    final_df = prepare_final_df_for_api(final_df)
    final_df["utc_datetime_news"] = pd.to_datetime(final_df["utc_datetime_news"], errors="coerce").dt.tz_localize(None)

    # Artefacts runtime utiles au diagnostic, exclus du dépôt Git.
    sanitize_dataframe_for_excel(final_df).to_excel(output_df, index=False)
    sanitize_dataframe_for_excel(result_df).to_excel(similarity_df, index=False)

    for _, row in final_df.iterrows():
        if pd.notna(row.get("update_id")) and int(row["update_id"]) > 0:
            update_hotnews(row, aria_api_key)
        else:
            post_hotnews(row, aria_api_key)

        source_ids = row.get("source_ids", [])
        if not isinstance(source_ids, (list, tuple, set)):
            source_ids = []
        for news_id in source_ids:
            mark_llm_summary(int(news_id), True, aria_api_key)

    logger.info("Consolidation Hot News terminée : %s synthèse(s)", len(final_df))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Échec fatal de la consolidation Hot News")
        raise SystemExit(1)
