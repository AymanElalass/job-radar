"""Tri des offres : pré-filtre gratuit en Python, puis notation par LLM.

Le pré-filtre écarte sans rien dépenser ce qui est de toute façon disqualifié
(freelance, expérience longue, doublons). Seules les offres restantes partent
au LLM, par lots, avec un extrait de leur description.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from job_radar.llm import ClientLLM

#: Nombre d'offres envoyées en une fois au LLM.
TAILLE_LOT_DEFAUT = 20

#: Nombre de caractères de description transmis par offre.
LONGUEUR_DESCRIPTION = 800

#: Seuls champs transmis au LLM : de quoi juger l'offre, rien de plus.
CHAMPS_ENVOYES = ("intitule", "entreprise", "lieu", "contrat", "experience")

#: Années d'expérience à partir desquelles une offre est écartée sans LLM.
SEUIL_EXPERIENCE_ANNEES = 3

#: Drapeaux que le LLM peut poser sur une offre.
DRAPEAUX_VALIDES = (
    "permis",
    "telephone",
    "experience",
    "bac5",
    "freelance",
    "rqth",
    "stage_deguise",
    "alternance",
    "teletravail",
    "teletravail_complet",
)

#: Drapeaux qui disqualifient une offre : affichés en rouge.
DRAPEAUX_ELIMINATOIRES = frozenset(
    {"permis", "telephone", "experience", "bac5", "freelance", "stage_deguise"}
)

#: Drapeaux valorisants : mis en avant à l'affichage.
DRAPEAUX_BONUS = frozenset({"teletravail", "teletravail_complet"})

VERDICTS = ("postuler", "peut-etre", "non")
VERDICTS_RETENUS = ("postuler", "peut-etre")

MOTIF_LIBERALE = "profession libérale (freelance)"
MOTIF_EXPERIENCE = f"{SEUIL_EXPERIENCE_ANNEES} ans d'expérience ou plus exigés"
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


def _cle_doublon(offre: dict[str, Any]) -> tuple[str, str]:
    return (
        _sans_accents(offre.get("intitule") or "").strip(),
        _sans_accents(offre.get("entreprise") or "").strip(),
    )


def prefiltrer(
    offres: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """Sépare les offres à envoyer au LLM de celles écartées, avec leur motif.

    Aucun appel réseau ni LLM : ce filtre ne coûte rien et retire l'essentiel
    du bruit avant de dépenser du quota.
    """
    retenues: list[dict[str, Any]] = []
    ecartees: list[tuple[dict[str, Any], str]] = []
    deja_vues: set[tuple[str, str]] = set()

    for offre in offres:
        if est_profession_liberale(offre):
            ecartees.append((offre, MOTIF_LIBERALE))
            continue
        if exige_experience_longue(offre):
            ecartees.append((offre, MOTIF_EXPERIENCE))
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


CONSIGNES = f"""Tu tries des offres d'emploi pour un candidat, selon ses critères.

Critères du candidat :
---
{{criteres}}
---

Pour chacune des {{nombre}} offres ci-dessous, produis un objet JSON avec :
- "id" : l'identifiant de l'offre, recopié tel quel
- "score" : entier de 0 à 100, adéquation avec les critères (100 = idéal)
- "resume" : deux lignes maximum, en français, ce que fait le poste et pourquoi il colle ou non
- "drapeaux" : liste, éventuellement vide, choisie STRICTEMENT parmi
  {list(DRAPEAUX_VALIDES)}
  ("permis" = permis de conduire exigé, "telephone" = travail au téléphone,
  "experience" = expérience significative exigée, "bac5" = bac+5 ou plus exigé,
  "freelance" = statut indépendant, "rqth" = poste ouvert aux travailleurs handicapés,
  "stage_deguise" = poste junior aux responsabilités de senior, "alternance" = alternance,
  "teletravail" = télétravail partiel, "teletravail_complet" = télétravail intégral)
- "verdict" : "postuler", "peut-etre" ou "non"

Réponds UNIQUEMENT par un tableau JSON de {{nombre}} objets, sans texte autour,
sans bloc de code, sans commentaire.

Offres :
{{offres}}
"""


def construire_prompt(criteres: str, lot: list[dict[str, Any]]) -> str:
    """Assemble le prompt d'un lot : critères du candidat + offres réduites."""
    offres = [resumer_pour_llm(offre) for offre in lot]
    return CONSIGNES.format(
        criteres=criteres,
        nombre=len(lot),
        offres=json.dumps(offres, ensure_ascii=False, indent=2),
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


def trier_lot(client: ClientLLM, criteres: str, lot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fait noter un lot par le LLM, avec une seule nouvelle tentative si besoin."""
    prompt = construire_prompt(criteres, lot)
    ids = [offre["id"] for offre in lot]

    try:
        return valider_reponse(client(prompt), ids)
    except ErreurReponseLLM:
        # Une réponse mal formée est souvent un accident : on réessaie une fois,
        # puis on abandonne ce lot pour ne pas bloquer les suivants.
        return valider_reponse(client(prompt), ids)


def trier(
    client: ClientLLM,
    criteres: str,
    offres: list[dict[str, Any]],
    taille_lot: int = TAILLE_LOT_DEFAUT,
    rappel: Callable[[int, int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Trie toutes les offres, lot par lot.

    Renvoie les résultats obtenus et la liste des lots en erreur (décrits en clair).
    Un lot en échec ne compromet pas les autres.
    """
    resultats: list[dict[str, Any]] = []
    erreurs: list[str] = []

    lots = list(decouper_en_lots(offres, taille_lot))
    for numero, lot in enumerate(lots, start=1):
        if rappel is not None:
            rappel(numero, len(lots), len(lot))
        try:
            resultats.extend(trier_lot(client, criteres, lot))
        except ErreurTri as erreur:
            erreurs.append(f"lot {numero}/{len(lots)} ({len(lot)} offres) : {erreur}")

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
