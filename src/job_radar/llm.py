"""Unique point de contact avec un LLM.

Tout le reste du projet ne connaît qu'une chose : une fonction qui prend un prompt
et renvoie du texte (:data:`ClientLLM`). Remplacer l'appel à ``claude -p`` par l'API
Claude ne demande donc que de réécrire ce module.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable

#: Un client LLM : du texte en entrée, du texte en sortie. Rien d'autre.
ClientLLM = Callable[[str], str]

COMMANDE = "claude"
MODELE_DEFAUT = "haiku"

#: Un lot de 20 offres peut demander une réponse longue : on laisse le temps au modèle.
TIMEOUT_DEFAUT = 300


class ErreurLLM(RuntimeError):
    """Le LLM n'a pas pu être interrogé, ou a répondu par une erreur."""


def interroger(prompt: str, modele: str = MODELE_DEFAUT, timeout: int = TIMEOUT_DEFAUT) -> str:
    """Envoie ``prompt`` à ``claude -p`` et renvoie la réponse textuelle du modèle.

    Le prompt passe par l'entrée standard : il peut dépasser la taille maximale
    d'un argument de ligne de commande.
    """
    if shutil.which(COMMANDE) is None:
        raise ErreurLLM(
            f"commande {COMMANDE!r} introuvable : installez Claude Code "
            "(https://claude.com/claude-code) pour utiliser le tri par LLM"
        )

    argv = [COMMANDE, "-p", "--output-format", "json", "--model", modele]
    try:
        processus = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as erreur:
        raise ErreurLLM(f"pas de réponse de {COMMANDE} après {timeout} s") from erreur

    if processus.returncode != 0:
        raise ErreurLLM(
            f"{COMMANDE} a échoué (code {processus.returncode}) : "
            f"{(processus.stderr or processus.stdout)[:300]}"
        )

    return _extraire_resultat(processus.stdout)


def _extraire_resultat(sortie: str) -> str:
    """Extrait le texte du modèle de l'enveloppe JSON renvoyée par ``--output-format json``."""
    try:
        enveloppe = json.loads(sortie)
    except json.JSONDecodeError as erreur:
        raise ErreurLLM(f"réponse de {COMMANDE} illisible : {sortie[:300]}") from erreur

    if isinstance(enveloppe, dict):
        if enveloppe.get("is_error"):
            raise ErreurLLM(f"{COMMANDE} signale une erreur : {str(enveloppe)[:300]}")
        resultat = enveloppe.get("result")
        if isinstance(resultat, str):
            return resultat

    raise ErreurLLM(f"réponse de {COMMANDE} inattendue : {sortie[:300]}")


def creer_client(modele: str = MODELE_DEFAUT, timeout: int = TIMEOUT_DEFAUT) -> ClientLLM:
    """Renvoie un :data:`ClientLLM` figé sur un modèle, prêt à être injecté."""

    def client(prompt: str) -> str:
        return interroger(prompt, modele=modele, timeout=timeout)

    return client
