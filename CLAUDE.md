# CLAUDE.md — job-radar

## Contexte

`job-radar` est un outil de veille d'offres d'emploi en ligne de commande, adossé à l'API
officielle **France Travail « Offres d'emploi v2 »**. Projet vitrine du GitHub d'Ayman Elalass
(`AymanElalass`) : le code doit rester propre, testé et documenté.

Le cœur du produit : à chaque exécution, n'afficher **que les offres jamais vues**, en comparant
les résultats de l'API à un historique local.

## Stack

- Python 3.11+, gestion de projet et d'environnement avec **uv**
- code applicatif dans `src/job_radar/`, tests dans `tests/`
- dépendances : `requests`, `python-dotenv` ; dev : `pytest`, `ruff`
- commande exposée : `job-radar` → `job_radar.cli:main`

## Architecture

| Module | Rôle |
| --- | --- |
| `src/job_radar/client.py` | OAuth2 client credentials, recherche paginée et triée, `reduire_offre()`, `normaliser_mot_cle()` |
| `src/job_radar/stockage.py` | historique SQLite (`data/offres.db`), détection des nouveautés |
| `src/job_radar/cli.py` | lecture de `config.toml` et du `.env`, orchestration, affichage, JSON |

Ce découpage en trois modules est volontaire : y ajouter un module demande une bonne raison.

## Rappels sur l'API

- Jeton : `POST https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire`,
  `grant_type=client_credentials`, scopes `api_offresdemploiv2 o2dsoffre`.
- Recherche : `GET https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search`,
  paramètres `motsCles`, `departement`, `publieeDepuis` (1, 3, 7, 14 ou 31), `range` (`0-149`)
  et `sort`.
- `sort` : `0` = pertinence décroissante, `1` = date de création décroissante, `2` = distance
  croissante. On envoie toujours `1` (`SORT_DATE_DECROISSANTE`) pour que la pagination ramène
  les offres les plus récentes.
- `departement` est facultatif : omis, la recherche porte sur toute la France. Une liste
  `departements` vide dans `config.toml` est donc valide et déclenche ce mode.
- `motsCles` : 7 mots-clés maximum séparés par des virgules, chacun d'au moins 2 caractères,
  caractères autorisés `[a-zA-Z0-9]`, espace et ``@#$%^&+./-"``. Une expression de plusieurs
  mots est valide et part telle quelle ; la virgule est refusée côté `job-radar` car elle
  changerait le sens de la requête. Toute la validation vit dans `normaliser_mot_cle()`.
- 150 offres maximum par requête ; statuts `200`/`206` → clé `resultats`, `204` → aucun résultat.
  L'index du premier élément du `range` ne doit pas dépasser 1000, celui du dernier 1149.
- Limite de 10 requêtes/seconde : ne pas contourner l'espacement des appels dans le client.
- Les champs des offres sont très majoritairement optionnels : toute lecture doit être tolérante
  aux clés absentes ou nulles.

## Gestion des erreurs

`ErreurFranceTravail` se décline en deux cas qu'il ne faut pas confondre :

- `ErreurAuthentification` — fatale. Elle traverse `collecter()` sans être rattrapée : sans jeton,
  aucune recherche ne peut aboutir, et réessayer pour chaque couple mot-clé/département ne ferait
  qu'empiler les échecs. La CLI l'affiche avec un message d'aide et sort en code 3.
- `ErreurRecherche` — locale à une recherche. Elle est signalée sur stderr et les recherches
  suivantes continuent.

Codes de retour de la CLI : `0` succès, `1` erreur d'exécution, `2` configuration ou identifiants
manquants, `3` authentification refusée.

## Règles de travail

- **Langue** : code, commentaires, docstrings, messages CLI, commits et documentation en français.
- **Secrets** : `FT_CLIENT_ID` / `FT_CLIENT_SECRET` vivent dans `.env`, jamais versionné.
  Ne jamais ouvrir, lire ou écrire `.env` ni `~/.secrets` — l'utilisateur les remplit lui-même.
  `.env.example` reste vide de toute valeur réelle.
- **Données locales** : `data/` (base SQLite, `nouvelles.json`) est ignoré par git.
- **Tests** : aucun appel réseau. Les doublures (`tests/faux_reseau.py`) fournissent une session
  HTTP et un client factices ; SQLite tourne sur `tmp_path` ou en mémoire. Les tests couvrent la
  réduction des offres, la détection des nouveautés, les paramètres de requête (tri, département
  optionnel, mots-clés) et la lecture de `config.toml`. Toute nouvelle logique métier vient avec
  ses tests.
- **Qualité** : `uv run pytest` et `uv run ruff check .` doivent passer avant tout commit.
- **Git** : branche `main`. Ne jamais créer de dépôt GitHub ni pousser sans demande explicite.

## Feuille de route

1. **Veille brute** (fait) — recherche, dédoublonnage, historique, export JSON.
2. **Tri par LLM** — noter les offres de `data/nouvelles.json` via `claude -p` selon un profil
   cible, ne garder que les plus pertinentes avec une justification courte.
3. **CV ciblé** — dépôt séparé : génération d'un CV et d'une lettre adaptés à chaque offre retenue.
