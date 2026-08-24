# -*- coding: utf-8 -*-
"""

"""

import numpy as np
import pandas as pd
import re
from rapidfuzz import fuzz
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import requests
from src.http_safety import safe_get, validate_https_url
from src.io_utils import atomic_write_text, resolve_within
from src.newsblotter.parsing import parse_rss_xml
from src.config import ARIA_API_KEY_HEADER, HOTNEWS_EXCLUDED_SOURCE_TOKENS, SOURCE_DOMAIN_ALIASES
from bs4 import BeautifulSoup
import html
import os
import logging
from pathlib import Path
import traceback
import sys

# Configuration minimale pour rediriger les logs vers stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',  # Supprime l'ajout de la date-heure locale
    handlers=[logging.StreamHandler(sys.stdout)]
)
# --------------------------------------------------------------------------------------------------------
# Construction du DataFrame de référence à partir de l’API applicative
# --------------------------------------------------------------------------------------------------------

def create_final_df_news(url_base, API_KEY):
    # Calcul des dates pour les paramètres de l'API
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")

    
    # Récupération des données via l'API
    df_read_data_v2 = read_data_between_dates(url_base, start_date, end_date, API_KEY)
    
    # Éliminer la colonne 'full_text' dès le début
    if 'full_text' in df_read_data_v2.columns:
        df_read_data_v2 = df_read_data_v2.drop(columns=['full_text'])
    
    # Initialisation des colonnes supplémentaires à None pour garantir le bon typage
    df_read_data_v2['TOT_cat_words'] = pd.Series([None] * len(df_read_data_v2), dtype='Int64')
    df_read_data_v2['cat_tokenomics'] = pd.Series([None] * len(df_read_data_v2), dtype='float64')
    df_read_data_v2['cat_risks'] = pd.Series([None] * len(df_read_data_v2), dtype='float64')
    df_read_data_v2['cat_adoption'] = pd.Series([None] * len(df_read_data_v2), dtype='float64')
    df_read_data_v2['cat_governance'] = pd.Series([None] * len(df_read_data_v2), dtype='float64')
    df_read_data_v2['cat_investment_analytics'] = pd.Series([None] * len(df_read_data_v2), dtype='float64')
    df_read_data_v2['Actor'] = pd.Series([None] * len(df_read_data_v2), dtype='Int64')
    df_read_data_v2['TradFi'] = pd.Series([None] * len(df_read_data_v2), dtype='Int64')
    df_read_data_v2['Action'] = pd.Series([None] * len(df_read_data_v2), dtype='Int64')
    df_read_data_v2['Crypto'] = pd.Series([None] * len(df_read_data_v2), dtype='Int64')
    
    # Renommer et réorganiser les colonnes selon les spécifications
    final_columns_mapping = {
        'id': 'id_news',
        'date': 'UTC_datetime_news',
        'source': 'source_domain',
        'title': 'titles',
        'summary': 'resum_news',
        'related_cryptos': 'list_cryptos',
        'is_hot': 'hot_news',
        'llm_resumed': 'llm_resum'
    }
    
    # Renommer les colonnes existantes
    df_final_news = df_read_data_v2.rename(columns=final_columns_mapping)
    
    # Conversion de la colonne 'UTC_datetime_news' en format datetime sans le fuseau horaire
    df_final_news['UTC_datetime_news'] = pd.to_datetime(df_final_news['UTC_datetime_news'], errors='coerce').dt.strftime('%Y-%m-%d %H:%M:%S')
    
    # Convertir chaque liste dans 'list_cryptos' en chaîne formatée avec ', ' comme séparateur
    df_final_news['list_cryptos'] = df_final_news['list_cryptos'].apply(lambda x: ', '.join(x) if isinstance(x, list) else x)
    
    # Ajouter la colonne 'path_TEXT_files' avec des valeurs vides pour l'instant
    df_final_news['path_TEXT_files'] = ""
    
    # Sélection et ordre des colonnes finales
    final_column_order = [
        'id_news', 'titles', 'resum_news', 'UTC_datetime_news', 'source_domain', 'list_cryptos', 'path_TEXT_files',
        'TOT_cat_words', 'cat_tokenomics', 'cat_risks', 'cat_adoption', 'cat_governance', 'cat_investment_analytics', 
        'Actor', 'TradFi', 'Action', 'Crypto', 'hot_news', 'llm_resum'
    ]
    
    df_final_news = df_final_news[final_column_order]
    
    return df_final_news


def read_data_between_dates(url_base: str, start_date: str, end_date: str, API_KEY: str) -> pd.DataFrame:
    """
    Récupère et nettoie les données des news depuis une API pour une catégorie donnée et entre deux dates.
    
    Parameters:
    - url_base (str): L'URL de base de l'API pour récupérer les données des news.
    - start_date (str): Date de début pour le filtrage au format "YYYY-MM-DD".
    - end_date (str): Date de fin pour le filtrage au format "YYYY-MM-DD".
    - API_KEY (str): Clé API pour l'authentification.
    
    Returns:
    - pd.DataFrame: DataFrame contenant les données nettoyées et filtrées.
    """
    # Construire l'URL avec les paramètres de date et de catégorie
    url = f"{url_base}?startDate={start_date}&endDate={end_date}"
    
    headers = {
        "Content-Type": "application/json",
        ARIA_API_KEY_HEADER: API_KEY
    }
    
    try:
        # Effectuer la requête GET avec l'URL complète et les en-têtes
        response = safe_get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # Vérifier si la requête a réussi
        if response.status_code == 200:
            raw_data = response.json()
        else:
            logging.info(f"Erreur: La requête API a échoué avec le code {response.status_code}")
            return pd.DataFrame()  # Retourne un DataFrame vide en cas d'échec

        # Extraire et nettoyer les données
        clean_data = raw_data.get('data', [])
        df = pd.DataFrame(clean_data)
        
        # Conversion de la colonne 'utc_datetime_news' en datetime
        if 'utc_datetime_news' in df.columns:
            df['utc_datetime_news'] = pd.to_datetime(df['utc_datetime_news'], errors='coerce')
            df = df.sort_values(by='utc_datetime_news', ascending=False)
            df['utc_datetime_news'] = df['utc_datetime_news'].dt.tz_localize(None)

        return df

    except requests.RequestException:
        logging.error("Erreur réseau lors de la récupération des données")
        return pd.DataFrame()
    except ValueError as e:
        logging.error(f"Erreur lors de la conversion des données : {e}")
        return pd.DataFrame()
    except Exception as e:
        logging.error(f"Erreur inattendue lors de la récupération des données : {e}")
        return pd.DataFrame()  # Retourne un DataFrame vide en cas d'exception


