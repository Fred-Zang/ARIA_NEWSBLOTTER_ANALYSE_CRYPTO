# Référentiels synthétiques

Ces fichiers montrent uniquement la **forme** de quelques référentiels attendus par les workers.
Ils ne contiennent ni catalogue de sources de production, ni sélecteurs réels, ni taxonomie interne complète.

En production, les workers lisent des classeurs `.xlsx` placés dans `runtime/data_main/` :

- `RSS_list_url_balises/RSS_classique_updated.xlsx`
- `RSS_list_url_balises/RSS_special_updated.xlsx`
- `updated_categories_files/list_TAGS_5_categories_updated.xlsx`
- `updated_crypto_list_files/list_crypto_acronymes_updated.xlsx`

Le référentiel `Actor / TradFi / Action / Crypto` utilisé pour la détection Hot News est fourni par l'API applicative et n'est pas publié ici.
