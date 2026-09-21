# CLAUDE.md — job-radar

## Contexte

`job-radar` est un outil de veille d'offres d'emploi en ligne de commande, adossé à l'API
officielle **France Travail « Offres d'emploi v2 »**. Projet vitrine du GitHub d'Ayman Elalass
(`AymanElalass`) : le code doit rester propre, testé et documenté.

Le cœur du produit : n'afficher **que les offres jamais vues** (commande `collecter`), puis ne
retenir **que celles qui valent une candidature** après notation par un LLM (commande `trier`).

## Stack

- Python 3.11+, gestion de projet et d'environnement avec **uv**
- code applicatif dans `src/job_radar/`, tests dans `tests/`
- dépendances : `requests`, `python-dotenv`, `rich` ; dev : `pytest`, `ruff`
- commande exposée : `job-radar` → `job_radar.cli:main`, avec les sous-commandes `collecter`
  (celle par défaut, pour rester compatible avec `job-radar` seul) et `trier`
- le tri appelle la CLI `claude` (Claude Code) en sous-processus

## Architecture

| Module | Rôle |
| --- | --- |
| `src/job_radar/client.py` | OAuth2 client credentials, recherche paginée et triée, `reduire_offre()`, `normaliser_mot_cle()` |
| `src/job_radar/stockage.py` | SQLite (`data/offres.db`) : table `offres` (vues + contenu réduit), table `tri` (verdicts) |
| `src/job_radar/llm.py` | **seul** point de contact avec un LLM : `claude -p --output-format json` |
| `src/job_radar/tri.py` | pré-filtre gratuit, découpage en lots, prompt, validation des réponses |
| `src/job_radar/cli.py` | `config.toml` et `.env`, sous-commandes, affichage `rich`, sorties JSON |

Le découpage est volontaire : ajouter un module demande une bonne raison. En particulier,
`llm.py` n'expose qu'une chose, `ClientLLM` — une fonction prompt → texte. Le reste du code ne
sait pas comment le modèle est appelé : passer de `claude -p` à l'API Claude ne doit toucher que
ce fichier. Aucun autre module n'importe `subprocess`.

## Tri par LLM (étape 2)

- **Pré-filtre d'abord, LLM ensuite.** Tout ce qui peut être écarté gratuitement en Python doit
  l'être : profession libérale, 3 ans d'expérience ou plus exigés (`SEUIL_EXPERIENCE_ANNEES`),
  doublons intitulé + entreprise. Le nombre d'offres écartées et leur motif sont toujours affichés.
- **Lots de 20** (`taille_lot`), et seulement les champs de `CHAMPS_ENVOYES` plus les
  `LONGUEUR_DESCRIPTION` (800) premiers caractères de la description : on n'envoie pas une offre
  entière au modèle.
- **Réponse JSON strictement validée** (`valider_reponse`) : id connu, score entier 0-100,
  verdict parmi `postuler` / `peut-etre` / `non`. Les drapeaux inconnus sont ignorés — une
  invention du modèle sur ce point ne justifie pas de jeter un lot. En cas de réponse
  inexploitable : **une seule** nouvelle tentative, puis le lot est signalé en erreur et les
  autres continuent.
- **Une offre n'est jamais triée deux fois** : la table `tri` est la mémoire du tri, et
  `offres_a_trier()` exclut ce qui y figure déjà.
- Le **fichier de critères** est personnel et vit hors du dépôt (`[tri].criteres`). Ne jamais le
  versionner ni recopier son contenu dans le dépôt : `criteres.example.md` est la seule version
  publique.

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
- **Tests** : aucun appel réseau, et **aucun appel réel au LLM**. Les doublures
  (`tests/faux_reseau.py`) fournissent une session HTTP, un client API et un client LLM factices ;
  SQLite tourne sur `tmp_path` ou en mémoire. Les tests couvrent la réduction des offres, la
  détection des nouveautés, les paramètres de requête, la lecture de `config.toml`, le pré-filtre,
  la validation des réponses du modèle et la sous-commande `trier` de bout en bout. Toute nouvelle
  logique métier vient avec ses tests.
- **Qualité** : `uv run pytest` et `uv run ruff check .` doivent passer avant tout commit.
- **Git** : branche `main`. Ne jamais créer de dépôt GitHub ni pousser sans demande explicite.

## Feuille de route

1. **Veille brute** (fait) — recherche, dédoublonnage, historique, export JSON.
2. **Tri par LLM** (fait) — pré-filtre gratuit, notation par lots via `claude -p`, verdicts en
   SQLite, tableau `rich` et `data/selection.json`.
3. **CV ciblé** — dépôt séparé : génération d'un CV et d'une lettre adaptés à chaque offre de
   `data/selection.json`.
