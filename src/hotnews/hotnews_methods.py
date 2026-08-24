"""Méthodes de regroupement et de consolidation des Hot News.

Le code conserve les règles métier historiques utiles au portfolio, tandis que
les accès réseau, secrets et prompts sont externalisés pour la version publique.
"""
import hashlib
import json
import requests
from src.http_safety import safe_get, safe_post, safe_put
from src.config import (
    ADD_NEWS_API_PATH,
    ARIA_API_KEY_HEADER,
    LATEST_NEWS_API_PATH,
    OPENAI_MODEL,
    PLATFORM_SOURCE_DOMAIN,
    UPDATE_LLM_STATUS_API_PATH,
    UPDATE_NEWS_API_PATH,
    api_url,
    public_news_url,
    require_env,
)
from src.llm import get_openai_client
from src.prompts import build_hotnews_prompt
import os
import shutil
from rapidfuzz import fuzz
import pandas as pd
import re
from datetime import datetime, timedelta, timezone
import time
import logging
import sys
import traceback
from pathlib import Path


# Configuration minimale pour rediriger les logs vers stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',  # Supprime l'ajout de la date-heure locale
    handlers=[logging.StreamHandler(sys.stdout)]
)



def fetch_and_prepare_data(df_read_api2: pd.DataFrame, hour_limit=48) -> pd.DataFrame:
    """
    Nettoie et prépare les données des news à partir d'un DataFrame fourni.
    INPUT   <df_read_api2> : pd.DataFrame : DataFrame contenant les données des news récupérées.
            <hour_limit> : int : Limite en heures pour filtrer les données par rapport à UTC maintenant.
    OUTPUT  <df_complet_start> : pd.DataFrame : DataFrame nettoyé et prêt pour le traitement.
    """
    try:
        df = df_read_api2.copy()
        
        # Renommer et réorganiser les colonnes selon les spécifications
        final_columns_mapping = {
            'id': 'id_news',
            'date': 'utc_datetime_news',
            'source': 'source_domain',
            'title': 'titles',
            'summary': 'resum_news',
            'related_cryptos': 'list_cryptos',
            'is_hot': 'hot_news',
            'llm_resumed': 'llm_resum',
            'external_link' : 'real_links'
        }
        
        # Renommer les colonnes existantes
        df_complet_start = df.rename(columns=final_columns_mapping)
        
        # éliminer la colonne CATEGORIE inutile et en + avec DICO de valeurs clés
        df_complet_start.drop(["categories"], axis = 1, inplace = True)
        
        # Conversion de la colonne 'utc_datetime_news' en format datetime sans le fuseau horaire
        df_complet_start['utc_datetime_news'] = pd.to_datetime(df_complet_start['utc_datetime_news'], errors='coerce').dt.strftime('%Y-%m-%d %H:%M:%S')
        
        # Convertir chaque liste dans 'list_cryptos' en chaîne formatée avec ', ' comme séparateur
        df_complet_start['list_cryptos'] = df_complet_start['list_cryptos'].apply(lambda x: ', '.join(x) if isinstance(x, list) else x)
        
        
        # Initialisation des colonnes nécessaires avec valeurs par défaut
        df_complet_start['title_tokens'] = [[] for _ in range(len(df_complet_start))]
        df_complet_start['list_similar_news'] = [[] for _ in range(len(df_complet_start))]
        df_complet_start['len_list_similar'] = 0
        df_complet_start['update_id'] = None  # init avec None pour chaque ligne
        
        
        # Sélection et ordre des colonnes finales
        final_column_order = [
            'id_news', 'hot_news', 'llm_resum', 'title_tokens', 'list_similar_news', 'len_list_similar', 'update_id',
            'real_links', 'utc_datetime_news','titles', 'full_text', 'resum_news',  'source_domain', 'list_cryptos']
        
        
        df_complet_start = df_complet_start[final_column_order]
        
        
        # Conversion de la colonne 'utc_datetime_news' en datetime
        if 'utc_datetime_news' in df_complet_start.columns:
            df_complet_start['utc_datetime_news'] = pd.to_datetime(df_complet_start['utc_datetime_news'], errors='coerce')
            # Trier le DataFrame par 'utc_datetime_news' décroissant
            df_complet_start = df_complet_start.sort_values(by='utc_datetime_news', ascending=False)
        
            # Enlever le fuseau horaire pour compatibilité avec Excel
            df_complet_start['utc_datetime_news'] = df_complet_start['utc_datetime_news'].dt.tz_localize(None)

        # Filtrer les données pour ne conserver que les news des dernières 'hour_limit' heures
        time_limit = datetime.now(timezone.utc) - timedelta(hours=hour_limit)
        time_limit = time_limit.astimezone(timezone.utc).replace(tzinfo=None)
        
        df_complet_start = df_complet_start[df_complet_start['utc_datetime_news'] >= time_limit]



        return df_complet_start

    except Exception:
        logging.exception("Erreur lors du nettoyage des news")
        raise




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







