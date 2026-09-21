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
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from job_radar.llm import ClientLLM, ErreurLLM

#: Nombre d'offres envoyées en une fois au LLM.
TAILLE_LOT_DEFAUT = 20

#: Nombre de caractères de description transmis par offre.
LONGUEUR_DESCRIPTION = 800

#: Seuls champs transmis au LLM : de quoi juger l'offre, rien de plus.
CHAMPS_ENVOYES = ("intitule", "entreprise", "lieu", "contrat", "experience")

#: Années d'expérience à partir desquelles une exigence mérite d'être signalée.
SEUIL_EXPERIENCE_ANNEES = 3

#: Années d'expérience tolérées avant d'écarter l'offre. Au-delà, elle est écartée
#: sans appel au LLM ; entre :data:`SEUIL_EXPERIENCE_ANNEES` et cette valeur, elle
#: est conservée avec le drapeau ``experience``. La valeur par défaut écarte donc
#: dès trois ans, et le drapeau ne sert pas. Une veille plus large peut relever ce
#: plafond pour garder les offres et se contenter du signalement.
EXPERIENCE_MAX_DEFAUT = SEUIL_EXPERIENCE_ANNEES - 1

#: Codes ROME retenus par défaut, vérifiés dans le référentiel de France Travail :
#: « M18 » est la famille « Systèmes d'information et de télécommunication »
#: (M1801 à M1810 : administration, expertise et support, direction, réseaux de
#: télécoms, études et développement, conseil et maîtrise d'ouvrage, exploitation,
#: information géographique et météorologique, production et exploitation),
#: « K2107 » l'enseignement général du second degré et « K2111 » la formation
#: professionnelle. Un préfixe retient toute la famille, un code complet une seule fiche.
CODES_ROME_DEFAUT = ("M18", "K2107", "K2111")

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
#:
#: ``permis`` et ``bac5`` écartent l'offre au pré-filtre : ils n'ont plus à être
#: signalés. ``experience`` ne figure ici que lorsque le plafond toléré dépasse le
#: seuil de signalement : l'offre est alors gardée, mais l'exigence reste visible.
#: Le LLM peut toujours poser ces drapeaux sur des formulations que ces règles ne
#: couvrent pas, et la règle de verdict s'applique alors.
DRAPEAUX_DETERMINISTES = ("rqth", "experience")

#: Tous les drapeaux acceptés, d'où qu'ils viennent.
DRAPEAUX_VALIDES = (*DRAPEAUX_LLM, *DRAPEAUX_DETERMINISTES)

#: Drapeaux qui disqualifient une offre : affichés en rouge.
DRAPEAUX_ELIMINATOIRES = frozenset({"permis", "telephone", "experience", "bac5", "freelance"})

#: Drapeaux valorisants : mis en avant à l'affichage.
DRAPEAUX_BONUS = frozenset({"teletravail", "teletravail_complet"})

#: Drapeaux rédhibitoires : l'offre est « non », quel que soit le score du modèle.
#: `experience` n'en fait pas partie : une expérience demandée se négocie, pas un
#: permis, un bac+5 ou un statut indépendant.
DRAPEAUX_REDHIBITOIRES = frozenset({"permis", "telephone", "bac5", "freelance"})

VERDICTS = ("postuler", "peut-etre", "non")
VERDICTS_RETENUS = ("postuler", "peut-etre")

#: Entreprises et domaines qui signent une offre réservée aux travailleurs handicapés.
MARQUEURS_RQTH_ENTREPRISE = ("talents handicap",)
MARQUEURS_RQTH_URL = ("handicap-job.com",)

MOTIF_LIBERALE = "profession libérale (freelance)"
MOTIF_ROME = "hors des codes ROME retenus"
MOTIF_TJM = "rémunéré au jour (TJM, freelance)"


def motif_experience(experience_max: int = EXPERIENCE_MAX_DEFAUT) -> str:
    """Motif d'écartement, qui dit la durée réellement tolérée."""
    return f"plus de {experience_max} ans d'expérience exigés"


MOTIF_STAGE = "stage (convention impossible, diplôme déjà obtenu)"
MOTIF_PERMIS = "permis de conduire exigé"
MOTIF_TELETRAVAIL_PARTIEL = "télétravail partiel"
MOTIF_BAC5 = "bac+5 ou diplôme d'ingénieur exigé"
MOTIF_RQTH = "offre réservée aux travailleurs handicapés (exclure_rqth)"
MOTIF_DOUBLON = "doublon (même intitulé, même entreprise ou même ville)"


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


