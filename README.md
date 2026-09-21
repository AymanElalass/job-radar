# job-radar

Outil de veille d'offres d'emploi en ligne de commande, branché sur l'API officielle
**France Travail « Offres d'emploi v2 »**.

À chaque exécution, `job-radar` interroge l'API selon les critères de `config.toml`,
compare les résultats à un historique local et n'affiche **que les offres jamais vues**.

## Fonctionnement

1. Authentification OAuth2 (*client credentials*) auprès de France Travail.
2. Une recherche par couple **mot-clé × département**, puis dédoublonnage par identifiant d'offre.
3. Réduction de chaque offre aux champs utiles : intitulé, lieu, entreprise, contrat, salaire,
   expérience, alternance, date de création, URL d'origine et description.
4. Comparaison à l'historique SQLite (`data/offres.db`) : les offres déjà vues sont écartées.
5. Affichage des nouveautés dans le terminal et écriture du détail dans `data/nouvelles.json`.

## Installation

Prérequis : [uv](https://docs.astral.sh/uv/) et Python 3.11+.

```bash
git clone https://github.com/AymanElalass/job-radar.git
cd job-radar
uv sync
```

### Identifiants France Travail

1. Créer un compte et une application sur [francetravail.io](https://francetravail.io/),
   en souscrivant à l'API **Offres d'emploi v2**.
2. Copier les identifiants dans un fichier `.env` (jamais versionné) :

```bash
cp .env.example .env
```

```dotenv
FT_CLIENT_ID=votre-identifiant
FT_CLIENT_SECRET=votre-secret
```

## Configuration

Les critères de veille se règlent dans `config.toml` :

```toml
[recherche]
mots_cles = ["développeur python", "data engineer"]
departements = ["75", "92"]
publiee_depuis = 7   # 1, 3, 7, 14 ou 31 jours
pages_max = 1        # pages de 150 offres par recherche
```

## Usage

```bash
uv run job-radar
```

Options disponibles :

| Option | Rôle |
| --- | --- |
| `--config CHEMIN` | autre fichier de critères (défaut `config.toml`) |
| `--base CHEMIN` | autre base d'historique (défaut `data/offres.db`) |
| `--sortie CHEMIN` | autre fichier JSON de sortie (défaut `data/nouvelles.json`) |
| `--sans-historique` | afficher les offres sans les marquer comme vues |
| `--silencieux` | masquer le détail de progression des recherches |

Exemple de sortie :

```
Recherche : 2 mot(s)-clé × 2 département(s), publiées depuis 7 jour(s)
  'développeur python' / dép. 75 / page 1 : 118 offres
  ...

143 offre(s) récupérée(s), dont 12 nouvelle(s).

• Développeur Python (H/F) — ACME
  75 - Paris (Dept.) | Contrat à durée indéterminée | Annuel de 45000.0 Euros à 55000.0 Euros
  https://candidat.francetravail.fr/offres/recherche/detail/190QJXZ

Détail complet écrit dans data/nouvelles.json
```

Pour une veille quotidienne, une entrée cron suffit :

```cron
0 8 * * * cd /chemin/vers/job-radar && uv run job-radar --silencieux
```

## Développement

```bash
uv sync           # installe les dépendances, dev comprises
uv run pytest     # tests (aucun appel réseau)
uv run ruff check .
uv run ruff format .
```

Structure du code :

```
src/job_radar/
├── client.py     # authentification OAuth2, recherche, réduction des offres
├── stockage.py   # historique SQLite des offres déjà vues
└── cli.py        # configuration, orchestration, affichage, sortie JSON
tests/
├── test_reduction.py   # réduction des offres brutes
└── test_nouvelles.py   # détection des offres jamais vues
```

## Notes sur l'API

- Jeton : `POST https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire`,
  scopes `api_offresdemploiv2 o2dsoffre`.
- Recherche : `GET https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search`.
- Pagination via le paramètre `range` (`0-149`), **150 offres maximum** par requête.
- Statuts `200` / `206` : résultats dans la clé `resultats`. Statut `204` : aucun résultat.
- Limite de **10 requêtes par seconde** ; le client espace ses appels en conséquence.

## Feuille de route

- **Étape 1 — veille brute (faite).** Recherche multi-critères, dédoublonnage, historique SQLite,
  affichage des seules nouvelles offres et export JSON.
- **Étape 2 — tri par LLM.** Passer `data/nouvelles.json` à `claude -p` pour noter chaque offre
  selon un profil cible (pertinence, séniorité, stack, signaux d'alerte) et ne garder que le haut
  du classement, avec une justification courte par offre.
- **Étape 3 — CV ciblé.** Dans un dépôt séparé, générer à partir des offres retenues un CV et une
  lettre adaptés à chaque annonce, à partir d'une base de contenus réutilisables.

## Licence

MIT.