def find_all_tokens_from_titles(df, df_tokens_tags):
    """
    Remplit chaque valeur de la colonne 'title_tokens' en extrayant les tokens présents dans 'titles'
    pour chaque ligne du DataFrame. Remplace les tokens par leur clé ('word') dans 'df_tokens_tags'.
    Assure l'unicité des tokens dans chaque titre.
    
    INPUT
        <df> : pd.DataFrame : Le DataFrame contenant les titres.
        <df_tokens_tags> : pd.DataFrame : Le DataFrame contenant les mots ('word') et leurs tags associés.
    
    OUTPUT
        <df> : pd.DataFrame : Le DataFrame mis à jour avec la colonne 'title_tokens' remplie.
    """
    df_copy = df.copy()
    df_copy['title_tokens'] = [[] for _ in range(len(df_copy))]  # Initialiser la colonne 'title_tokens'

    # Créer un mapping des tags vers leur clé principale
    token_replacements = {}
    for _, row in df_tokens_tags.iterrows():
        if row['word']:
            token_replacements[row['word'].lower()] = row['word']  # Associer le mot clé à lui-même
        if row['tags']:
            tags = [tag.strip() for tag in row['tags'].split(",") if tag.strip()]
            for tag in tags:
                token_replacements[tag.lower()] = row['word']  # Associer chaque tag à la clé principale

    # Parcourir chaque ligne du DataFrame
    for index, row in df_copy.iterrows():
        title = row['titles']
        title_tokens = []

        # Rechercher chaque token dans le titre
        for token, replacement in token_replacements.items():
            regex_pattern, flags = generate_regex_pattern(token)
            if re.search(regex_pattern, title, flags=flags) and replacement not in title_tokens:
                title_tokens.append(replacement)

        # Assigner la liste unique des tokens à la colonne 'title_tokens'
        df_copy.at[index, 'title_tokens'] = title_tokens

    return df_copy








def mark_llm_resum(df, list_similar_news):
    """
    Marque `llm_resum` à True pour chaque ID dans `list_similar_news`.
    """
    for similar_id in list_similar_news:
        logging.info(f"Marquage de llm_resum=True pour l'ID {similar_id}.")
        df.loc[df["id"] == similar_id, "llm_resum"] = True



def analyser_et_mettre_a_jour_similarites(df):
    """
    Parcourt chaque ligne du DataFrame `df`, identifie les news similaires,
    et met à jour les colonnes `llm_resum`, `list_similar_news`, `len_list_similar`, et `update_id`.

    Conditions et logiques principales :
    
    1. **Condition de traitement** :
       - La fonction ne traite que les lignes où `hot_news=True` et `llm_resum=False`.
       - Si une ligne satisfait cette condition, elle est marquée pour un résumé (`llm_resum=True`),
         et son ID est initialisé dans `list_similar_news`.

    2. **Recherche de similarité** :
       - Pour chaque ligne traitée, la fonction parcourt toutes les autres lignes du DataFrame pour
         rechercher des news ayant au moins 2 tokens communs dans `title_tokens`.

    3. **Mise à jour de la liste des similaires** :
       - Si une news similaire a `hot_news=True`, `llm_resum=True`, et `source_domain=PLATFORM_SOURCE_DOMAIN`, elle est considérée
         comme un candidat potentiel pour `update_id`. L'ID de mise à jour est choisi en fonction de l'ID le plus récent.
       - Si une news similaire a `hot_news=False` et `llm_resum=False`, elle est ajoutée à `list_similar_news`
         et est marquée comme résumée (`llm_resum=True`) pour éviter tout futur traitement.

    4. **Mise à jour des colonnes de la ligne courante** :
       - Après le traitement de toutes les lignes, la fonction met à jour les colonnes suivantes pour la ligne actuelle :
         - `list_similar_news`: la liste des IDs des news similaires trouvées.
         - `len_list_similar`: le nombre d'IDs dans `list_similar_news`.
         - `update_id`: l'ID de la news résumée la plus récente trouvée parmi les similaires ayant `source_domain=PLATFORM_SOURCE_DOMAIN`.

    Cette fonction retourne le DataFrame mis à jour avec les informations de similarité.
    """


    # Vérification de la présence des colonnes nécessaires pour éviter les erreurs
    if 'list_similar_news' not in df.columns:
        df['list_similar_news'] = [[] for _ in range(len(df))]
    if 'len_list_similar' not in df.columns:
        df['len_list_similar'] = 0
    if 'update_id' not in df.columns:
        df['update_id'] = None
    # Ajouter une colonne vide au départ si elle n'existe pas
    if "tokens_for_llm" not in df.columns:
        df["tokens_for_llm"] = None

    # Parcours de chaque ligne par index pour éviter les problèmes de copies
    for index in df.index:
        row = df.loc[index]

        # Vérification si la ligne actuelle est une hot news sans résumé
        if row["hot_news"] == True and row["llm_resum"] == False:
            logging.info(f"Traitement de l'ID {row['id_news']}: hot_news={row['hot_news']}, llm_resum={row['llm_resum']}")
            # Copier la liste `title_tokens` dans `tokens_for_llm`
            df.at[index, "tokens_for_llm"] = row["title_tokens"]
            # Initialisation de la liste des news similaires et marquage de la news courante comme résumée
            list_similar_news = [row["id_news"]]
            df.loc[index, "llm_resum"] = True
            update_id = None

            # Recherche des news similaires dans le reste du DataFrame
            for other_index in df.index:
                if other_index == index:
                    continue

                other_row = df.loc[other_index]

                # Vérification de similarité : au moins 2 tokens similaires
                similar_tokens = set(row["title_tokens"]).intersection(other_row["title_tokens"])
                if len(similar_tokens) >= 2:
                    if other_row["hot_news"] == True and other_row["llm_resum"] == True and other_row["source_domain"] == PLATFORM_SOURCE_DOMAIN:
                        # Sélection de l'ID de mise à jour si nécessaire
                        if update_id is None or other_row["utc_datetime_news"] > df.loc[df["id_news"] == update_id, "utc_datetime_news"].values[0]:
                            update_id = other_row["id_news"]
                            logging.info(f"ID {other_row['id_news']} sélectionné comme update_id pour la ligne {row['id_news']}")

                        if update_id not in list_similar_news:
                            list_similar_news.append(update_id)
                            logging.info(f"Ajout de {update_id} à list_similar_news pour la ligne {row['id_news']}")
                    elif other_row["llm_resum"] == False:
                        # Ajout de l'ID et marquage comme traité
                        list_similar_news.append(other_row["id_news"])
                        df.loc[other_index, "llm_resum"] = True
                        logging.info(f"Ajout de l'ID {other_row['id_news']} à list_similar_news pour la ligne {row['id_news']}")

            # Mise à jour des informations dans le DataFrame pour la ligne actuelle
            df.at[index, "list_similar_news"] = [int(id) for id in list_similar_news]
            df.at[index, "len_list_similar"] = len(list_similar_news)
            df.at[index, "update_id"] = update_id

    return df






