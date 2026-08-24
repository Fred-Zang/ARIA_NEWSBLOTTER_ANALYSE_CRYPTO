# 🔐 Politique de sécurité — édition publique

## Périmètre

Ce dépôt est une version publique et anonymisée de workers Python historiques. Il ne contient pas l’infrastructure ARIA de production, les secrets, les routes réelles, les données clients/utilisateurs, les historiques complets de news ni les référentiels internes.

## Secrets

Les secrets doivent être fournis uniquement au runtime via l’environnement :

- `ARIA_API_KEY`
- `ARIA_WALLET_API_KEY`
- `OPENAI_API_KEY`
- `ETHERSCAN_API_KEY`

Ne jamais committer de fichier `.env`. Le fichier `.env.example` ne contient que des valeurs factices ou publiques.

## Réseau et scraping

Les primitives de `src/http_safety.py` imposent HTTPS, bornent les redirections et la taille des réponses, refusent par défaut les destinations privées/locales et retirent les headers sensibles lors d’une redirection vers une autre origine.

L’accès à une API privée peut être activé explicitement avec `ARIA_ALLOW_PRIVATE_API_HOSTS=true`. Cette exception ne vaut que pour l’hôte exact configuré dans `ARIA_API_BASE_URL`.

La validation DNS réduit les risques SSRF mais ne constitue pas à elle seule un sandbox réseau contre toutes les variantes possibles, notamment les scénarios avancés de rebinding. Pour un système acceptant des URLs arbitraires et non fiables, isoler les scrapers dans un réseau/conteneur dédié sans secrets inutiles.

## Parsing et navigateur

- Les flux RSS contenant `DOCTYPE` ou `ENTITY` sont refusés avant parsing XML.
- Selenium applique des timeouts de chargement et de script.
- Les conteneurs ne publient aucun port WebDriver/VNC.
- Le dépôt public n’inclut pas d’automatisation de CAPTCHA ou de mécanisme visant à contourner une vérification humaine.

## Docker

Les services principaux utilisent un utilisateur non-root, un filesystem racine en lecture seule, `no-new-privileges` et `cap_drop: ALL`. Les seuls chemins modifiables sont les volumes runtime et `/tmp` en `tmpfs`.

Le `docker-compose.yml` applique également le principe du moindre privilège aux variables d’environnement : chaque service reçoit uniquement les secrets nécessaires à son rôle, plutôt qu’un `.env` complet.

Selon le noyau Linux et la configuration de Chromium, le sandbox navigateur peut nécessiter des ajustements spécifiques à l’hôte. Il est préférable d’adapter le runtime plutôt que d’ajouter globalement des privilèges au conteneur.

## Données et logs

Le contenu de `runtime/` est ignoré par Git à l’exception des placeholders. Les workers évitent de journaliser les clés, les bodies de réponses API et les prompts/réponses LLM complets.

Les exports Excel de diagnostic appliquent une neutralisation simple des préfixes pouvant être interprétés comme des formules.

## Dépendances

Les dépendances de la V1 publique sont épinglées et Dependabot est configuré pour signaler les mises à jour. Les images Docker doivent être reconstruites régulièrement afin de récupérer les correctifs de sécurité du système de base.

## Signalement

Si ce dépôt est publié sur GitHub, utiliser de préférence la fonctionnalité **Private vulnerability reporting** du dépôt pour transmettre une vulnérabilité sans l’exposer publiquement dans une issue.
