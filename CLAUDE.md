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
| `src/job_radar/client.py` | OAuth2 client credentials, recherche paginée, `reduire_offre()` |
| `src/job_radar/stockage.py` | historique SQLite (`data/offres.db`), détection des nouveautés |
| `src/job_radar/cli.py` | lecture de `config.toml` et du `.env`, orchestration, affichage, JSON |

Ce découpage en trois modules est volontaire : y ajouter un module demande une bonne raison.

## Rappels sur l'API

- Jeton : `POST https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire`,
  `grant_type=client_credentials`, scopes `api_offresdemploiv2 o2dsoffre`.
- Recherche : `GET https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search`,
  paramètres `motsCles`, `departement`, `publieeDepuis` (1, 3, 7, 14 ou 31), `range` (`0-149`).
- 150 offres maximum par requête ; statuts `200`/`206` → clé `resultats`, `204` → aucun résultat.
- Limite de 10 requêtes/seconde : ne pas contourner l'espacement des appels dans le client.
- Les champs des offres sont très majoritairement optionnels : toute lecture doit être tolérante
  aux clés absentes ou nulles.

## Règles de travail

- **Langue** : code, commentaires, docstrings, messages CLI, commits et documentation en français.
- **Secrets** : `FT_CLIENT_ID` / `FT_CLIENT_SECRET` vivent dans `.env`, jamais versionné.
  Ne jamais ouvrir, lire ou écrire `.env` ni `~/.secrets` — l'utilisateur les remplit lui-même.
  `.env.example` reste vide de toute valeur réelle.
- **Données locales** : `data/` (base SQLite, `nouvelles.json`) est ignoré par git.
- **Tests** : aucun appel réseau. Les tests couvrent la réduction des offres et la détection des
  nouveautés ; SQLite tourne sur `tmp_path` ou en mémoire. Toute nouvelle logique métier vient
  avec ses tests.
- **Qualité** : `uv run pytest` et `uv run ruff check .` doivent passer avant tout commit.
- **Git** : branche `main`. Ne jamais créer de dépôt GitHub ni pousser sans demande explicite.

## Feuille de route

1. **Veille brute** (fait) — recherche, dédoublonnage, historique, export JSON.
2. **Tri par LLM** — noter les offres de `data/nouvelles.json` via `claude -p` selon un profil
   cible, ne garder que les plus pertinentes avec une justification courte.
3. **CV ciblé** — dépôt séparé : génération d'un CV et d'une lettre adaptés à chaque offre retenue.