def compile_TXT_and_sav(df: pd.DataFrame, index: int, sav_path_compil_TXT: str, my_computer_path: str) -> str:
    """
    Compile les textes complets des news dont les IDs sont dans 'list_similar_news' pour la ligne donnée,
    en utilisant directement le contenu de la colonne `full_text`.
    
    INPUT
        df : pd.DataFrame
            DataFrame contenant les informations sur les news.
        index : int
            Index de la ligne à traiter dans df.
        sav_path_compil_TXT : str
            Chemin où le fichier compilé sera sauvegardé.
        my_computer_path : str
            Chemin principal de l'application, ajouté en début de chemin pour les accès aux fichiers.

    OUTPUT
        compiled_file_path : str
            Chemin complet du fichier compilé généré.
    """
    # Initialisation de l'ID principal
    main_news_id = df.at[index, 'id_news']
    logging.info(f"Traitement de l'ID principal : {main_news_id}")

    try:
        # Extraire la liste des IDs similaires pour la ligne actuelle
        list_similar_news = df.at[index, 'list_similar_news']
        logging.info(f"Liste des news similaires pour l'ID {main_news_id} : {list_similar_news}")

        # Correction : utilisation de Path pour la concaténation correcte du chemin
        compiled_file_name = f"compil_hotnews_and_similarnews_from_id_{main_news_id}.txt"
        compiled_file_path = Path(my_computer_path) / sav_path_compil_TXT.lstrip("/") / compiled_file_name
        logging.info(f"Chemin de sauvegarde prévu pour le fichier compilé : {compiled_file_path}")


        # Créer le répertoire cible s'il n'existe pas
        os.makedirs(os.path.dirname(compiled_file_path), exist_ok=True)
        logging.info(f"Dossier de sauvegarde créé ou déjà existant : {os.path.dirname(compiled_file_path)}")

        # Variable pour accumuler le contenu des textes
        compiled_content = ""

        # Parcourir chaque ID dans la liste des news similaires et extraire `full_text`
        for news_id in list_similar_news:
            try:
                # Récupérer le texte complet directement depuis la colonne `full_text`
                full_text = df.loc[df['id_news'] == news_id, 'full_text'].values[0]
                compiled_content += full_text + "\n-----\n\n"  # Séparateur entre les news
                logging.info(f"Ajouté texte pour l'ID similaire : {news_id}")
            except IndexError:
                logging.warning(f"Aucun texte trouvé pour l'ID {news_id}, vérifiez que l'ID est correct.")

        # Sauvegarder le contenu compilé dans un fichier texte
        with open(compiled_file_path, 'w', encoding='utf-8') as compiled_file:
            compiled_file.write(compiled_content.strip())  # Éliminer le dernier séparateur
        logging.info(f"Fichier compilé sauvegardé avec succès : {compiled_file_path}")

        # Retourner le chemin complet du fichier compilé
        return compiled_file_path

    except Exception:
        logging.exception("Erreur lors de la compilation des textes pour l'ID %s", main_news_id)
        raise