def read_db_tokens_tags(url_base: str, aria_crypto_api_key: str) -> pd.DataFrame:
    """
    Récupère les tokens et leurs tags depuis l'API et retourne un DataFrame structuré.
    
    Parameters:
    - url_base (str): L'URL de base de l'API pour récupérer les données des tokens et tags.
    - aria_crypto_api_key (str): Clé API pour l'authentification.
    
    Returns:
    - pd.DataFrame: DataFrame contenant les catégories, mots et tags des tokens.
    """

    
    headers = {
        "Content-Type": "application/json",
        ARIA_API_KEY_HEADER: aria_crypto_api_key
    }
    
    try:
        # Effectuer la requête GET
        response = safe_get(url_base, headers=headers, timeout=10)
        response.raise_for_status()
        
        # Vérifier si la requête a réussi
        if response.status_code == 200:
            raw_data = response.json()
            logging.info("Requête API tokens_tags réussie, données récupérées.")
        else:
            logging.error(f"Erreur: La requête API a échoué avec le code {response.status_code}")
            return pd.DataFrame()  # Retourne un DataFrame vide en cas d'échec

        # Extraire les données
        clean_data = raw_data.get('data', [])
        if not clean_data:
            logging.warning("Aucune donnée disponible dans la réponse JSON.")
            return pd.DataFrame()  # Retourne un DataFrame vide si aucune donnée

        # Convertir les données en DataFrame
        df = pd.json_normalize(clean_data)

        # Nettoyage et structuration
        if 'tags' in df.columns:
            df['tags'] = df['tags'].apply(lambda x: ", ".join(x) if isinstance(x, list) else "")
        
        return df

    except requests.RequestException:
        logging.error("Erreur réseau lors de la récupération des données")
        return pd.DataFrame()
    except ValueError as e:
        logging.error(f"Erreur lors de la conversion des données : {e}")
        return pd.DataFrame()
    except Exception as e:
        logging.error(f"Erreur inattendue lors de la récupération des données : {e}")
        return pd.DataFrame()  # Retourne un DataFrame vide en cas d'exception



# --------------------------------------------------------------------------------------------------------
# définition des classes utiles et méthodes à utiliser
# --------------------------------------------------------------------------------------------------------



class News:
    def __init__(
        self,
        id_news,
        last_build_date,
        datetime_news,
        GMT_news,
        UTC_datetime_news,
        link_RSS_url,
        real_links,
        source_domain,
        titles,
        resum_news,
        path_TEXT_files,
        list_cryptos,
        TOT_cat_words,
        cat_tokenomics,
        cat_risks,
        cat_adoption,
        cat_governance,
        cat_investment_analytics,
        Actor,
        TradFi,
        Action,
        Crypto,
        hot_news=False,
        llm_resum=False,
        read_count=0
    ):
        self.id_news = id_news
        self.last_build_date = last_build_date
        self.datetime_news = datetime_news
        self.GMT_news = GMT_news
        self.UTC_datetime_news = UTC_datetime_news
        self.link_RSS_url = link_RSS_url
        self.real_links = real_links
        self.source_domain = source_domain
        self.titles = titles
        self.resum_news = resum_news
        self.path_TEXT_files = path_TEXT_files
        self.list_cryptos = list_cryptos
        self.TOT_cat_words = TOT_cat_words
        self.cat_tokenomics = cat_tokenomics
        self.cat_risks = cat_risks
        self.cat_adoption = cat_adoption
        self.cat_governance = cat_governance
        self.cat_investment_analytics = cat_investment_analytics
        self.Actor = Actor
        self.TradFi = TradFi
        self.Action = Action
        self.Crypto = Crypto
        self.hot_news = hot_news
        self.llm_resum = llm_resum
        self.read_count = read_count

    def to_dict(self):
        # Retourne un dictionnaire des attributs de l'instance
        return {
            "id_news": self.id_news,
            "last_build_date": self.last_build_date,
            "datetime_news": self.datetime_news,
            "GMT_news": self.GMT_news,
            "UTC_datetime_news": self.UTC_datetime_news,
            "link_RSS_url": self.link_RSS_url,
            "real_links": self.real_links,
            "source_domain": self.source_domain,
            "titles": self.titles,
            "resum_news": self.resum_news,
            "path_TEXT_files": self.path_TEXT_files,
            "list_cryptos": self.list_cryptos,
            "TOT_cat_words": self.TOT_cat_words,
            "cat_tokenomics": self.cat_tokenomics,
            "cat_risks": self.cat_risks,
            "cat_adoption": self.cat_adoption,
            "cat_governance": self.cat_governance,
            "cat_investment_analytics": self.cat_investment_analytics,
            "Actor": self.Actor,
            "TradFi": self.TradFi,
            "Action": self.Action,
            "Crypto": self.Crypto,
            "hot_news": self.hot_news,
            "llm_resum": self.llm_resum,
            "read_count": self.read_count
        }