def code_rome_retenu(offre: dict[str, Any], codes: Sequence[str] = CODES_ROME_DEFAUT) -> bool:
    """Vrai si le code ROME de l'offre appartient à l'un des codes ou familles retenus.

    Une offre sans code ROME est conservée : les offres collectées avant que ce champ
    ne soit gardé n'en ont pas, et un filtre ne doit pas écarter ce qu'il ne sait pas
    juger. Une liste de codes vide désactive le filtre.
    """
    if not codes:
        return True

    rome = (offre.get("rome") or "").strip().upper()
    if not rome:
        return True

    return any(rome.startswith(code.strip().upper()) for code in codes)


#: Formulations qui annoncent un télétravail intégral. Les recherches par mots-clés
#: ramènent beaucoup d'offres simplement « ouvertes au télétravail » : seules ces
#: tournures disent que le poste se fait entièrement à distance.
REGEX_TELETRAVAIL_COMPLET = re.compile(
    r"\b100\s*%?\s*(?:de\s*)?(?:teletravail|remote|a distance|distanciel)\b"
    r"|\b(?:teletravail|remote|distanciel)\s*(?:a|:)?\s*100\s*%"
    r"|\bfull\s*(?:remote|teletravail|distanciel)\b"
    r"|\bfully\s*remote\b"
    r"|\bremote\s*(?:total|complet|integral|first)\w*\b"
    r"|\bteletravail\s*(?:total|complet|integral|permanent|exclusif)\w*\b"
    r"|\b(?:entierement|totalement|integralement|exclusivement|full)\s*"
    r"(?:en\s*)?(?:a\s*distance|teletravaille?|remote)\b"
    r"|\bteletravail\s*(?:de\s*)?5\s*(?:j|jours?)\s*(?:/|sur)\s*5\b"
    r"|\b5\s*(?:j|jours?)\s*(?:/|sur)\s*5\s*(?:de\s*)?teletravail\b"
    r"|\b(?:poste|mission|emploi)\s*(?:100\s*%\s*)?(?:en\s*)?remote\b"
)


def est_teletravail_complet(offre: dict[str, Any]) -> bool:
    """Vrai si l'annonce dit explicitement que le poste se fait entièrement à distance.

    « Télétravail possible » ou « deux jours par semaine » ne comptent pas : une
    recherche sur « full remote » ramène surtout des offres hybrides.
    """
    texte = _texte_offre(offre, "intitule", "description")
    return REGEX_TELETRAVAIL_COMPLET.search(texte) is not None


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
REGEX_MOT_STAGE = re.compile(r"\bstages?\b|\bstagiaires?\b")

