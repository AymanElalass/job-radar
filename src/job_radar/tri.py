"""Tri des offres : pré-filtre gratuit en Python, puis notation par LLM.

Le pré-filtre écarte sans rien dépenser ce qui est de toute façon disqualifié
(freelance, expérience longue, doublons). Seules les offres restantes partent
au LLM, par lots, avec un extrait de leur description.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from job_radar.llm import ClientLLM, ErreurLLM

#: Nombre d'offres envoyées en une fois au LLM.
TAILLE_LOT_DEFAUT = 20

#: Nombre de caractères de description transmis par offre.
LONGUEUR_DESCRIPTION = 800

#: Seuls champs transmis au LLM : de quoi juger l'offre, rien de plus.
CHAMPS_ENVOYES = ("intitule", "entreprise", "lieu", "contrat", "experience")

#: Années d'expérience à partir desquelles une offre est écartée sans LLM.
SEUIL_EXPERIENCE_ANNEES = 3

#: Drapeaux que le LLM peut poser sur une offre.
DRAPEAUX_LLM = (
    "permis",
    "telephone",
    "experience",
    "bac5",
    "freelance",
    "alternance",
    "teletravail",
    "teletravail_complet",
)

#: Drapeaux posés par Python, sans LLM, parce qu'ils sont vérifiables mécaniquement.
DRAPEAUX_DETERMINISTES = ("rqth",)

#: Tous les drapeaux acceptés, d'où qu'ils viennent.
DRAPEAUX_VALIDES = (*DRAPEAUX_LLM, *DRAPEAUX_DETERMINISTES)

#: Drapeaux qui disqualifient une offre : affichés en rouge.
DRAPEAUX_ELIMINATOIRES = frozenset({"permis", "telephone", "experience", "bac5", "freelance"})

#: Drapeaux valorisants : mis en avant à l'affichage.
DRAPEAUX_BONUS = frozenset({"teletravail", "teletravail_complet"})

VERDICTS = ("postuler", "peut-etre", "non")
VERDICTS_RETENUS = ("postuler", "peut-etre")

#: Entreprises et domaines qui signent une offre réservée aux travailleurs handicapés.
MARQUEURS_RQTH_ENTREPRISE = ("talents handicap",)
MARQUEURS_RQTH_URL = ("handicap-job.com",)

MOTIF_LIBERALE = "profession libérale (freelance)"
MOTIF_EXPERIENCE = f"{SEUIL_EXPERIENCE_ANNEES} ans d'expérience ou plus exigés"
MOTIF_STAGE = "stage (convention impossible, diplôme déjà obtenu)"
MOTIF_RQTH = "offre réservée aux travailleurs handicapés (exclure_rqth)"
MOTIF_DOUBLON = "doublon (même intitulé, même entreprise)"


class ErreurTri(RuntimeError):
    """Le tri n'a pas pu être mené à bien."""


class ErreurReponseLLM(ErreurTri):
    """Le LLM a répondu autre chose que le JSON attendu."""


# --------------------------------------------------------------------- critères


def charger_criteres(chemin: str | Path) -> str:
    """Charge le fichier Markdown de critères (hors dépôt, propre à l'utilisateur)."""
    chemin = Path(chemin).expanduser()
    if not chemin.is_file():
        raise ErreurTri(
            f"fichier de critères introuvable : {chemin}. Renseignez [tri].criteres "
            "dans config.toml (voir criteres.example.md)."
        )

    criteres = chemin.read_text(encoding="utf-8").strip()
    if not criteres:
        raise ErreurTri(f"fichier de critères vide : {chemin}")
    return criteres


# -------------------------------------------------------------------- pré-filtre


def _sans_accents(texte: str) -> str:
    decompose = unicodedata.normalize("NFD", texte)
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn").casefold()


def est_profession_liberale(offre: dict[str, Any]) -> bool:
    """Vrai si l'offre est un statut indépendant plutôt qu'un emploi salarié."""
    return "profession liberale" in _sans_accents(offre.get("contrat") or "")


def annees_experience(libelle: str | None) -> int | None:
    """Années d'expérience demandées, lues dans le libellé de l'API.

    L'API écrit « 3 An(s) », parfois « 36 Mois », ou rien de chiffré du tout
    (« Débutant accepté », « Expérience exigée ») : on renvoie alors ``None``.
    """
    if not libelle:
        return None

    correspondance = re.search(r"(\d+)\s*(an|mois)", _sans_accents(libelle))
    if not correspondance:
        return None

    valeur = int(correspondance.group(1))
    return valeur if correspondance.group(2) == "an" else valeur // 12