# analyse la date avec plusieurs formats. Si aucun format ne fonctionne,
# elle retourne None
def try_parsing_date(text: str) -> datetime:
    """
    DESCRIP : Analyse la date avec plusieurs formats.
              Exemples de formats : Wed, 08 Nov 2023 21:00:00, Wed, 08 Nov 2023 21:00:00 +0530, Wed, 08 Nov 2023 21:00:00 GMT.
              Transforme en un format datetime SANS le GMT.
              Si aucun format ne fonctionne, elle retourne None.
    INPUT   <text> : str : Date au format texte récupérée en scrapant un flux RSS ou URL.
    OUTPUT  <datetime> : datetime : Date et heure au format datetime ou bien None.
    """
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S",
        "%a, %d %b %Y %H:%M:%S GMT",
    ):
        try:
            if fmt.endswith("GMT") and text.endswith("GMT"):
                # Correct the timezone attribute reference
                return datetime.strptime(text[:-4], "%a, %d %b %Y %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            else:
                return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None  # retourne None si tous les formats échouent



def format_timezone(timezone_str: str) -> str:
    """
    DESCRIP : Conversion du fuseau horaire GMT en format hh:mm.
    INPUT   <timezone_str> : str : Chaîne de caractère du GMT récupéré sur la page web.
    OUTPUT  <formatted_timezone> : str : <+hh:mm> ou <-hh:mm> ou <''> chaine de caractères par exemple "+05:30"
    """
    if not timezone_str:
        return ""
    sign = "+" if int(timezone_str) >= 0 else "-"
    hh_mm = f"{abs(int(timezone_str)) // 100:02d}:{abs(int(timezone_str)) % 100:02d}"
    return f"{sign}{hh_mm}"



def extract_domain_from_url(url):
    """
    DESCRIP : vérifie qu'une entrée est de type URL
    INPUT   <url> chaine de caractères
    OUTPUT  <parsed_url> chaine de caractères de type URL analysée
    """
    parsed_url = urlparse(url)
    return parsed_url.netloc




def convert_to_utc(date_converted: datetime, gmt_value: str) -> datetime:
    """
    Convertit une date donnée et son décalage GMT en heure UTC.
    
    Exemple :
        date_converted = datetime.strptime("2024-05-29 12:29:49", "%Y-%m-%d %H:%M:%S")
        gmt_value = "+01:00"
        => utc_datetime = 29/05/2024 11:29:49 (1 heure en moins)
    
    Args:
        date_converted (datetime): Date et heure à convertir.
        gmt_value (str): Décalage GMT sous forme de chaîne (ex: "+01:00").
    
    Returns:
        datetime: Date et heure converties en UTC.
    
    Raises:
        ValueError: Si `gmt_value` est mal formaté.
    """
    try:
        # Validation du format GMT
        if not (gmt_value.startswith(('+', '-')) and len(gmt_value) == 6 and gmt_value[3] == ':'):
            raise ValueError("Le format du gmt_value doit être '+HH:MM' ou '-HH:MM'.")

        # Extraction des heures et minutes
        gmt_hours = int(gmt_value[1:3])
        gmt_minutes = int(gmt_value[4:])

        # Création du timedelta
        gmt_timedelta = timedelta(hours=gmt_hours, minutes=gmt_minutes)

        # Application du décalage
        if gmt_value[0] == "+":
            utc_datetime = date_converted - gmt_timedelta
        else:
            utc_datetime = date_converted + gmt_timedelta

        return utc_datetime

    except Exception as e:
        raise ValueError(f"Erreur dans la conversion : {e}")





def is_similar(title: str, df: pd.DataFrame, threshold: int = 90) -> bool:
    """
    DESCRIPTION : Vérifier si un titre scrapé est présent à 90% dans les 10 000 derniers titres de df ( = FINAL_df_news).
    Ce 90% correspond à 90% des lettres ordonnées pareil entre le titre et un titre de df
    car pas mal de correction par update dans les RSS sur les titre avec ajout d'un point, espace ou petit mot rectificatif.
    INPUT   <title> : str : Titre à vérifier.
            <df> : pd.DataFrame : DataFrame contenant les titres existants.
            <threshold> : int : Seuil de similarité (par défaut 90).
    OUTPUT  <is_similar> : bool : True si title similaire à 90% d'un des 10 000 titres de df, False sinon.
    """
    # Vérifier que le titre fourni est bien une chaîne de caractères
    if not isinstance(title, str) or title.strip() == "":
        logging.info(f"Erreur: Le titre fourni est vide ou de type {type(title)}.")
        return False
    
    # Parcourir les 10 000 dernières lignes de la colonne "titles"
    for index, existing_title in df["titles"].tail(10000).items():
        # Vérifier que chaque titre existant est une chaîne de caractères
        if not isinstance(existing_title, str):
            (f"Erreur: Un titre dans FINAL_df_news à l'index {index} n'est pas une chaîne de caractères, mais {type(existing_title)}.")
            continue  # Passer au titre suivant si le type n'est pas compatible
        
        # Calculer le pourcentage de similitude
        similarity = fuzz.ratio(title, existing_title)
        
        # Vérifier si la similitude est supérieure ou égale au seuil
        if similarity >= threshold:
            return True
    
    return False



def generate_regex_pattern(keyword: str) -> (str, int):
    """
    Génère un motif regex pour un mot-clé donné.
    - Sensible à la casse pour les tokens tout en majuscules (ex. "SEC", "(SEC)").
    - Insensible à la casse pour les autres tokens (simples ou composés).
    """
    if keyword.isupper():  # Tokens tout en majuscules avec éventuellement des points ou parenthèses
        regex_pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
        flags = 0  # Sensible à la casse
    else:  # Tokens simples ou composés (insensibles à la casse)
        regex_pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
        flags = re.IGNORECASE
    return regex_pattern, flags



def categorize_1_news(path_TEXT_files: str, df_categories: pd.DataFrame) -> (list, int):
    """
    DESCRIP : Catégoriser les mots-clés présents dans un fichier texte en fonction des catégories données.
    INPUT   <path_TEXT_files> : str : Chemin du fichier texte à analyser.
            <df_categories> : pd.DataFrame : DataFrame contenant les catégories et leurs mots-clés.
    OUTPUT  <percent_categories> : list : Liste des pourcentages de mots-clés pour chaque catégorie.
            <total_keywords> : int : Nombre total de mots-clés trouvés.
    """
    # Lire le contenu du fichier texte
    with open(path_TEXT_files, "r", encoding="utf-8") as file:
        content = file.read()

    # Initialiser le compteur de mots-clés pour chaque catégorie
    total_count_key_words = []
    # autre initialisation utile
    percent_categories = []

    try:
        for col in df_categories.columns:
            # compter pour chaque colonne le nombre de mots clés présents dans le fichier texte considéré
            count = 0
            # éliminer les cellules vides de la colonne prise car regex sur ces cellules (sinon error)
            for keyword in df_categories[col].dropna():
                # Si le mot est en majuscules (avec ou sans parenthèses)
                regex_pattern, flags = generate_regex_pattern(keyword)
                try:
                    count += len(re.findall(regex_pattern, content, flags=flags))
                except TypeError as e:
                    logging.info(f"Erreur avec le mot-clé '{keyword}': {e}")
            total_count_key_words.append(count)
    except Exception as e:
        logging.error(f"Erreur inattendue sur la fonction categorize_1_news() : {e}")

    # Calculer le pourcentage pour chaque catégorie
    total_keywords = sum(total_count_key_words)
    if total_keywords == 0:  # Pour éviter la division par zéro
        percent_categories = [0 for _ in total_count_key_words]
    else:
        percent_categories = [
            round(count / total_keywords * 100, 2) for count in total_count_key_words
        ]

    return percent_categories, total_keywords



def prepare_tokens_dataframe(df_tokens_tags: pd.DataFrame) -> pd.DataFrame:
    """
    Prépare un DataFrame optimisé contenant les tokens organisés par catégorie, en remplaçant les cellules vides par NaN.
    
    INPUT
        <df_tokens_tags>: pd.DataFrame : DataFrame contenant les catégories, mots-clés et tags.
    
    OUTPUT
        <tokens_df>: pd.DataFrame : DataFrame structuré avec les catégories comme colonnes, et NaN pour les valeurs vides.
    """
    tokens_data = {}

    for category in df_tokens_tags['category'].unique():
        # Filtrer les tokens pour cette catégorie
        category_tokens = df_tokens_tags[df_tokens_tags['category'] == category]

        # Récupérer les mots et les tags éclatés
        tokens = set(category_tokens['word'].dropna())
        for tag_list in category_tokens['tags'].dropna():
            tokens.update(tag.strip() for tag in tag_list.split(","))

        # Ajouter les tokens pour cette catégorie
        tokens_data[category] = list(tokens)

    # Convertir en DataFrame et gérer les cellules vides
    tokens_df = pd.DataFrame(dict([(k, pd.Series(v)) for k, v in tokens_data.items()]))

    # Remplacer les cellules vides (valeurs None ou "") par NaN
    tokens_df.replace("", np.nan, inplace=True)
    
    # Réorganiser les colonnes dans l'ordre souhaité
    desired_order = ["Actor", "TradFi", "Action", "Crypto"]
    tokens_df = tokens_df.reindex(columns=desired_order)

    return tokens_df



def categorize_1_title(title: str, df_tokens_tags_reshape: pd.DataFrame) -> tuple:
    """
    DESCRIP : Catégoriser les mots-clés présents dans un titre en fonction des catégories données.
    INPUT   <title> : str : Titre à analyser.
            <df_tokens_tags_reshape> : pd.DataFrame : DataFrame contenant les catégories et leurs mots-clés.
    OUTPUT  <total_count_key_words> : list : Liste des compteurs de mots-clés pour chaque catégorie.
            <found_keywords> : list : Liste des mots-clés trouvés pour chaque catégorie.
    """
    total_count_key_words = []
    found_keywords = []  # Pour enregistrer les mots-clés trouvés dans chaque catégorie

    try:
        for col in df_tokens_tags_reshape.columns:
            count = 0
            keywords_found_in_category = []  # Liste des mots-clés trouvés pour cette catégorie

            for keyword in df_tokens_tags_reshape[col].dropna():
                regex_pattern, flags = generate_regex_pattern(keyword)
                try:
                    matches = re.findall(regex_pattern, title, flags=flags)
                    count += len(matches)
                    if matches:
                        keywords_found_in_category.append(keyword)
                except TypeError as e:
                    logging.error(f"Erreur avec le mot-clé '{keyword}': {e}")

            total_count_key_words.append(count)
            found_keywords.append(keywords_found_in_category)

    except Exception as e:
        logging.error(f"Erreur inattendue sur la fonction categorize_1_title() : {e}")

    return total_count_key_words, found_keywords



def find_coin_concerned(file_path: str, df_coin_list: pd.DataFrame) -> list:
    """
    DESCRIP : Trouver les tokens de cryptomonnaies mentionnés dans le fichier texte.
    INPUT   <file_path> : str : Chemin du fichier texte à analyser.
            <df_coin_list> : pd.DataFrame : DataFrame contenant les tokens et noms de cryptomonnaies.
    OUTPUT  <list_coin_found> : list : Liste des tokens de cryptomonnaies trouvés dans le texte.
    """
    # Read the file content
    with open(file_path, "r", encoding="utf-8") as file:
        content = file.read()

    # Utiliser une expression régulière pour trouver tous les mots, même s'ils
    # sont encadrés par () ou []
    words = re.findall(r"\b\w+\b", re.sub("[\\[\\]()]", " ", content))

    # Créer un ensemble vide pour collecter les tokens de cryptos trouvés dans
    # le texte
    list_coin_found = set()

    # Chercher les tokens de cryptos dans le contenu du fichier avec le regex
    # words pour les trouver meme s'ils sont entre ( ) ou [ ]
    list_coin_found.update(
        df_coin_list[df_coin_list["crypto_ticker"].isin(words)][
            "crypto_ticker"
        ].tolist()
    )

    # Chercher les noms de cryptos dans le contenu du fichier en s'assurant
    # qu'ils sont encadrés par des espaces ou des points
    for crypto_name in df_coin_list["crypto_name"]:
        if re.search(r"\b" + re.escape(crypto_name) + r"\b", content):
            corresponding_ticker = df_coin_list[
                df_coin_list["crypto_name"] == crypto_name
            ]["crypto_ticker"].values[0]
            list_coin_found.add(corresponding_ticker)

    return list(list_coin_found)


def determinate_hotnews(actor, tradfi, action, crypto, source_domain, found_keywords):
    # init des argument pour éviter un None parmis elles
    llm_resum = False  # Initialisation à False pour toutes les news
    hot_news = False
    read_count = 0

    source_name = str(source_domain).lower()
    source_is_excluded = any(token in source_name for token in HOTNEWS_EXCLUDED_SOURCE_TOKENS)

    if ((actor > 0 or tradfi > 0) and action > 0 and crypto > 0 and not source_is_excluded):
        hot_news = True

        # Affichage des valeurs et mots-clés associés pour chaque catégorie lorsque hot_news est True
        logging.info("")
        logging.info("Hotnews détectée!")
        logging.info(f"Valeurs - Actor: {actor}, TradFi: {tradfi}, Action: {action}, Crypto: {crypto}")
        
        # Afficher les mots-clés trouvés pour chaque catégorie
        categories = ["Actor", "TradFi", "Action", "Crypto"]
        for i, category_keywords in enumerate(found_keywords):
            logging.info(f"Mots-clés trouvés pour {categories[i]}: {', '.join(category_keywords) if category_keywords else 'Aucun'}")

    return hot_news, llm_resum, read_count


def process_news_updates(df_updated_news: pd.DataFrame, 
                         df_categories: pd.DataFrame, 
                         df_tokens_tags_reshape: pd.DataFrame, 
                         df_coin_list: pd.DataFrame, 
                         my_computer_path: str, 
                         FINAL_df_news: pd.DataFrame, 
                         url: str,
                         api_key: str):
    """
    DESCRIP : Traiter et mettre à jour les nouvelles scrappées, incluant la catégorisation et la détection des cryptos.
    INPUT   <df_updated_news> : pd.DataFrame : DataFrame contenant les nouvelles mises à jour.
            <df_categories> : pd.DataFrame : DataFrame contenant les catégories de mots-clés.
            <df_tokens_tags_reshape> : pd.DataFrame : DataFrame contenant les titres et tags.
            <df_coin_list> : pd.DataFrame : DataFrame contenant la liste des cryptomonnaies.
            <my_computer_path> : str : Chemin de l'ordinateur local pour le projet.
            <FINAL_df_news> : pd.DataFrame : DataFrame contenant les nouvelles finales combinées.
            <url> : str : URL de l'API pour récupérer le dernier ID de news.
    OUTPUT  None : Aucune sortie, met à jour et sauvegarde les nouvelles scrappées.
    """
    # A - Catégorisation des nouvelles news
    # Parcourir chaque ligne du DataFrame df_updated_news
    for index, row in df_updated_news.iterrows():
        # Initialisation précoce pour éviter les valeurs None
        actor = tradfi = action = crypto = 0  # Initialisation des compteurs à zéro
        hot_news = llm_resum = False          # Valeurs booléennes initialement False
        # Vérifier que le chemin du fichier texte est valide et que les catégories n'ont pas déjà été calculées
        if pd.notnull(row["path_TEXT_files"]) and pd.isnull(row["TOT_cat_words"]):
            file_path = str(resolve_within(Path(my_computer_path), str(row["path_TEXT_files"]).lstrip("/")))
            if os.path.exists(file_path):
                # Appliquer la catégorisation sur le contenu du fichier texte
                percentages, total_keywords = categorize_1_news(file_path, df_categories)
                # Mettre à jour les colonnes de catégories dans le DataFrame
                df_updated_news.at[index, "TOT_cat_words"] = total_keywords
                df_updated_news.at[index, "cat_tokenomics"] = percentages[0]
                df_updated_news.at[index, "cat_risks"] = percentages[1]
                df_updated_news.at[index, "cat_adoption"] = percentages[2]
                df_updated_news.at[index, "cat_governance"] = percentages[3]
                df_updated_news.at[index, "cat_investment_analytics"] = percentages[4]
        
        # calculs pour TitlesTags
        if pd.notnull(row["titles"]):
            title = row["titles"]
            total_count_key_words, found_keywords = categorize_1_title(title, df_tokens_tags_reshape)
            actor = total_count_key_words[0] if total_count_key_words[0] is not None else 0
            tradfi = total_count_key_words[1] if total_count_key_words[1] is not None else 0
            action = total_count_key_words[2] if total_count_key_words[2] is not None else 0
            crypto = total_count_key_words[3] if total_count_key_words[3] is not None else 0

            # Mise à jour des valeurs dans le DataFrame
            df_updated_news.at[index, "Actor"] = actor
            df_updated_news.at[index, "TradFi"] = tradfi
            df_updated_news.at[index, "Action"] = action
            df_updated_news.at[index, "Crypto"] = crypto

            # Appel à determinate_hotnews pour vérifier si c'est une hot_news
            source_domain = row['source_domain']
            hot_news, llm_resum, read_count = determinate_hotnews(actor, tradfi, action, crypto, source_domain, found_keywords)
            df_updated_news.at[index, 'hot_news'] = hot_news
            df_updated_news.at[index, 'llm_resum'] = llm_resum
            df_updated_news.at[index, 'read_count'] = read_count

        # Log pour confirmer l'exécution
            
        # Vérifier que la détection des cryptos n'a pas déjà été faite
        if pd.notnull(row["path_TEXT_files"]) and pd.isnull(row["list_cryptos"]):
            file_path = str(resolve_within(Path(my_computer_path), str(row["path_TEXT_files"]).lstrip("/")))
            if os.path.exists(file_path):
                # Appliquer la détection des cryptos sur le contenu du fichier texte
                list_cryptos = find_coin_concerned(file_path, df_coin_list)
                # Mettre à jour la colonne des cryptos dans le DataFrame
                df_updated_news.at[index, "list_cryptos"] = (
                    ", ".join(list_cryptos) if list_cryptos else "no_crypto_detected"
                )
    # B - Récupération du dernier id_news chargé sur le site ARIA pour implémenter les nouveaux id des news récupérées
    response = safe_get(
        url,
        headers={"Accept": "application/json", ARIA_API_KEY_HEADER: api_key},
        timeout=10,
    )
    response.raise_for_status()
    # Vérifier si la requête a réussi
    if response.status_code == 200:
        # Parser le contenu de la réponse en JSON
        data_last_update = response.json()
        last_news_id = data_last_update["id"]
    else:
        logging.info(f"Échec de la requête: status code {response.status_code}")
        return None
    # C - Implémentation des id des news récupérées dans df_updated_news
    id_to_implement = int(last_news_id)
    for i in range(df_updated_news.shape[0]):
        id_to_implement += 1
        df_updated_news.loc[i, "id_news"] = id_to_implement
    # D - Sauvegarde df_updated_news terminé au format JSON pour envoi sur site ARIA ensuite
    folder_path = resolve_within(Path(my_computer_path), "json_rss_to_send/last_RSS_news_found.json")
    json_result = df_updated_news.to_json(orient="records")
    atomic_write_text(folder_path, json_result)
        




def clear_text_from(start_to_clear, full_text):
    """ pour nettoyer tous les full_text scrapé pour éliminer les pubs & commentaires inutiles"""
    # Utilisation de regex pour trouver et supprimer tout ce qui suit start_to_clear
    pattern = re.escape(start_to_clear) + r'.*'
    full_text_result = re.sub(pattern, '', full_text, flags=re.DOTALL)
    return full_text_result


class ClassicSource:
    def __init__(self, URL_concerned, link_RSS_url, last_datetime_sav, sav_path_news, sav_name_news, balise_last_build_date,
                 balise_item, balise_title, balise_link, balise_datetime, balise_text, start_to_clear, my_computer_path, FINAL_df_news):
        # Initialisation des attributs de l'instance RSSSource
        self.URL_concerned = URL_concerned
        self.link_RSS_url = link_RSS_url
        self.last_datetime_sav = last_datetime_sav
        self.sav_path_news = sav_path_news
        self.sav_name_news = sav_name_news
        self.balise_last_build_date = balise_last_build_date
        self.balise_item = balise_item
        self.balise_title = balise_title
        self.balise_link = balise_link
        self.balise_datetime = balise_datetime
        self.balise_text = balise_text
        self.start_to_clear = start_to_clear
        self.my_computer_path = my_computer_path
        self.FINAL_df_news = FINAL_df_news

    def scrap_classic(self) -> list:
        """
        DESCRIP : Scraper les articles d'un flux RSS classique et les convertir en objets News.
        INPUT   Aucune entrée directe, utilise les attributs de l'instance de la classe.
        OUTPUT  <news_items> : list : Liste des url des News extraits du flux RSS.
        """
        # Étape A: Récupérer le lastbuild_datetime du contenu XML du flux RSS
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
    
            response = safe_get(self.link_RSS_url, headers=headers, timeout=10)
            response.raise_for_status()  # Ajout de la gestion des erreurs HTTP
        except requests.RequestException:
            logging.error("Échec de la requête HTTP vers la source RSS")
            return None
        try:
            xml_content = response.text
            soup = parse_rss_xml(xml_content)
        except Exception as e:
            logging.error(f"Erreur lors du parsing XML: {e}")
            return None
        # Étape B: Extraire et formater la date de la dernière mise à jour
        last_build_date_element = self.get_last_build_date_element(soup)
        if not last_build_date_element:
            return None

        last_build_date, last_build_date_str, last_build_GMT_info = self.parse_last_build_date(last_build_date_element)
        # donne en résultat : 29 May 2024 12:29:49 +0000 puis _str 2024-05-29 12:29:49 puis _GMT_info +00:00
        if not last_build_date_str:
            return None

        # Étape C: Vérifier si une mise à jour est nécessaire
        if str(self.last_datetime_sav) == last_build_date_str:
            logging.info("-NON réalisé car aucune MAJ constatée !")
            return None

        # Étape D: Scraper les éléments du flux RSS
        news_items = self.scrape_news_items(soup, last_build_date_str, last_build_GMT_info)
        return news_items

    def get_last_build_date_element(self, soup: BeautifulSoup) -> BeautifulSoup:
        """
        DESCRIP : Retourner la valeur de last_build_date si la balise existe,
                  sinon donne la valeur de datetime.utcnow() si balise = "aucune".
        INPUT   <soup> : BeautifulSoup : Contenu XML parsé du flux RSS.
        OUTPUT  <last_build_date_element> : BeautifulSoup : Élément de la date de dernière mise à jour ou datetime actuel.
        """
        if self.balise_last_build_date == "aucune":
            current_datetime_utc = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
            return BeautifulSoup(f"<dummy>{current_datetime_utc}</dummy>", "html.parser")
        else:
            return soup.find(self.balise_last_build_date)

    def parse_last_build_date(self, last_build_date_element: BeautifulSoup) -> (str, str, str):
        """
        DESCRIP : Analyser la valeur de la balise last_build_date_element et
                  retourner une valeur de last_build_date conforme en séparant la date et le GMT.
        INPUT   <last_build_date_element> : BeautifulSoup : Élément contenant la date de dernière mise à jour.
        OUTPUT  <last_build_date> : str : Date de dernière mise à jour avec GMT.= 29 May 2024 12:29:49 +0000
                <last_build_date_str> : str : Date de dernière mise à jour en format standard.= 2024-05-29 12:29:49
                <last_build_GMT_info> : str : Information sur le décalage horaire GMT.= +00:00
        """
        try:
            last_build_date = last_build_date_element.text
            if last_build_date.endswith("GMT"):
                last_build_date = last_build_date.replace("GMT", "+0000")
            
            last_build_date_dt = datetime.strptime(last_build_date, "%a, %d %b %Y %H:%M:%S %z")
            last_build_date_str = last_build_date_dt.strftime("%Y-%m-%d %H:%M:%S")
            last_build_GMT_info = format_timezone(last_build_date_dt.strftime("%z"))
            return last_build_date, last_build_date_str, last_build_GMT_info
        except (ValueError, AttributeError) as e:
            logging.error(f"Erreur lors du parsing de la date: {e}")
            return None, None, None


    def scrape_news_items(self, soup: BeautifulSoup, last_build_date_str: str, last_build_GMT_info: str) -> list:
        """
        DESCRIP : Scraper les éléments du flux RSS et construire une liste des articles OK en scraping.
                1 item OK si les 6 variables : title, real_link, utc_datetime, file_path_shortcut, full_text_title, max_500_first_char sont OK
        INPUT   <soup> : BeautifulSoup : Contenu XML parsé du flux RSS.
                <last_build_date_str> : str : Date de dernière mise à jour en format standard.= 2024-05-29 12:29:49
                <last_build_GMT_info> : str : Information sur le décalage horaire GMT.= +00:00
        OUTPUT  <news_items> : list : Liste des objets News extraits du flux RSS.
        """
        news_items = []
        items = soup.find_all(self.balise_item)
        source_domain = extract_domain_from_url(self.link_RSS_url)
        source_domain = self.adjust_source_domain(source_domain)

        counter = 1 # pour le nom du fichier txt scrapé par exple : news_coinedition_20240304_140231_7.txt ici counter = 7
        for item in items:
            # initialisations obligatoires des variables utiles pour éviter ces erreurs suite aux sorties de boucles et autres returns
            max_500_first_char = ""
            utc_datetime = None
            real_link = ""
            title = ""
            file_path_shortcut = ""
            title, real_link, utc_datetime, file_path_shortcut, full_text_title, max_500_first_char = self.extract_news_details(item, last_build_GMT_info, counter)
            if not title or not real_link or not utc_datetime or not file_path_shortcut or not full_text_title or not max_500_first_char:
                continue

            news_item = News(
                id_news=None,
                last_build_date=last_build_date_str,
                datetime_news=None,
                GMT_news=None,
                UTC_datetime_news=utc_datetime,
                link_RSS_url=self.link_RSS_url,
                real_links=real_link,
                source_domain=source_domain,
                titles=title,
                resum_news=max_500_first_char,
                path_TEXT_files=file_path_shortcut,
                list_cryptos=None,
                TOT_cat_words=None,
                cat_tokenomics=None,
                cat_risks=None,
                cat_adoption=None,
                cat_governance=None,
                cat_investment_analytics=None,
                Actor=None,
                TradFi=None,
                Action=None,
                Crypto=None,
            )

            news_items.append(news_item)
            logging.info("-news_items créé ok +1 pour titre ci-dessous :")
            logging.info(f"{title} :")
            counter += 1

        return news_items

    def adjust_source_domain(self, source_domain: str) -> str:
        """
        DESCRIP : Ajuster le domaine source pour certains flux RSS spécifiques.
        INPUT   <source_domain> : str : Domaine source extrait de l'URL du flux RSS.
        OUTPUT  <adjusted_source_domain> : str : Domaine source ajusté.
        """
        normalized = str(source_domain).strip().lower()
        return SOURCE_DOMAIN_ALIASES.get(normalized, normalized)



    def extract_news_details(self, item: BeautifulSoup, last_build_GMT_info: str, counter: int) -> (str, str, datetime, str, str, str):
        """
        DESCRIP : Extraire les détails des articles d'1 item du flux RSS
                  selon son last_build_GMT_info et counter pour n° de txt à sauvegarder
        INPUT   <item> : BeautifulSoup : Élément de l'article dans le flux RSS.
                <last_build_GMT_info> : str : Information sur le décalage horaire GMT.
                <counter> : int : Compteur pour numéroter les fichiers textes sauvegardés.
        OUTPUT  <title> : str : Titre de l'article.
                <real_link> : str : Lien réel de l'article.
                <utc_datetime> : datetime : Date et heure de l'article en UTC.
                <file_path_shortcut> : str : Chemin du fichier texte sauvegardé.
                <full_text_title> : str : Texte complet de l'article avec le titre.
                <max_500_first_char> : str : Les 500 premiers caractères de l'article.
        """
        try:
            # scrap du titre
            title = self.get_title(item)
            # si pas de titre ou titre similaire trouvé
            if not title or is_similar(title, self.FINAL_df_news):
                return None, None, None, None, None, None
    
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            file_path_shortcut = f"{str(self.sav_path_news).strip('/')}/{self.sav_name_news}_{timestamp}_{counter}.txt"
            file_path = str(resolve_within(Path(self.my_computer_path), file_path_shortcut))
    
            real_link = self.get_real_link(item)
            pub_date_str, utc_datetime = self.get_datetime_info(item, last_build_GMT_info) # pourquoi last_build_GMT_info et pas l'utc créé ?
    
            full_text_title, max_500_first_char = self.get_full_text(item, title)
            if not full_text_title or not max_500_first_char:
                return None, None, None, None, None, None
    
            self.save_full_text(file_path, full_text_title)
            
            utc_datetime = int(datetime.now(timezone.utc).timestamp() * 1000)  # Timestamp UTC en millisecondes
 
            
            return title, real_link, utc_datetime, file_path_shortcut, full_text_title, max_500_first_char
        
        except Exception as e:
            logging.error(f"Erreur lors de l'extraction des détails de la news: {e}\n{traceback.format_exc()}")
            return None, None, None, None, None, None




    def get_title(self, item: BeautifulSoup) -> str:
        """
        DESCRIP : Extraire le titre d'un article.
        INPUT   <item> : BeautifulSoup : Élément de l'article dans le flux RSS.
        OUTPUT  <title> : str : Titre de l'article ou None si erreur.
        """
        try:
            title_element = item.find(self.balise_title)
            if not hasattr(title_element, "text") or not title_element.text.strip():
                logging.info("Erreur : title est vide ou non-textuel")
                return None
            
            title = title_element.text.strip()
            
            if title.startswith("=") or "nil" in title.lower():
                logging.info(f"Erreur : titre suspect détecté '{title}', ignoré pour éviter un problème de formule.")
                return None
            
            return title
        except AttributeError as e:
            logging.error(f"Erreur lors de l'extraction du titre: {e}")
            return None
        
    def get_real_link(self, item: BeautifulSoup) -> str:
        """
        DESCRIP : Extraire le lien réel d'un article.
        INPUT   <item> : BeautifulSoup : Élément de l'article dans le flux RSS.
        OUTPUT  <real_link> : str : Lien réel de l'article ou None si erreur.
        """
        try:
            real_link_element = item.find(self.balise_link)
            if not hasattr(real_link_element, "text") or not real_link_element.text.strip():
                logging.info("Erreur : link est vide ou non-textuel")
                return None
            return real_link_element.text
        except AttributeError as e:
            logging.error(f"Erreur lors de l'extraction du lien réel: {e}")
            return None

    def get_datetime_info(self, item: BeautifulSoup, last_build_GMT_info: str) -> (str, datetime):
        """
        DESCRIP : Extraire les informations de date et heure d'un article.
        INPUT   <item> : BeautifulSoup : Élément de l'article dans le flux RSS.
                <last_build_GMT_info> : str : Information sur le décalage horaire GMT.
        OUTPUT  <pub_date_str> : str : Date de publication en format standard.
                <utc_datetime> : datetime : Date et heure de l'article en UTC.
        """
        try:
            pub_date_element = item.find(self.balise_datetime)
            if not hasattr(pub_date_element, "text") or not pub_date_element.text.strip():
                logging.info("Erreur : pubDate est vide ou non-textuel")
                return None, None
    
            pub_date = pub_date_element.text
            pub_date_dt = try_parsing_date(pub_date)
            if not pub_date_dt:
                return None, None
    
            pub_date_str = pub_date_dt.strftime("%Y-%m-%d %H:%M:%S")
            utc_datetime = convert_to_utc(pub_date_dt, last_build_GMT_info).replace(tzinfo=None)
            return pub_date_str, utc_datetime
        except Exception as e:
            logging.error(f"Erreur lors de l'extraction de la date et heure: {e}")
            return None, None


    def get_full_text(self, item: BeautifulSoup, title: str) -> (str, str):
        """
        DESCRIP : Extraire le texte complet d'un article.
        INPUT   <item> : BeautifulSoup : Élément de l'article dans le flux RSS.
                <title> : str : Titre de l'article.
        OUTPUT  <full_text_title> : str : Texte complet de l'article avec le titre.
                <max_500_first_char> : str : Les 500 premiers caractères de l'article ou None si erreur.
        """
        try:
            content_encoded = item.find(self.balise_text)
            if not hasattr(content_encoded, "text") or not content_encoded.text.strip():
                logging.info(f"** Erreur : balise '{self.balise_text}' est vide ou non-textuel sur titre {title}")
                return None, None
    
            decoded_content = html.unescape(content_encoded.string)
            content_soup = BeautifulSoup(decoded_content, "html.parser")
            full_text = content_soup.get_text()
            if not full_text:
                return None, None
            
            if self.start_to_clear:
                full_text_result = clear_text_from(self.start_to_clear, full_text)
            else:
                full_text_result = full_text  # Si start_to_clear est vide, on garde le texte tel quel
            
            full_text_clean = html.unescape(full_text_result)
            full_text_title = title + "\n" + "\n" + full_text_clean
            max_500_first_char = full_text_clean[:495] + "[...]"
            if max_500_first_char == "[...]" or max_500_first_char == " [...]":
                return None, None
    
            return full_text_title, max_500_first_char
        except Exception as e:
            logging.error(f"Erreur lors de l'extraction du texte complet: {e}")
            return None, None


    def save_full_text(self, file_path: str, full_text_title: str):
        """
        DESCRIP : Sauvegarder le texte complet d'un article dans un fichier.
        INPUT   <file_path> : str : Chemin du fichier texte à sauvegarder.
                <full_text_title> : str : Texte complet de l'article avec le titre.
        OUTPUT  None : Aucune sortie, sauvegarde le fichier texte.
        """
        try:
            folder_path = os.path.dirname(file_path)
            if not os.path.exists(folder_path):
                os.makedirs(folder_path)
            with open(file_path, "w", encoding="utf-8") as file:
                file.write(full_text_title)
        except IOError as e:
            logging.error(f"Erreur lors de la sauvegarde du texte complet: {e}")


    def save_to_df(self, news_items: list) -> pd.DataFrame:
        """
        DESCRIP : Convertir une liste d'objets News en DataFrame.
        INPUT   <news_items> : list : Liste des objets News.
        OUTPUT  <df> : pd.DataFrame : DataFrame contenant les données des objets News.
        """
        try:
            # Convertit la liste des instances de News en une liste de dictionnaires
            news_dicts = [news_item.to_dict() for news_item in news_items]
    
            # Convertit la liste de dictionnaires en DataFrame
            df = pd.DataFrame(news_dicts)
            return df
        except Exception as e:
            logging.error(f"Erreur lors de la conversion en DataFrame : {e}")
            return None


    def update_last_datetime_sav(self, news_items: list, df_source_RSS_balises: pd.DataFrame, path_to_excel_RSS_source: str):
        """
        DESCRIP : Mettre à jour la date et heure de la dernière sauvegarde pour une source RSS.
        INPUT   <news_items> : list : Liste des objets News.
                <df_source_RSS_balises> : pd.DataFrame : DataFrame des sources RSS.
                <path_to_excel_RSS_source> : str : Chemin du fichier Excel des sources RSS.
        OUTPUT  None : Aucune sortie, met à jour le fichier Excel des sources RSS.
        """
        try:
            if news_items:
                last_build_date = news_items[-1].last_build_date
                index = df_source_RSS_balises.index[df_source_RSS_balises["link_RSS_url"] == self.link_RSS_url].tolist()
                if index:
                    df_source_RSS_balises.at[index[0], "last_datetime_sav"] = last_build_date
                    df_source_RSS_balises.to_excel(path_to_excel_RSS_source, index=False)
                else:
                    logging.info("Aucune correspondance trouvée pour le domaine source %s.", urlparse(self.URL_concerned).hostname)
            else:
                logging.info("Aucune news à utiliser pour la mise à jour du domaine %s.", urlparse(self.URL_concerned).hostname)
        except Exception as e:
            logging.error(f"Erreur lors de la mise à jour de last_datetime_sav: {e}")