def generate_resum_df(df_result, my_computer_path, sav_path_compil_TXT, sav_path_resume_to_post):
    """
    Génère un DataFrame `df_resum_to_send` contenant les résumés LLM des hot_news non résumées.
    
    Paramètres:
        - df_result : pd.DataFrame : DataFrame contenant les hot_news avec leur statut de similarité.
        - my_computer_path : str : Chemin de base sur l'ordinateur.
        - sav_path_compil_TXT : str : Chemin de sauvegarde pour les fichiers de compilation de texte.
        - sav_path_resume_to_post : str : Chemin de sauvegarde pour les fichiers de résumé à envoyer.
        
    Retour:
        - df_resum_to_send : pd.DataFrame : DataFrame avec les informations des résumés à envoyer.
    
    Exceptions:
        - FileNotFoundError : si un fichier compilé ou de résumé est introuvable.
        - ValueError : si un résumé LLM ne peut pas être généré.
    """
    # Initialisation de df_resum_to_send avec les colonnes nécessaires pour stocker les informations des résumés générés
    df_resum_to_send = pd.DataFrame(columns=["id_working", "id_to_send", "titles", "hot_news", "llm_resum", 
                                             "prepare_txt", "update_id", "list_cryptos", "resum_news", "full_text", 
                                             "utc_datetime_news", "source_domain", "path_text_files", "source_ids"])
    # Initialisation de l'ensemble pour stocker les IDs uniques de `list_similar_news`
    unique_similar_news_ids = set()

    try:
        for index, row in df_result.iterrows():
            # Traiter toutes les lignes Hot News disposant de similarités exploitables
            if row['hot_news'] and row['len_list_similar'] > 0:
                
                # Génère le texte compilé à partir de textes similaires et sauvegarde le fichier
                compiled_TXT_path = compile_TXT_and_sav(df_result, index, sav_path_compil_TXT, my_computer_path)
                
                # Vérifie que le fichier compilé existe bien, sinon lève une exception FileNotFoundError
                if not os.path.isfile(compiled_TXT_path):
                    raise FileNotFoundError(f"Le fichier compilé {compiled_TXT_path} n'a pas été trouvé.")
                
                # Lit le contenu du fichier compilé pour le résumé
                with open(compiled_TXT_path, 'r', encoding='utf-8') as compiled_file:
                    compiled_content = compiled_file.read()
                # Le client LLM applique un timeout et un nombre de retries borné.
                llm_text = build_resum_with_LLM(compiled_content, row.get("tokens_for_llm"))

                # Nettoyage du titre extrait du résumé et reformatage du texte
                title = clean_title(llm_text.splitlines()[0])  # Extrait la première ligne comme titre
                llm_lines = llm_text.splitlines()
                llm_lines[0] = title  # Remplace la première ligne par le titre nettoyé
                llm_text = "\n".join(llm_lines)  # Recombine le résumé complet

                # Génère un nom de fichier basé sur la date et heure actuelles pour stocker le résumé
                current_time = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                resume_file_name = f"resum_ARIA_hotnews_{current_time}.txt"
                resume_file_path = os.path.join(my_computer_path, sav_path_resume_to_post.lstrip("/"), resume_file_name).replace("\\", "/")
                
                # Sauvegarde le résumé dans le chemin défini
                with open(resume_file_path, 'w', encoding='utf-8') as resume_file:
                    resume_file.write(llm_text)
                
                # Génère l'horodatage UTC actuel pour l'entrée du DataFrame
                current_utc_datetime = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                path_for_df = os.path.join(sav_path_resume_to_post, resume_file_name).replace("\\", "/")
                
                # Enregistrement du chemin de sauvegarde dans les logs pour vérification
                logging.info(f"Résumé LLM archivé dans {path_for_df}")

                # Prépare une nouvelle ligne pour df_resum_to_send contenant toutes les informations
                new_row = {
                    "id_working": row["id_news"],
                    "id_to_send": None,
                    "titles": title,
                    "hot_news": row["hot_news"],
                    "llm_resum": True,
                    "prepare_txt": True,
                    "update_id": row["update_id"],
                    "list_cryptos": None,  # Ce champ peut être rempli plus tard
                    "resum_news": None,
                    "full_text": llm_text,
                    "utc_datetime_news": current_utc_datetime,
                    "source_domain": PLATFORM_SOURCE_DOMAIN,
                    "path_text_files": path_for_df,
                    "source_ids": [int(value) for value in row["list_similar_news"]],
                }
                # Ajoute la nouvelle ligne au DataFrame `df_resum_to_send`
                df_resum_to_send = pd.concat([df_resum_to_send, pd.DataFrame([new_row])], ignore_index=True)
                
                # Ajouter les IDs de `list_similar_news` au set pour obtenir des valeurs uniques
                unique_similar_news_ids.update(row['list_similar_news'])
                
    except (FileNotFoundError, ValueError):
        logging.exception("Échec de génération d'un résumé Hot News")
        raise
    except Exception:
        logging.exception("Erreur inattendue pendant la génération Hot News")
        raise
    
    # Retourne le DataFrame final contenant les résumés générés
    return df_resum_to_send, unique_similar_news_ids




