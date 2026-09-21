# job-radar

Outil de veille d'offres d'emploi en ligne de commande, branché sur l'API officielle
**France Travail « Offres d'emploi v2 »**.

`job-radar collecter` interroge l'API selon les critères de `config.toml`, compare les résultats
à un historique local et n'affiche **que les offres jamais vues**. `job-radar trier` fait ensuite
noter ces offres par un LLM selon vos propres critères, et n'en garde que celles qui valent
une candidature.

## Fonctionnement

### Collecte (`job-radar collecter`)

1. Authentification OAuth2 (*client credentials*) auprès de France Travail. En cas de refus,
   l'outil s'arrête immédiatement plutôt que de retenter chaque recherche.
2. Une recherche par couple **mot-clé × département** — ou une seule recherche par mot-clé sur
   toute la France si aucun département n'est configuré —, puis dédoublonnage par identifiant.
   Les offres sont demandées triées par **date de création décroissante** (`sort=1`), donc la
   première page contient les annonces les plus récentes.
3. Réduction de chaque offre aux champs utiles : intitulé, lieu, entreprise, contrat, salaire,
   expérience, alternance, date de création, URL d'origine et description.
4. Comparaison à l'historique SQLite (`data/offres.db`) : les offres déjà vues sont écartées.
5. Affichage des nouveautés dans le terminal et écriture du détail dans `data/nouvelles.json`.

### Tri (`job-radar trier`)