def exige_experience_longue(offre: dict[str, Any]) -> bool:
    """Vrai si l'offre demande explicitement :data:`SEUIL_EXPERIENCE_ANNEES` ans ou plus."""
    annees = annees_experience(offre.get("experience"))
    return annees is not None and annees >= SEUIL_EXPERIENCE_ANNEES


def est_rqth(offre: dict[str, Any]) -> bool:
    """Vrai si l'offre passe par un canal réservé aux travailleurs handicapés.

    Vérifiable mécaniquement : l'entreprise porte « Talents Handicap », ou l'offre
    vient de handicap-job.com. Inutile de demander au LLM ce qu'un `in` suffit à voir.
    """
    entreprise = _sans_accents(offre.get("entreprise") or "")
    url = (offre.get("url") or "").casefold()

    return any(marqueur in entreprise for marqueur in MARQUEURS_RQTH_ENTREPRISE) or any(
        marqueur in url for marqueur in MARQUEURS_RQTH_URL
    )


#: Le mot « stage » dans l'intitulé ou l'URL désigne l'offre elle-même.
MOTIF_MOT_STAGE = re.compile(r"\bstages?\b|\bstagiaires?\b")

#: Dans la description, le mot seul ne suffit pas : « la compréhension de vos stagiaires »
#: décrit un poste de formateur et « première expérience (stage, alternance) » un poste
#: ouvert aux débutants — deux offres à garder. Seules ces tournures disent que l'offre
#: EST un stage.
MOTIFS_STAGE_DESCRIPTION = re.compile(
    r"\ben tant que stagiaire\b"
    r"|\bvous serez stagiaire\b"
    r"|\b(offre|contrat|convention|type de contrat) (de |d'|: ?)?stage\b"
    r"|\bstage (de |d')?\d+ (mois|semaines?)\b"
    r"|\b(recherch|recrut|propos)\w* (un|une|des) (stagiaire|stage)\b"
    r"|\bstage (conventionne|obligatoire|de fin d)\w*"
    r"|\b(le|la|notre) stagiaire\b"
)


def est_stage(offre: dict[str, Any]) -> bool:
    """Vrai si l'offre est un stage.

    Un stage suppose une convention avec un établissement, impossible pour un
    candidat déjà diplômé. L'intitulé et l'URL sont pris au mot ; la description,
    elle, n'est retenue que sur des tournures qui désignent l'offre elle-même
    (voir :data:`MOTIFS_STAGE_DESCRIPTION`).
    """
    for champ in ("intitule", "url"):
        if MOTIF_MOT_STAGE.search(_sans_accents(str(offre.get(champ) or ""))):
            return True

    description = _sans_accents(str(offre.get("description") or ""))
    return MOTIFS_STAGE_DESCRIPTION.search(description) is not None


def _cle_doublon(offre: dict[str, Any]) -> tuple[str, str]:
    return (
        _sans_accents(offre.get("intitule") or "").strip(),
        _sans_accents(offre.get("entreprise") or "").strip(),
    )