def find_cryptos_in_text(full_text_to_analyse: str, df_coin_list: pd.DataFrame) -> str:
    """
    DESCRIP : Identifier les tokens de cryptomonnaies mentionnés dans le texte fourni et les retourner sous forme de chaîne de caractères séparée par des virgules.
    INPUT   <full_text_to_analyse> : str : Texte complet à analyser.
            <df_coin_list> : pd.DataFrame : DataFrame contenant les tokens et noms de cryptomonnaies.
    OUTPUT  <coins_str> : str : Chaîne de caractères avec les tokens de cryptomonnaies trouvés, séparés par des virgules,
                                ou 'no_crypto_detected' si aucun token n'est trouvé.
    """
    # Utiliser une expression régulière pour trouver tous les mots, même s'ils sont encadrés par () ou []
    words = re.findall(r"\b\w+\b", re.sub("[\\[\\]()]", " ", full_text_to_analyse))

    # Créer un ensemble pour collecter les tokens de cryptos trouvés dans le texte
    list_coin_found = set()

    # Chercher les tokens de cryptos dans le contenu du texte
    list_coin_found.update(
        df_coin_list[df_coin_list["crypto_ticker"].isin(words)]["crypto_ticker"].tolist()
    )

    # Chercher les noms de cryptos dans le contenu du texte
    for crypto_name in df_coin_list["crypto_name"]:
        if re.search(r"\b" + re.escape(crypto_name) + r"\b", full_text_to_analyse):
            corresponding_ticker = df_coin_list[
                df_coin_list["crypto_name"] == crypto_name
            ]["crypto_ticker"].values[0]
            list_coin_found.add(corresponding_ticker)

    # Convertir en une chaîne de caractères séparée par des virgules, ou retourner 'no_crypto_detected' si aucun token n'est trouvé
    coins_str = ", ".join(list_coin_found) if list_coin_found else "no_crypto_detected"

    return coins_str



def generate_resum_500(full_text_to_analyse: str) -> str:
    """
    Extrait un résumé de 500 caractères d'un texte donné en éliminant le titre et les sauts de ligne initiaux.
    
    Arguments :
        full_text_to_analyse (str) : Le texte complet à analyser.
        
    Retour :
        str : Le résumé de 500 caractères, avec "[...]" ajouté en fin.
    """
    # Éliminer le titre (1ère ligne) et les lignes blanches éventuelles
    text_lines = full_text_to_analyse.splitlines()
    cleaned_text = " ".join(line for line in text_lines[1:] if line.strip())

    # Récupérer les 495 premiers caractères
    resum_500 = cleaned_text[:495] + "[...]"
    return resum_500



def clean_title(title: str) -> str:
    """
    DESCRIPTION : Nettoie le titre en supprimant les caractères indésirables comme '#' ou '*'.
    INPUT   <title> : str : Le titre à nettoyer.
    OUTPUT  <cleaned_title> : str : Le titre nettoyé sans les caractères indésirables.
    """
    # Liste des caractères indésirables à supprimer
    chars_to_remove = ['#', '*']
    
    # Supprimer les caractères indésirables du titre
    cleaned_title = title
    for char in chars_to_remove:
        cleaned_title = cleaned_title.replace(char, '')
    
    # Supprimer les espaces en trop au début et à la fin
    cleaned_title = cleaned_title.strip()

    return cleaned_title


def build_resum_with_LLM(file_content, tokens_for_llm=None):
    """Génère l'article Hot News à partir de plusieurs sources textuelles."""
    prompt = build_hotnews_prompt(file_content, tokens_for_llm)
    client = get_openai_client()
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Return a factual, concise crypto-news article."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=1000,
    )
    response_text = response.choices[0].message.content
    if not response_text or not response_text.strip():
        raise ValueError("Le LLM a retourné une réponse vide")
    return response_text.strip()