#: Dans la description, le mot seul ne suffit pas : « la compréhension de vos stagiaires »
#: décrit un poste de formateur et « première expérience (stage, alternance) » un poste
#: ouvert aux débutants — deux offres à garder. Seules ces tournures disent que l'offre
#: EST un stage.
REGEX_STAGE_DESCRIPTION = re.compile(
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
    (voir :data:`REGEX_STAGE_DESCRIPTION`).
    """
    for champ in ("intitule", "url"):
        if REGEX_MOT_STAGE.search(_sans_accents(str(offre.get(champ) or ""))):
            return True

    description = _sans_accents(str(offre.get("description") or ""))
    return REGEX_STAGE_DESCRIPTION.search(description) is not None


#: « permis B obligatoire », « permis de conduire exigé »…
REGEX_PERMIS = re.compile(
    r"\bpermis\b(?: de conduire)?(?: [ab]\b)?[^.;!?]{0,40}"
    r"\b(obligatoire|exige\w*|requis\w*|indispensable|necessaire|imperatif)\b"
    r"|\b(obligatoire|exige\w*|requis\w*|indispensable)\b[^.;!?]{0,20}\bpermis\b"
)

#: Tournures qui disent l'inverse : le permis n'est pas demandé.
#: « permis/certification » est un intitulé de rubrique des annonces agrégées :
#: l'exigence qui suit porte sur la certification, pas sur la conduite.
REGEX_PERMIS_FACULTATIF = re.compile(
    r"\bpermis\s*/"
    r"|\b(sans|aucun|pas de|ni) permis\b"
    r"|\bpermis\b[^.;!?]{0,30}\b(non|pas) (obligatoire|exige\w*|requis\w*|necessaire)\b"
    r"|\bpermis\b[^.;!?]{0,20}\b(apprecie|souhaite|un plus|bienvenu)\w*\b"
)

#: Diplôme bac+5 demandé : le niveau seul ne suffit pas, il faut une exigence.
REGEX_BAC5 = re.compile(
    r"\b(bac\s*\+\s*5|bac\s*\+\s*[5-8]|master\s*2|master\s*ii|mastere|"
    r"ecole d.ingenieur\w*|diplome d.ingenieur|ingenieur diplome|doctorat)\b"
)

#: Une fourchette de niveaux (« de bac à bac+5 », « bac+3 à bac+5 ») n'exige pas
#: un bac+5 : elle décrit un éventail, souvent celui d'un public en formation.
REGEX_FOURCHETTE_DIPLOME = re.compile(
    r"\bbac\b\s*(\+\s*[0-4])?\s*(a|au|jusqu.a|et)\s*bac\s*\+\s*[5-8]"
    r"|\bbac\s*\+\s*[0-4]\s*(a|au|jusqu.a|et|/|-)\s*(\+\s*)?[5-8]\b"
    # « bac+3/bac+5 », « bac+3 - bac+5 » : « bac » répété de part et d'autre.
    r"|\bbac\s*\+\s*[0-4]\s*[/-]\s*bac\s*\+\s*[5-8]\b"
    # Énumération de niveaux proposés (catalogue d'école, offres d'alternance) :
    # « de niveau 4 à niveau 7 (bac, bac+2, bachelor/bac+3 ou mastère/bac+5) ».
    r"|\b(bac\s*\+\s*[0-3]|bachelor)\b[^.;!?]{0,40}\b(ou|et)\b\s*"
    r"(mastere|master|bac\s*\+\s*[5-8])"
    r"|\bniveau\s*[0-6]\s*(a|au)\s*niveau\s*[4-8]\b"
)

#: L'exigence, à proximité du diplôme (« de formation bac+5 », « master 2 exigé »).
REGEX_EXIGENCE_DIPLOME = re.compile(
    r"\b(exige\w*|requis\w*|obligatoire|imperatif\w*|minimum|demande|titulaire|"
    r"de formation|formation|niveau|diplome)\b"
)

#: Durées d'expérience exigées, dans toutes les tournures rencontrées sur les
#: annonces réelles. Chaque branche capture la borne basse de la durée : c'est elle
#: que le candidat doit atteindre. Les groupes sont lus par :func:`_premiere_annee`,
#: qui prend le premier groupe renseigné — inutile de compter les parenthèses.
REGEX_ANNEES_MINIMUM = re.compile(
    # « 3 ans minimum », « 3 à 5 ans minimum », « (5 ans minimum) »
    r"(\d+)\s*(?:a\s*\d+\s*)?ans?\b[^.;!?]{0,30}\b(?:minimum|mini|au minimum)\b"
    # « minimum 5 ans », « au moins 4 ans », « a minima 3 ans »
    r"|\b(?:minimum|au moins|a minima)\b\D{0,15}?(\d+)\s*ans?\b"
    # « environ 5 ans », « environ 5 a 7 ans »
    r"|\benviron\s*(\d+)\s*(?:a\s*\d+\s*)?ans?\b"
    # « 5 ans d'experience », « 5 ans d'exp. », « 5 annees d'experience »
    r"|(\d+)\s*(?:a\s*\d+\s*)?an(?:s|nees?)?\s*(?:et plus\s*)?d.exp\w*"
    # « au moins 7/8 d'experience », « minimum 5/6 ans » — la durée sans le mot « ans »
    r"|\b(?:minimum|au moins|a minima|environ)\s*(\d+)\s*/\s*\d+\b"
    # « experience de 5 ans et plus », « 5 ans ou plus »
    r"|(\d+)\s*ans?\s*(?:ou|et)\s*plus\b"
)

#: Tournures qui rendent la durée souhaitable plutôt qu'exigée : on ne les compte pas.
REGEX_EXPERIENCE_FACULTATIVE = re.compile(
    r"\b(apprecie|souhaite|un plus|bienvenu|serait un atout|atout)\w*\b"
)

#: Rémunération à la journée : marque une mission d'indépendant.
REGEX_TJM = re.compile(
    r"\btjm\b|\btaux journalier\b|(?:euros?|eur|\u20ac|k\u20ac)\s*(?:/|par |la )\s*jour"
    r"|\bpar jour travaille\b"
)


def _texte_offre(offre: dict[str, Any], *champs: str) -> str:
    """Concatène les champs demandés, sans accents et en minuscules."""
    return _sans_accents(" ".join(str(offre.get(champ) or "") for champ in champs))


def exige_permis(offre: dict[str, Any]) -> bool:
    """Vrai si l'annonce exige le permis de conduire.

    Les tournures qui le rendent facultatif (« permis apprécié », « sans permis »)
    l'emportent : mieux vaut laisser passer une offre que jeter une offre valable.
    """
    texte = _texte_offre(offre, "intitule", "description")
    if REGEX_PERMIS_FACULTATIF.search(texte):
        return False
    return REGEX_PERMIS.search(texte) is not None


def exige_bac5(offre: dict[str, Any]) -> bool:
    """Vrai si l'annonce demande un bac+5, un master 2 ou un diplôme d'ingénieur.

    La mention du diplôme ne suffit pas : elle doit être accompagnée d'une exigence
    dans la même phrase, sinon « parcours bac+3 à bac+5 » suffirait à écarter l'offre.
    """
    texte = _texte_offre(offre, "intitule", "description")

    for correspondance in REGEX_BAC5.finditer(texte):
        debut = max(0, correspondance.start() - 80)
        phrase = texte[debut : correspondance.end() + 40]
        if REGEX_FOURCHETTE_DIPLOME.search(phrase):
            continue
        if REGEX_EXIGENCE_DIPLOME.search(phrase):
            return True
    return False


def annees_dans_le_texte(offre: dict[str, Any]) -> int | None:
    """Vrai si la description réclame :data:`SEUIL_EXPERIENCE_ANNEES` ans au minimum.

    Complète :func:`exige_experience_longue`, qui ne lit que le libellé de l'API :
    beaucoup d'annonces affichent « débutant accepté » puis demandent « 5 ans minimum ».

    La description est lue **en entier** : l'exigence se trouve souvent au-delà des
    :data:`LONGUEUR_DESCRIPTION` premiers caractères, qui ne bornent que l'extrait
    envoyé au modèle. Les tournures couvertes sont « X ans minimum », « minimum X ans »
    et « au moins X ans », avec X exprimé en années.
    """
    texte = _texte_offre(offre, "description")
    exigences = []

    for correspondance in REGEX_ANNEES_MINIMUM.finditer(texte):
        annees = _premiere_annee(correspondance)
        if annees is None or annees < SEUIL_EXPERIENCE_ANNEES:
            continue
        # « 5 ans d'expérience appréciés » n'est pas une exigence.
        phrase = texte[correspondance.start() : correspondance.end() + 40]
        if REGEX_EXPERIENCE_FACULTATIVE.search(phrase):
            continue
        exigences.append(annees)

    return max(exigences) if exigences else None


def exige_experience_dans_le_texte(offre: dict[str, Any]) -> bool:
    """Vrai si la description réclame :data:`SEUIL_EXPERIENCE_ANNEES` ans ou plus."""
    return annees_dans_le_texte(offre) is not None


def annees_exigees(offre: dict[str, Any]) -> int | None:
    """Durée d'expérience demandée, la plus longue trouvée.

    Deux sources complémentaires : le libellé de l'API (« 3 An(s) ») et la description
    (« 5 ans minimum »), une annonce affichant souvent « débutant accepté » avant de
    réclamer cinq ans en clair.
    """
    durees = [
        duree
        for duree in (annees_experience(offre.get("experience")), annees_dans_le_texte(offre))
        if duree is not None
    ]
    return max(durees) if durees else None


def _premiere_annee(correspondance: re.Match[str]) -> int | None:
    """Renvoie le premier groupe chiffré renseigné d'une correspondance."""
    for groupe in correspondance.groups():
        if groupe:
            return int(groupe)
    return None


def est_remunere_au_jour(offre: dict[str, Any]) -> bool:
    """Vrai si la rémunération est un taux journalier : mission d'indépendant."""
    return REGEX_TJM.search(_texte_offre(offre, "salaire", "description")) is not None


def drapeaux_deterministes(
    offre: dict[str, Any], experience_max: int = EXPERIENCE_MAX_DEFAUT
) -> list[str]:
    """Drapeaux que Python pose seul, dans l'ordre de :data:`DRAPEAUX_DETERMINISTES`.

    Une offre retenue malgré une expérience demandée porte le drapeau ``experience`` :
    elle n'est pas écartée, mais l'exigence reste sous les yeux.
    """
    annees = annees_exigees(offre)
    detections = {
        "rqth": est_rqth(offre),
        # Entre le seuil de signalement et le plafond toléré : l'offre est gardée,
        # mais l'exigence reste sous les yeux. Au-delà, elle a déjà été écartée.
        "experience": annees is not None and SEUIL_EXPERIENCE_ANNEES <= annees <= experience_max,
    }
    return [drapeau for drapeau in DRAPEAUX_DETERMINISTES if detections[drapeau]]


#: Mentions de genre à retirer d'un intitulé avant comparaison.
REGEX_MENTION_GENRE = re.compile(r"\(?\b[hf]\s*/\s*[fh]\b\)?")

#: Préfixe de département d'un lieu : « 59 - Lille ».
REGEX_PREFIXE_DEPARTEMENT = re.compile(r"^\s*\d{2,3}\s*-\s*")


def _normaliser(texte: str) -> str:
    """Minuscules, sans accents, ponctuation réduite à des espaces simples."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", _sans_accents(texte)).split())


def normaliser_intitule(intitule: str | None) -> str:
    """Intitulé comparable : sans accents, sans « (H/F) », sans ponctuation."""
    return _normaliser(REGEX_MENTION_GENRE.sub(" ", _sans_accents(intitule or "")))


def normaliser_ville(lieu: str | None) -> str:
    """Ville comparable, débarrassée du préfixe de département de l'API."""
    return _normaliser(REGEX_PREFIXE_DEPARTEMENT.sub("", _sans_accents(lieu or "")))


def cles_doublon(offre: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    """Clés sous lesquelles une offre peut être reconnue comme déjà vue.

    Deux clés, parce que deux situations : la même annonce republiée par la même
    entreprise, et la même annonce diffusée par des intermédiaires différents —
    auquel cas seuls l'intitulé et la ville se ressemblent.
    """
    intitule = normaliser_intitule(offre.get("intitule"))
    return (
        ("entreprise", intitule, _normaliser(offre.get("entreprise") or "")),
        ("ville", intitule, normaliser_ville(offre.get("lieu"))),
    )


def prefiltrer(
    offres: Iterable[dict[str, Any]],
    exclure_rqth: bool = False,
    codes_rome: Sequence[str] = CODES_ROME_DEFAUT,
    experience_max: int = EXPERIENCE_MAX_DEFAUT,
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """Sépare les offres à envoyer au LLM de celles écartées, avec leur motif.

    Aucun appel réseau ni LLM : ce filtre ne coûte rien et retire l'essentiel
    du bruit avant de dépenser du quota. Avec ``exclure_rqth``, les offres des
    canaux réservés aux travailleurs handicapés sont écartées ; sinon elles sont
    conservées et porteront le drapeau ``rqth``.

    Une offre rémunérée au taux journalier est écartée comme une profession
    libérale : le contrat annoncé a beau être un CDI, la mission est celle d'un
    indépendant.

    ``codes_rome`` restreint la veille à des familles de métiers (voir
    :func:`code_rome_retenu`).

    Les exigences que Python sait lire — permis, bac+5, durée d'expérience — écartent
    l'offre ici plutôt que de la faire noter puis recaler par la règle de verdict :
    autant ne pas la payer.

    Une offre portant ``teletravail_complet_exige`` vient d'une recherche par mots-clés
    de télétravail : elle n'est gardée que si son texte annonce un travail entièrement
    à distance (voir :func:`est_teletravail_complet`).
    """
    retenues: list[dict[str, Any]] = []
    ecartees: list[tuple[dict[str, Any], str]] = []
    deja_vues: set[tuple[str, ...]] = set()

    for offre in offres:
        # Provenance posée à la collecte : la règle ne vaut que pour les offres
        # ramenées par une recherche « télétravail complet ».
        if offre.get("teletravail_complet_exige") and not est_teletravail_complet(offre):
            ecartees.append((offre, MOTIF_TELETRAVAIL_PARTIEL))
            continue
        if not code_rome_retenu(offre, codes_rome):
            ecartees.append((offre, MOTIF_ROME))
            continue
        if est_profession_liberale(offre):
            ecartees.append((offre, MOTIF_LIBERALE))
            continue
        if est_stage(offre):
            ecartees.append((offre, MOTIF_STAGE))
            continue
        # Le libellé de l'API et la description se complètent : beaucoup d'annonces
        # affichent « débutant accepté » puis réclament « 5 ans minimum » en clair.
        annees = annees_exigees(offre)
        if annees is not None and annees > experience_max:
            ecartees.append((offre, motif_experience(experience_max)))
            continue
        if est_remunere_au_jour(offre):
            ecartees.append((offre, MOTIF_TJM))
            continue
        if exige_permis(offre):
            ecartees.append((offre, MOTIF_PERMIS))
            continue
        if exige_bac5(offre):
            ecartees.append((offre, MOTIF_BAC5))
            continue
        if exclure_rqth and est_rqth(offre):
            ecartees.append((offre, MOTIF_RQTH))
            continue

        cles = cles_doublon(offre)
        if any(cle in deja_vues for cle in cles):
            ecartees.append((offre, MOTIF_DOUBLON))
            continue

        deja_vues.update(cles)
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
- Si l'annonce dit explicitement accueillir les débutants ou privilégier la capacité
  d'apprentissage, ne pénalise pas l'ampleur des missions : une liste de technologies
  longue n'est pas un obstacle quand l'employeur annonce ne pas tout exiger.
- Une offre qui mérite l'un des drapeaux "permis", "telephone", "bac5" ou "freelance"
  sera de toute façon classée "non" : ne lui donne pas le verdict "postuler".

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
    lot: list[dict[str, Any]],
    resultats: list[dict[str, Any]],
    experience_max: int = EXPERIENCE_MAX_DEFAUT,
) -> list[dict[str, Any]]:
    """Ajoute aux résultats les drapeaux que Python sait poser seul.

    Ces drapeaux s'ajoutent à ceux du modèle : les règles Python ne couvrent que
    des formulations précises, le modèle attrape le reste.
    """
    par_id = {offre["id"]: offre for offre in lot}

    for resultat in resultats:
        offre = par_id.get(resultat["id"])
        if offre is None:
            continue
        for drapeau in drapeaux_deterministes(offre, experience_max):
            if drapeau not in resultat["drapeaux"]:
                resultat["drapeaux"].append(drapeau)

    return resultats


def appliquer_regle_verdict(resultats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Force le verdict « non » sur les offres portant un drapeau rédhibitoire.

    Le score du modèle est conservé tel quel : il dit l'intérêt du poste, pas
    l'accessibilité. Un permis, un bac+5 ou un statut indépendant ferment la porte,
    quel que soit cet intérêt.
    """
    for resultat in resultats:
        if DRAPEAUX_REDHIBITOIRES.intersection(resultat["drapeaux"]):
            resultat["verdict"] = "non"

    return resultats


def trier_lot(
    client: ClientLLM,
    criteres: str,
    lot: list[dict[str, Any]],
    experience_max: int = EXPERIENCE_MAX_DEFAUT,
) -> list[dict[str, Any]]:
    """Fait noter un lot par le LLM, avec une seule nouvelle tentative si besoin."""
    prompt = construire_prompt(criteres, lot)
    ids = [offre["id"] for offre in lot]

    try:
        notees = valider_reponse(client(prompt), ids)
    except ErreurReponseLLM:
        # Une réponse mal formée est souvent un accident : on réessaie une fois,
        # puis on abandonne ce lot pour ne pas bloquer les suivants.
        notees = valider_reponse(client(prompt), ids)

    return appliquer_regle_verdict(ajouter_drapeaux_deterministes(lot, notees, experience_max))


def trier(
    client: ClientLLM,
    criteres: str,
    offres: list[dict[str, Any]],
    taille_lot: int = TAILLE_LOT_DEFAUT,
    experience_max: int = EXPERIENCE_MAX_DEFAUT,
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
            notees = trier_lot(client, criteres, lot, experience_max)
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