1. **Pré-filtre en Python, gratuit** : sont écartées sans aucun appel au LLM les offres en
   « Profession libérale », celles **rémunérées au taux journalier** (TJM, `€/jour` : le
   contrat annoncé a beau être un CDI, la mission est celle d'un indépendant), les **stages**
   (impossible de signer une convention quand on est déjà diplômé), celles qui exigent
   explicitement 3 ans d'expérience ou plus, et les **doublons**. Le détail des offres
   écartées et de leur motif est affiché.

   Le **dédoublonnage** joue sur deux clés : même intitulé normalisé + même entreprise (annonce
   republiée), et même intitulé normalisé + même ville (même annonce diffusée par des
   intermédiaires différents — `MANPOWER` et `Randstad` pour le même poste à Mérignac).
   L'intitulé est normalisé sans accents, sans casse, sans `(H/F)` ni ponctuation ; la ville
   sans le préfixe de département de l'API (`59 - Lille` → `lille`).

   Plusieurs **drapeaux sont posés en Python**, sans LLM, parce qu'ils sont vérifiables
   mécaniquement — ils s'ajoutent à ceux du modèle, qui attrape les formulations non couvertes :
   - **`rqth`** : entreprise contenant « Talents Handicap » ou URL sur `handicap-job.com`.
     Avec `exclure_rqth = true`, ces offres sont écartées ; sinon elles portent le drapeau.
   - **`permis`** : « permis B obligatoire / exigé / nécessaire ». Les tournures qui le rendent
     facultatif l'emportent, et `permis/certification` — une rubrique des annonces agrégées —
     est ignoré : l'exigence qui suit porte sur la certification.
   - **`bac5`** : bac+5, master 2, mastère, école ou diplôme d'ingénieur, doctorat, à condition
     qu'une exigence accompagne le diplôme. Une fourchette (`bac+3 à bac+5`) ou une énumération
     de niveaux proposés (`bac, bac+2, bachelor/bac+3 ou mastère/bac+5`, typique des offres
     d'alternance) n'est pas une exigence.
   - **`experience`** : « X ans minimum », « minimum X ans », « au moins X ans » avec X ≥ 3 dans
     la description — utile quand l'annonce affiche « débutant accepté » puis demande 5 ans.
   - **stage** : le mot « stage » ou « stagiaire » dans l'intitulé ou l'URL, ou une tournure
     de la description qui désigne l'offre elle-même (« en tant que stagiaire », « offre de
     stage », « le stagiaire sera… »). Le mot seul dans la description ne suffit pas : un poste
     de formateur parle des stagiaires qu'il encadre, et une offre junior cite le stage parmi
     les premières expériences acceptées — deux offres à garder.
2. Les offres restantes partent par **lots de 20** à `claude -p --output-format json`, avec
   seulement l'intitulé, l'entreprise, le lieu, le contrat, l'expérience et les **800 premiers
   caractères** de la description.
3. Le modèle renvoie, pour chaque offre, un **score sur 100**, un résumé de deux lignes, des
   **drapeaux** et un **verdict** (`postuler`, `peut-etre`, `non`). Le prompt définit chaque
   drapeau (ce qu'il signifie *et* ce qu'il ne signifie pas), donne trois exemples d'offres
   notées, et impose un tri sévère : `postuler` seulement si le candidat a une chance réelle
   avec son diplôme et son expérience, et un intitulé « confirmé », « senior », « expert » ou
   « lead » fait chuter le score même quand l'offre affiche « débutant accepté ». La réponse JSON est validée ;
   en cas de réponse inexploitable, une seule nouvelle tentative, puis le lot est signalé en
   erreur sans bloquer les autres. Un lot qui dépasse `delai_max` secondes est abandonné de la
   même façon. Le temps écoulé est affiché pour chaque lot.
4. **Règle de verdict** : une offre portant un drapeau rédhibitoire — `permis`, `telephone`,
   `bac5` ou `freelance` — est classée `non`, quel que soit le score du modèle. Le score est
   conservé tel quel : il dit l'intérêt du poste, pas son accessibilité. `experience` n'est pas
   rédhibitoire : une expérience demandée se négocie, pas un permis ou un bac+5.
5. Les verdicts sont stockés en SQLite : **une offre n'est jamais triée deux fois**.
6. Affichage d'un tableau trié par score décroissant et écriture de `data/selection.json`,
   qui contient **toute** la sélection accumulée (offres `postuler` et `peut-etre`, relues
   depuis la base) : trier par tranches avec `--limite` complète le fichier au lieu de
   l'écraser.

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
departements = ["75", "92"]   # liste vide = recherche sur toute la France
publiee_depuis = 7            # 1, 3, 7, 14 ou 31 jours
pages_max = 1                 # pages de 150 offres par recherche
```

Quelques règles utiles :

- **`departements = []`** (ou clé absente) lance une seule requête par mot-clé, sans filtre
  géographique : la veille porte alors sur la France entière.
- Un **mot-clé peut contenir plusieurs mots** (`"alternance data"`) : l'API accepte les
  expressions, l'espace faisant partie des caractères autorisés. Chaque mot-clé doit faire au
  moins 2 caractères.
- La **virgule est interdite** dans un mot-clé : côté API, elle sépare les mots-clés. Pour
  chercher deux choses, écrivez deux entrées dans la liste. Un mot-clé invalide est refusé dès
  la lecture de `config.toml`, avant tout appel réseau.

Le tri par LLM se règle dans le même fichier :

```toml
[tri]
criteres = "~/Documents/cv/criteres-tri.md"   # fichier Markdown, hors du dépôt
modele = "sonnet"                             # modèle passé à `claude -p --model`
taille_lot = 20                               # offres envoyées en une fois
delai_max = 180                               # secondes par lot, au-delà le lot est abandonné
exclure_rqth = false                          # true : écarter les offres réservées RQTH
```

### Fichier de critères

Le tri s'appuie sur un fichier Markdown qui décrit votre profil, les postes visés, vos
contraintes éliminatoires et vos bonus. Son contenu part tel quel en tête du prompt : plus il est
précis, meilleur est le tri. Il vit **hors du dépôt** parce qu'il est personnel ;
[`criteres.example.md`](criteres.example.md) en donne un exemple anonyme à copier :

```bash
cp criteres.example.md ~/Documents/cv/criteres-tri.md
```

Le tri suppose la CLI [Claude Code](https://claude.com/claude-code) installée et authentifiée
(`claude`), puisque l'appel se fait via `claude -p`.

L'appel est volontairement réduit à un **simple appel de modèle** : aucun outil
(`--tools ""`), aucun serveur MCP (`--strict-mcp-config`), aucune skill
(`--disable-slash-commands`), rien qui puisse réclamer une autorisation
(`--permission-prompts none`), pas de session écrite sur le disque
(`--no-session-persistence`), ni `CLAUDE.md` ni hooks hérités du dossier courant
(`--safe-mode`), un prompt système minimal et le raisonnement étendu coupé
(`MAX_THINKING_TOKENS=0`). Sur un lot de 20 offres, cela fait passer l'appel de
**64 s à environ 19 s** et l'en-tête de **31 400 à 7 900 jetons** (mesuré avec `haiku`).

## Usage

```bash
uv run job-radar collecter   # ou simplement « uv run job-radar »
uv run job-radar trier
```

### `collecter`

Options disponibles :

| Option | Rôle |
| --- | --- |
| `--config CHEMIN` | autre fichier de critères (défaut `config.toml`) |
| `--base CHEMIN` | autre base d'historique (défaut `data/offres.db`) |
| `--sortie CHEMIN` | autre fichier JSON de sortie (défaut `data/nouvelles.json`) |
| `--sans-historique` | afficher les offres sans les marquer comme vues |
| `--silencieux` | masquer le détail de progression des recherches |

Codes de retour : `0` succès, `1` erreur d'exécution, `2` configuration ou identifiants
manquants, `3` authentification refusée par France Travail.

Exemple de sortie :

```
Recherche : 2 mot(s)-clé × 2 département(s), offres publiées depuis 7 jour(s), triées par date de création décroissante
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
0 8 * * * cd /chemin/vers/job-radar && uv run job-radar collecter --silencieux
```

### `trier`

| Option | Rôle |
| --- | --- |
| `--config CHEMIN` | autre fichier de critères (défaut `config.toml`) |
| `--base CHEMIN` | autre base d'historique (défaut `data/offres.db`) |
| `--sortie CHEMIN` | autre fichier JSON de sélection (défaut `data/selection.json`) |
| `--limite N` | ne trier que les N offres les plus récentes (pour tester sans brûler de quota) |
| `--simulation` | annoncer combien d'offres et de lots partiraient, sans rien envoyer |
| `--reinitialiser` | effacer les résultats de tri (les offres sont conservées) puis retrier |
| `--modele NOM` | modèle à utiliser, au lieu de celui de `config.toml` |
| `--importer FICHIER` | compléter l'historique avec un export JSON (bases d'avant l'étape 2) |

Commencer par une simulation, qui ne coûte rien :

```
$ uv run job-radar trier --simulation
995 offre(s) à trier : 481 retenue(s) par le pré-filtre, 514 écartée(s) sans appel au LLM.
  - 248 × 3 ans d'expérience ou plus exigés
  - 188 × profession libérale (freelance)
  - 78 × doublon (même intitulé, même entreprise)

Simulation : 481 offre(s) partiraient au modèle sonnet en 25 lot(s) de 20 au maximum.
Rien n'a été envoyé.
```

Puis trier pour de vrai, éventuellement par petites tranches :

```
$ uv run job-radar trier --limite 20
20 offre(s) à trier : 13 retenue(s) par le pré-filtre, 7 écartée(s) sans appel au LLM.
  - 4 × 3 ans d'expérience ou plus exigés
  - 3 × profession libérale (freelance)

Tri de 13 offre(s) par sonnet en 1 lot(s), 180 s au plus par lot :
  lot 1/1 (13 offres) → sonnet…
  lot 1/1 : 13 offre(s) notée(s) en 18.7 s
  total : 18.7 s

                      Offres triées par score décroissant
┏━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━┓
┃ Score ┃ Verdict   ┃ Intitulé                ┃ Entreprise   ┃ Lieu       ┃ Drapeaux  ┃ Résumé ┃
┡━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━┩
│    85 │ postuler  │ Enseignant SII (H/F)    │ RECTORAT     │ 57 -       │           │ …      │
│    75 │ postuler  │ Chargé(e) de test H/F   │ MAISON MGA   │ 42 - Mably │ experien… │ …      │
│    15 │ non       │ TECHNICIEN SUPPORT      │ Randstad     │ 34 -       │ telephone │ …      │
└───────┴───────────┴─────────────────────────┴──────────────┴────────────┴───────────┴────────┘

10 offre(s) retenue(s) sur 13 triée(s) → data/selection.json
```

Le verdict `postuler` s'affiche en vert, `peut-etre` en jaune, `non` en gris. Les drapeaux
**éliminatoires** (`permis`, `telephone`, `experience`, `bac5`, `freelance`) sortent en rouge,
le **télétravail** (`teletravail`, `teletravail_complet`) en vert gras.

Après un changement de critères ou de prompt, on peut tout retrier :

```bash
uv run job-radar trier --reinitialiser --limite 20             # efface, puis retrie
uv run job-radar trier --reinitialiser --simulation            # n'efface rien, montre le volume
uv run job-radar trier --limite 20 --modele sonnet             # comparer deux modèles
```

Les offres déjà triées ne repartent jamais au LLM : relancer la commande ne traite que les
nouveautés.

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
├── stockage.py   # historique SQLite : offres vues (table offres) et triées (table tri)
├── llm.py        # unique point de contact avec un LLM (`claude -p`)
├── tri.py        # pré-filtre gratuit, lots, prompt, validation des réponses
└── cli.py        # configuration, sous-commandes, affichage rich, sorties JSON
tests/
├── faux_reseau.py      # doublures de session HTTP, de client API et de LLM
├── test_reduction.py   # réduction des offres brutes
├── test_nouvelles.py   # détection des offres jamais vues, stockage du tri
├── test_recherche.py   # tri API, département optionnel, mots-clés, authentification
├── test_config.py      # lecture de config.toml et orchestration des recherches
├── test_tri.py         # pré-filtre, lots, prompt, validation, reprise sur erreur
└── test_cli_tri.py     # sous-commande « trier » de bout en bout
```

Aucun test ne fait d'appel réseau ni d'appel réel au LLM.

## Notes sur l'API

- Jeton : `POST https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire`,
  scopes `api_offresdemploiv2 o2dsoffre`.
- Recherche : `GET https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search`.
- Pagination via le paramètre `range` (`0-149`), **150 offres maximum** par requête. La
  documentation borne l'index du premier élément à 1000 et celui du dernier à 1149, soit
  7 pages exploitables au maximum.
- `sort` : `0` = pertinence décroissante, **`1` = date de création décroissante**,
  `2` = distance croissante. `job-radar` utilise toujours `1`.
- `motsCles` : jusqu'à 7 mots-clés séparés par des virgules, chacun d'au moins 2 caractères.
  Caractères autorisés : lettres, chiffres, espace et ``@#$%^&+./-"``. Une expression de
  plusieurs mots est donc valide ; `job-radar` envoie un seul mot-clé par requête.
- `departement` : paramètre facultatif ; omis, la recherche couvre toute la France.
- Statuts `200` / `206` : résultats dans la clé `resultats`. Statut `204` : aucun résultat.
- Limite de **10 requêtes par seconde** ; le client espace ses appels en conséquence.

## Feuille de route

- **Étape 1 — veille brute (faite).** Recherche multi-critères, dédoublonnage, historique SQLite,
  affichage des seules nouvelles offres et export JSON.
- **Étape 2 — tri par LLM (faite).** Pré-filtre gratuit, notation par lots via `claude -p` selon
  un fichier de critères personnel, verdicts stockés en SQLite, tableau `rich` et
  `data/selection.json`.
- **Étape 3 — CV ciblé.** Dans un dépôt séparé, générer à partir de `data/selection.json` un CV
  et une lettre adaptés à chaque annonce retenue, depuis une base de contenus réutilisables.

Pistes pour la suite : remplacer `claude -p` par l'API Claude (il suffit de réécrire
`src/job_radar/llm.py`), et rejouer le tri d'une offre quand les critères changent.

## Licence

MIT.