def prefiltrer(
    offres: Iterable[dict[str, Any]],
    exclure_rqth: bool = False,
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """Sépare les offres à envoyer au LLM de celles écartées, avec leur motif.

    Aucun appel réseau ni LLM : ce filtre ne coûte rien et retire l'essentiel
    du bruit avant de dépenser du quota. Avec ``exclure_rqth``, les offres des
    canaux réservés aux travailleurs handicapés sont écartées ; sinon elles sont
    conservées et porteront le drapeau ``rqth``.
    """
    retenues: list[dict[str, Any]] = []
    ecartees: list[tuple[dict[str, Any], str]] = []
    deja_vues: set[tuple[str, str]] = set()

    for offre in offres:
        if est_profession_liberale(offre):
            ecartees.append((offre, MOTIF_LIBERALE))
            continue
        if est_stage(offre):
            ecartees.append((offre, MOTIF_STAGE))
            continue
        if exige_experience_longue(offre):
            ecartees.append((offre, MOTIF_EXPERIENCE))
            continue
        if exclure_rqth and est_rqth(offre):
            ecartees.append((offre, MOTIF_RQTH))
            continue

        cle = _cle_doublon(offre)
        if cle in deja_vues:
            ecartees.append((offre, MOTIF_DOUBLON))
            continue

        deja_vues.add(cle)
        retenues.append(offre)

    return retenues, ecartees


# ------------------------------------------------------------------------ lots


def decouper_en_lots(
    offres: list[dict[str, Any]], taille: int = TAILLE_LOT_DEFAUT
) -> Iterator[list[dict[str, Any]]]:
    """Découpe les offres en lots de ``taille`` au plus."""
    if taille < 1:
        raise ValueError(f"taille de lot invalide : {taille}")
    for debut in range(0, len(offres), taille):
        yield offres[debut : debut + taille]


def resumer_pour_llm(offre: dict[str, Any]) -> dict[str, Any]:
    """Réduit une offre à ce qui est utile au jugement, description tronquée."""
    resume = {"id": offre.get("id")}
    resume.update({champ: offre.get(champ) for champ in CHAMPS_ENVOYES})
    description = (offre.get("description") or "").strip()
    resume["description"] = description[:LONGUEUR_DESCRIPTION]
    return resume


#: Mots d'un intitulé qui trahissent un poste non junior, quoi que dise l'offre.
MOTS_SENIORITE = ("confirme", "senior", "expert", "lead")

DEFINITIONS_DRAPEAUX = """\
- "permis" : le permis de conduire ou un véhicule est exigé, ou le poste implique des
  déplacements que seul un conducteur peut assurer. Ne pas poser ce drapeau parce que
  l'offre est loin ou mal desservie.
- "telephone" : le travail consiste en tout ou partie à appeler ou être appelé (hotline,
  standard, télévente, support téléphonique). Ne pas poser ce drapeau parce qu'un numéro
  figure dans l'annonce, ni pour un poste où le téléphone est accessoire.
- "experience" : une expérience professionnelle significative est exigée, ou l'annonce
  décrit des attendus qu'un débutant ne peut pas tenir. Poser ce drapeau même si le
  champ « expérience » indique « débutant accepté » quand le texte dit le contraire.
- "bac5" : un diplôme bac+5, un master, un diplôme d'ingénieur ou un doctorat est exigé.
  Ne pas poser ce drapeau si bac+2 ou bac+3 suffit, ni si le diplôme n'est pas précisé.
- "freelance" : le poste suppose un statut indépendant, une auto-entreprise, un portage
  ou une commission plutôt qu'un salaire.
- "alternance" : le poste est en alternance, en apprentissage ou en contrat de
  professionnalisation.
- "teletravail" : du télétravail partiel est explicitement proposé (jours par semaine,
  hybride). Ne pas poser ce drapeau si l'annonce n'en parle pas.
- "teletravail_complet" : le poste est intégralement à distance ou en full remote.
"""

EXEMPLES = """\
Exemple noté « postuler » :
  {"intitule": "Chargé de recette applicative (H/F)", "experience": "Débutant accepté",
   "description": "Vous exécutez les cas de test, rédigez les anomalies, formation assurée."}
  → {"score": 88, "verdict": "postuler", "drapeaux": [],
     "resume": "Recette applicative junior, formation assurée : correspond au poste visé."}

Exemple noté « peut-etre » :
  {"intitule": "Technicien support informatique (H/F)", "experience": "2 An(s)",
   "description": "Support de proximité, installation de postes, gestion des tickets."}
  → {"score": 52, "verdict": "peut-etre", "drapeaux": ["experience"],
     "resume": "Support de proximité à l'écrit et sur site, mais deux ans d'expérience demandés."}

Exemple noté « non » :
  {"intitule": "Développeur Java confirmé (H/F)", "experience": "Débutant accepté",
   "description": "Vous concevez l'architecture des microservices et encadrez deux juniors."}
  → {"score": 12, "verdict": "non", "drapeaux": ["experience"],
     "resume": "Poste confirmé avec encadrement et architecture : hors de portée sans expérience."}
"""

CONSIGNES = f"""Tu tries des offres d'emploi pour un candidat, selon ses critères.

Critères du candidat :
---
{{criteres}}
---

Sois SÉVÈRE. Le but n'est pas de trouver quelque chose à dire sur chaque offre, mais
d'isoler les rares offres où ce candidat a une vraie chance :

- "postuler" UNIQUEMENT si le candidat a une chance réelle d'être retenu avec une
  licence (bac+3) et sans expérience professionnelle. En cas de doute, "peut-etre".
- Un intitulé contenant {list(MOTS_SENIORITE)} désigne un poste non junior : le score
  baisse fortement, même si le champ « expérience » indique « débutant accepté », et le
  verdict ne peut pas être "postuler".
- Un poste qui exige des compétences ou des responsabilités qu'un débutant ne peut pas
  tenir n'est pas "postuler", quoi qu'affiche le champ « expérience ».
- Un métier sans rapport avec le profil se note bas, sans chercher de rapprochement.

Pour chacune des {{nombre}} offres ci-dessous, produis un objet JSON avec :
- "id" : l'identifiant de l'offre, recopié tel quel
- "score" : entier de 0 à 100, chance réelle d'être retenu et intérêt du poste
- "resume" : deux lignes maximum, en français, ce que fait le poste et pourquoi il colle ou non
- "drapeaux" : liste, éventuellement vide, choisie STRICTEMENT parmi {list(DRAPEAUX_LLM)}.
  Un drapeau se pose sur ce que l'annonce dit, pas sur une supposition :
{DEFINITIONS_DRAPEAUX}
- "verdict" : "postuler", "peut-etre" ou "non"

{EXEMPLES}
Réponds UNIQUEMENT par un tableau JSON de {{nombre}} objets, sans texte autour,
sans bloc de code, sans commentaire.

Offres :
{{offres}}
"""


def construire_prompt(criteres: str, lot: list[dict[str, Any]]) -> str:
    """Assemble le prompt d'un lot : critères du candidat + offres réduites.

    La substitution est faite par remplacement et non par ``format`` : le prompt
    contient des exemples JSON, donc des accolades que ``format`` interpréterait.
    """
    offres = [resumer_pour_llm(offre) for offre in lot]
    return (
        CONSIGNES.replace("{criteres}", criteres)
        .replace("{nombre}", str(len(lot)))
        .replace("{offres}", json.dumps(offres, ensure_ascii=False, indent=2))
    )


# ------------------------------------------------------------------ validation


def _extraire_tableau_json(texte: str) -> Any:
    """Isole le tableau JSON d'une réponse, même entourée de texte ou de balises."""
    nettoye = texte.strip()
    nettoye = re.sub(r"^```(?:json)?|```$", "", nettoye, flags=re.MULTILINE).strip()

    debut, fin = nettoye.find("["), nettoye.rfind("]")
    if debut == -1 or fin <= debut:
        raise ErreurReponseLLM(f"aucun tableau JSON dans la réponse : {texte[:200]!r}")

    try:
        return json.loads(nettoye[debut : fin + 1])
    except json.JSONDecodeError as erreur:
        raise ErreurReponseLLM(f"JSON invalide : {erreur}") from erreur


def valider_reponse(texte: str, ids_attendus: Iterable[str]) -> list[dict[str, Any]]:
    """Valide la réponse du LLM et renvoie les résultats normalisés.

    Les drapeaux inconnus sont ignorés — une invention du modèle sur ce point ne
    justifie pas de jeter un lot entier. Tout le reste (identifiant inconnu, score
    hors bornes, verdict inattendu) invalide la réponse.
    """
    ids_attendus = set(ids_attendus)
    charge = _extraire_tableau_json(texte)

    if not isinstance(charge, list) or not charge:
        raise ErreurReponseLLM("la réponse n'est pas un tableau JSON non vide")

    resultats: list[dict[str, Any]] = []
    for element in charge:
        if not isinstance(element, dict):
            raise ErreurReponseLLM(f"élément de réponse inattendu : {element!r}")

        identifiant = element.get("id")
        if identifiant not in ids_attendus:
            raise ErreurReponseLLM(f"identifiant d'offre inconnu : {identifiant!r}")

        try:
            score = int(element["score"])
        except (KeyError, TypeError, ValueError) as erreur:
            raise ErreurReponseLLM(f"score absent ou non entier pour {identifiant!r}") from erreur
        if not 0 <= score <= 100:
            raise ErreurReponseLLM(f"score hors bornes pour {identifiant!r} : {score}")

        verdict = _sans_accents(str(element.get("verdict") or "")).strip()
        if verdict not in VERDICTS:
            raise ErreurReponseLLM(
                f"verdict inattendu pour {identifiant!r} : {element.get('verdict')!r}"
            )

        drapeaux_bruts = element.get("drapeaux") or []
        if not isinstance(drapeaux_bruts, list):
            raise ErreurReponseLLM(f"drapeaux non listés pour {identifiant!r}")

        resultats.append(
            {
                "id": identifiant,
                "score": score,
                "resume": _deux_lignes(element.get("resume")),
                "drapeaux": [d for d in drapeaux_bruts if d in DRAPEAUX_VALIDES],
                "verdict": verdict,
            }
        )

    return resultats


def _deux_lignes(resume: Any) -> str:
    """Ramène un résumé à deux lignes au plus."""
    if not isinstance(resume, str):
        return ""
    lignes = [ligne.strip() for ligne in resume.splitlines() if ligne.strip()]
    return "\n".join(lignes[:2])


# ------------------------------------------------------------------------- tri


def ajouter_drapeaux_deterministes(
    lot: list[dict[str, Any]], resultats: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Ajoute aux résultats les drapeaux que Python sait poser seul.

    Le LLM ne se prononce pas sur ``rqth`` : l'information est dans l'entreprise ou
    l'URL de l'offre, donc vérifiable sans lui — et sans risque d'oubli.
    """
    par_id = {offre["id"]: offre for offre in lot}

    for resultat in resultats:
        offre = par_id.get(resultat["id"])
        if offre is not None and est_rqth(offre) and "rqth" not in resultat["drapeaux"]:
            resultat["drapeaux"].append("rqth")

    return resultats


def trier_lot(client: ClientLLM, criteres: str, lot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fait noter un lot par le LLM, avec une seule nouvelle tentative si besoin."""
    prompt = construire_prompt(criteres, lot)
    ids = [offre["id"] for offre in lot]

    try:
        notees = valider_reponse(client(prompt), ids)
    except ErreurReponseLLM:
        # Une réponse mal formée est souvent un accident : on réessaie une fois,
        # puis on abandonne ce lot pour ne pas bloquer les suivants.
        notees = valider_reponse(client(prompt), ids)

    return ajouter_drapeaux_deterministes(lot, notees)


def trier(
    client: ClientLLM,
    criteres: str,
    offres: list[dict[str, Any]],
    taille_lot: int = TAILLE_LOT_DEFAUT,
    rappel_debut: Callable[[int, int, int], None] | None = None,
    rappel_fin: Callable[[int, int, float, int, str | None], None] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Trie toutes les offres, lot par lot.

    Renvoie les résultats obtenus et la liste des lots en erreur (décrits en clair).
    Un lot en échec — réponse inexploitable comme délai dépassé — ne compromet pas
    les autres. ``rappel_debut`` est appelé avant chaque lot, ``rappel_fin`` après,
    avec le temps écoulé.
    """
    resultats: list[dict[str, Any]] = []
    erreurs: list[str] = []

    lots = list(decouper_en_lots(offres, taille_lot))
    for numero, lot in enumerate(lots, start=1):
        if rappel_debut is not None:
            rappel_debut(numero, len(lots), len(lot))

        depart = time.monotonic()
        notees: list[dict[str, Any]] = []
        motif: str | None = None
        try:
            notees = trier_lot(client, criteres, lot)
        except (ErreurTri, ErreurLLM) as erreur:
            # Un lot abandonné (JSON inexploitable, délai dépassé, CLI en échec)
            # est signalé, et les lots suivants partent quand même.
            motif = str(erreur)
            erreurs.append(f"lot {numero}/{len(lots)} ({len(lot)} offres) : {motif}")

        duree = time.monotonic() - depart
        resultats.extend(notees)
        if rappel_fin is not None:
            rappel_fin(numero, len(lots), duree, len(notees), motif)

    return resultats, erreurs


def fusionner(
    offres: list[dict[str, Any]], resultats: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Associe chaque résultat de tri à son offre, trié par score décroissant."""
    par_id = {offre["id"]: offre for offre in offres}
    fusionnees = [
        {**par_id[resultat["id"]], **resultat} for resultat in resultats if resultat["id"] in par_id
    ]
    return sorted(fusionnees, key=lambda offre: offre["score"], reverse=True)