def final_data_ARIA_to_send(df_resum_to_send, file_path_df_list_crypto_acronymes_updated):
    """
    Complète le DataFrame `df_resum_to_send` en ajoutant les informations `id_to_send` et `list_cryptos` pour chaque news.
    
    Étapes :
        1. Charge le fichier des acronymes et noms de cryptomonnaies dans `df_coin_list`.
        2. Récupère `last_id_api_newsblotter` depuis l'API pour initialiser l'ID de la news.
        3. Parcourt chaque ligne de `df_resum_to_send` pour ajouter les informations :
            - Recherche des cryptomonnaies dans `full_text` et mise à jour de la colonne `list_cryptos`.
            - Génère un résumé de 500 caractères pour `resum_news`.
            - Gère l'attribution de `id_to_send` basé sur `update_id` ou en incrémentant `last_id_api_newsblotter`.
    
    Paramètres :
        - df_resum_to_send : pd.DataFrame : DataFrame contenant les news résumées.
        - file_path_df_list_crypto_acronymes_updated : str : Chemin du fichier des acronymes et noms de crypto.
        - get_last_news_id : fonction : Fonction pour récupérer le dernier ID de l'API newsblotter.
        - find_cryptos_in_text : fonction : Fonction pour identifier les cryptomonnaies dans le texte.
        - generate_resum_500 : fonction : Fonction pour générer un résumé de 500 caractères.
    
    Exceptions :
        - Arrête le script si la récupération de `last_id_api_newsblotter` échoue.
    
    Retour :
        - df_resum_to_send : pd.DataFrame : DataFrame mis à jour avec `list_cryptos`, `resum_news`, et `id_to_send`.
    """
    df_resum_to_send["real_links"] = ""

    # Chargement de la liste des tokens de cryptomonnaies
    try:
        df_coin_list = pd.read_excel(file_path_df_list_crypto_acronymes_updated)
    except Exception:
        logging.exception("Erreur lors du chargement du référentiel crypto")
        raise

    # Récupération de l'ID le plus récent de l'API newsblotter
    try:
        last_id_api_newsblotter = int(get_last_news_id())
        if last_id_api_newsblotter == -1:
            raise ValueError("Échec de la récupération du dernier ID de l'API.")
    except Exception as exc:
        raise RuntimeError("Impossible de récupérer le dernier ID de l'API news") from exc

    # Parcourir chaque ligne de df_resum_to_send pour remplir `list_cryptos` et `id_to_send`
    for index in df_resum_to_send.index:
        full_text = df_resum_to_send.loc[index, "full_text"]

        # Identifier les cryptomonnaies dans le texte et mettre à jour `list_cryptos`
        list_cryptos_found = find_cryptos_in_text(full_text, df_coin_list)
        df_resum_to_send.at[index, "list_cryptos"] = list_cryptos_found

        # Générer le résumé de 500 caractères pour `resum_news`
        resum_500 = generate_resum_500(full_text)
        df_resum_to_send.at[index, "resum_news"] = resum_500

        # Gestion de `id_to_send` basé sur `update_id` ou incrément de `last_id_api_newsblotter`
        if pd.notnull(df_resum_to_send.loc[index, "update_id"]):
            df_resum_to_send.at[index, "id_to_send"] = df_resum_to_send.loc[index, "update_id"]
        else:
            last_id_api_newsblotter += 1
            df_resum_to_send.at[index, "id_to_send"] = last_id_api_newsblotter
        
        df_resum_to_send.at[index, "real_links"] = public_news_url(df_resum_to_send.at[index, "id_to_send"])


    return df_resum_to_send

# --------------------------------------------------- API TOKENS / TAGS ---------------------------------------------------



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
            raise ValueError("Référentiel tokens/tags vide")

        # Convertir les données en DataFrame
        df = pd.json_normalize(clean_data)

        # Nettoyage et structuration
        if 'tags' in df.columns:
            df['tags'] = df['tags'].apply(lambda x: ", ".join(x) if isinstance(x, list) else "")
        
        return df

    except (requests.RequestException, ValueError):
        logging.exception("Échec de récupération du référentiel tokens/tags")
        raise
    except Exception:
        logging.exception("Erreur inattendue pendant la récupération du référentiel tokens/tags")
        raise






# --------------------------------------------------------------------------------------------------------
# Requêtes GET vers l’API applicative configurée
# --------------------------------------------------------------------------------------------------------
def read_data_between_dates(url_base: str, start_date: str, end_date: str, aria_crypto_api_key: str) -> pd.DataFrame:
    """
    Récupère et nettoie les données des news depuis une API pour une catégorie donnée et entre deux dates.
    
    Parameters:
    - url_base (str): L'URL de base de l'API pour récupérer les données des news.
    - start_date (str): Date de début pour le filtrage au format "YYYY-MM-DD".
    - end_date (str): Date de fin pour le filtrage au format "YYYY-MM-DD".
    - aria_crypto_api_key (str): Clé API pour l'authentification.
    
    Returns:
    - pd.DataFrame: DataFrame contenant les données nettoyées et filtrées.
    """
    # Construire l'URL avec les paramètres de date et de catégorie
    url = f"{url_base}?startDate={start_date}&endDate={end_date}"
    
    headers = {
        "Content-Type": "application/json",
        ARIA_API_KEY_HEADER: aria_crypto_api_key
    }
    
    try:
        # Effectuer la requête GET avec l'URL complète et les en-têtes
        response = safe_get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # Vérifier si la requête a réussi
        if response.status_code == 200:
            raw_data = response.json()
            logging.info("Requête API pour df_read_api2 réussie, données récupérées.")
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

    except (requests.RequestException, ValueError):
        logging.exception("Erreur lors de la récupération des données")
        raise
    except Exception:
        logging.exception("Erreur inattendue lors de la récupération des données")
        raise


# Récupération du dernier identifiant publié.
def get_last_news_id() -> int:
    """Récupère le dernier identifiant avec authentification explicite."""
    api_key = require_env("ARIA_API_KEY")
    response = safe_get(
        api_url(LATEST_NEWS_API_PATH),
        headers={"Accept": "application/json", ARIA_API_KEY_HEADER: api_key},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or "id" not in data:
        raise ValueError("Réponse inattendue pour le dernier ID news")
    return int(data["id"])



def prepare_final_df_for_api(final_df_to_send: pd.DataFrame) -> pd.DataFrame:
    """
    Prépare le DataFrame `final_df_to_send` pour être compatible avec l'API en appliquant les conversions de type nécessaires.

    Paramètre:
        - final_df_to_send : pd.DataFrame : DataFrame contenant les données à envoyer à l'API.

    Retour:
        - pd.DataFrame : DataFrame `final_df_to_send` avec les colonnes au format attendu.
    """
    
    # Conversion des colonnes en entier
    int_columns = ["id_to_send", "id_working", "update_id"]
    for col in int_columns:
        final_df_to_send[col] = pd.to_numeric(final_df_to_send[col], errors='coerce').fillna(0).astype(int)

    # Conversion des colonnes en booléen
    bool_columns = ["hot_news", "llm_resum", "prepare_txt"]
    for col in bool_columns:
        final_df_to_send[col] = final_df_to_send[col].astype(bool)

    # Conversion de la colonne date en format datetime UTC (ISO 8601)
    final_df_to_send["utc_datetime_news"] = pd.to_datetime(
        final_df_to_send["utc_datetime_news"], errors='coerce'
    ).dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    # Vérifier que les colonnes de texte sont bien au format `str`
    text_columns = ["titles", "list_cryptos", "resum_news", "source_domain", "path_text_files"]
    for col in text_columns:
        final_df_to_send[col] = final_df_to_send[col].astype(str)
    
    # Conversion de `id_to_send` en int64 avec NaN remplacés par 0 pour garantir qu'il n'est jamais vide
    final_df_to_send["id_to_send"] = pd.to_numeric(final_df_to_send["id_to_send"], errors='coerce').fillna(0).astype(int)
    
    # Conversion de `id_working` sans remplacer NaN par 0 (il est supposé ne jamais être vide)
    final_df_to_send["id_working"] = pd.to_numeric(final_df_to_send["id_working"], errors='coerce').astype(int)
    
    # Conversion de `update_id` sans remplacer NaN par 0 (laisser NaN pour signifier "pas de mise à jour")
    final_df_to_send["update_id"] = pd.to_numeric(final_df_to_send["update_id"], errors='coerce')


    return final_df_to_send



# --------------------------------------------------------------------------------------------------------
# requetes POST sur API applicative
# --------------------------------------------------------------------------------------------------------


def post_hotnews(row, aria_crypto_api_key):
    """
    Publie une Hot News consolidée vers l’API applicative.
    INPUT   <row> : dict : Une ligne de données de df_resum_to_send
    """
    url = api_url(ADD_NEWS_API_PATH)
    source_ids = sorted(int(value) for value in row.get("source_ids", []) if pd.notna(value))
    fingerprint = ",".join(map(str, source_ids)) or f"id:{int(row['id_to_send'])}"
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]
    headers = {
        "Content-Type": "application/json",
        ARIA_API_KEY_HEADER: aria_crypto_api_key,
        "Idempotency-Key": f"hotnews-{digest}",
    }

    # Champs attendus par l'API avec les types appropriés
    # Utiliser 'update_id' si présent, sinon 'id_to_send'
    expected_fields = {
        "id": int(row["id_to_send"]),
        "real_links": row.get("real_links", ""),
        "titles": row.get("titles", ""),
        "list_cryptos": row.get("list_cryptos", "no_crypto_detected"),
        "tot_cat_words": 0,
        "cat_risks": 0.0,
        "cat_governance": 0.0,            # float obligatoire !! pas None ni Nan
        "cat_tokenomics": 0.0,
        "cat_adoption": 0.0,
        "cat_investment_analytics": 0.0,
        "resum_news": row.get("resum_news", ""),
        "utc_datetime_news": row["utc_datetime_news"].strftime('%Y-%m-%dT%H:%M:%SZ') if row["utc_datetime_news"] else "",
        "source_domain": row.get("source_domain", ""),
        "path_text_files": row.get("path_text_files", ""),
        "hot_news": bool(row.get("hot_news", False)),
        "llm_resum": bool(row.get("llm_resum", False)),
        "actor":  0,
        "tradfi":  0,
        "action":  0,
        "crypto":  0,
        "full_text": row.get("full_text", "")
    }
    logging.info(f"Envoi des données de news pour ID {row['id_to_send']}")



    try:
        response = safe_post(url, json=expected_fields, headers=headers, timeout=10)
        response.raise_for_status()
        logging.info(f"News ID {row['id_to_send']} envoyée avec succès")

    except requests.RequestException:
        logging.exception("Erreur réseau lors de l'envoi des données de news")
        raise
    except Exception:
        logging.exception("Erreur inattendue lors de l'envoi des données de news")
        raise





# --------------------------------------------------------------------------------------------------------
# requetes delete sur API applicative

# --------------------------------------------------------------------------------------------------------
# Requêtes PUT sur l’API applicative
# --------------------------------------------------------------------------------------------------------

def mark_llm_summary(news_id, llm_resum_value, aria_crypto_api_key):
    """
    DESCRIPTION : Met à jour le statut "llm_resum" pour une news spécifique via l'API applicative.
    INPUT   <news_id> : int : L'ID de la news à mettre à jour.
            <llm_resum_value> : bool : La nouvelle valeur à affecter à "llm_resum".
    OUTPUT  None
    """
    try:
        url = api_url(UPDATE_LLM_STATUS_API_PATH)
        headers = {
            "Content-Type": "application/json",
            ARIA_API_KEY_HEADER: aria_crypto_api_key
        }
        news_id = int(news_id)
        payload = {"id": news_id, "llm_resum": llm_resum_value}
        logging.info(f"Tentative de mise à jour de la news #{news_id} avec llm_resum={llm_resum_value}")
        response = safe_put(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        logging.info(f"News #{news_id} mise à jour avec succès")
    except requests.RequestException:
        logging.exception("Erreur réseau lors de la mise à jour du statut LLM pour news #%s", news_id)
        raise
    except Exception:
        logging.exception("Erreur inattendue lors de la mise à jour du statut LLM pour news #%s", news_id)
        raise


def update_hotnews(row, aria_crypto_api_key):
    """
    Met à jour les données de la news sur l’API applicative.
    INPUT   <row> : dict ou pd.Series : Une ligne de données de df_resum_to_send
            <aria_crypto_api_key> : str : Clé API pour l'authentification.
    """
    url = api_url(UPDATE_NEWS_API_PATH)
    headers = {
        "Content-Type": "application/json",
        ARIA_API_KEY_HEADER: aria_crypto_api_key
    }

     # Champs attendus par l'API avec les types appropriés
    expected_fields = {
        "id": int(row["update_id"]),
        "real_links": row.get("real_links", ""),
        "titles": row.get("titles", ""),
        "list_cryptos": row.get("list_cryptos", "no_crypto_detected"),
        "tot_cat_words": int(row.get("tot_cat_words", 0)),
        "cat_risks": float(row.get("cat_risks", 0.0)),
        "cat_governance": float(row.get("cat_governance", 0.0)),            # float obligatoire !! pas None ni NaN
        "cat_tokenomics": float(row.get("cat_tokenomics", 0.0)),
        "cat_adoption": float(row.get("cat_adoption", 0.0)),
        "cat_investment_analytics": float(row.get("cat_investment_analytics", 0.0)),
        "resum_news": row.get("resum_news", ""),
        "utc_datetime_news": row["utc_datetime_news"].strftime('%Y-%m-%dT%H:%M:%SZ') if row["utc_datetime_news"] else "",
        "source_domain": row.get("source_domain", ""),
        "path_text_files": row.get("path_text_files", ""),
        "hot_news": bool(row.get("hot_news", False)),
        "llm_resum": bool(row.get("llm_resum", False)),
        "actor": int(row.get("actor", 0)),
        "tradfi": int(row.get("tradfi", 0)),
        "action": int(row.get("action", 0)),
        "crypto": int(row.get("crypto", 0)),
        "full_text": row.get("full_text", "")
    }
    logging.info(f"Envoi de la mise à jour des données pour ID {row['id_to_send']}") # avec payload : {expected_fields}")

    try:
        response = safe_put(url, json=expected_fields, headers=headers, timeout=10)
        response.raise_for_status()
        logging.info(f"News ID {row['id_to_send']} mise à jour avec succès")

    except requests.RequestException:
        logging.exception("Erreur réseau lors de la mise à jour des données de news")
        raise
    except Exception:
        logging.exception("Erreur inattendue lors de la mise à jour des données de news")
        raise







