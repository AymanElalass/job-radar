"""Unique point de contact avec un LLM.

Tout le reste du projet ne connaît qu'une chose : une fonction qui prend un prompt
et renvoie du texte (:data:`ClientLLM`). Remplacer l'appel à ``claude -p`` par l'API
Claude ne demande donc que de réécrire ce module.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable

#: Un client LLM : du texte en entrée, du texte en sortie. Rien d'autre.
ClientLLM = Callable[[str], str]

COMMANDE = "claude"
MODELE_DEFAUT = "haiku"

#: Délai maximal par appel, en secondes. Au-delà, l'appel est abandonné.
DELAI_MAX_DEFAUT = 180

#: Options qui réduisent ``claude -p`` à un simple appel de modèle.
#:
#: Sans elles, la CLI monte une session complète de Claude Code : outils, serveurs
#: MCP configurés par l'utilisateur, skills, hooks, CLAUDE.md du dossier courant.
#: Tout cela gonfle le prompt (mesuré : plus de 24 000 jetons d'en-tête pour un lot
#: qui n'en demande que 7 000) et, si le modèle décide d'appeler un outil, l'appel
#: peut attendre une autorisation que personne ne donnera jamais.
OPTIONS_ISOLEMENT = (
    # Aucun outil : le modèle ne peut que répondre du texte.
    "--tools",
    "",
    # Rien ne peut réclamer une autorisation : ce qui en demanderait est refusé.
    "--permission-prompts",
    "none",
    # Ignorer les serveurs MCP de l'utilisateur (aucun n'est fourni ici).
    "--strict-mcp-config",
    # Pas de skills ni de commandes personnalisées.
    "--disable-slash-commands",
    # Ne pas écrire de session sur le disque : ces appels ne sont pas une conversation.
    "--no-session-persistence",
    # Pas de CLAUDE.md, de hooks ni de plugins hérités du dossier courant.
    "--safe-mode",
)

#: Consigne système minimale, à la place du long prompt système de Claude Code.
PROMPT_SYSTEME = (
    "Tu es un assistant de classement. Tu réponds uniquement par le JSON demandé, "
    "sans texte autour."
)

#: Variables d'environnement imposées au sous-processus.
#:
#: ``MAX_THINKING_TOKENS=0`` coupe le raisonnement étendu. C'est le levier décisif :
#: sur un lot de 20 offres, il représentait 5 000 à 14 000 jetons de sortie et
#: l'essentiel du temps d'attente (mesuré : 64 s puis 145 s avec, 19 s sans), alors
#: que classer des offres selon des critères écrits n'en a pas besoin.
ENVIRONNEMENT = {"MAX_THINKING_TOKENS": "0"}


class ErreurLLM(RuntimeError):
    """Le LLM n'a pas pu être interrogé, ou a répondu par une erreur."""


def construire_argv(modele: str) -> list[str]:
    """Construit la ligne de commande : un appel de modèle, sans outil ni session."""
    return [
        COMMANDE,
        "-p",
        "--output-format",
        "json",
        "--model",
        modele,
        "--system-prompt",
        PROMPT_SYSTEME,
        *OPTIONS_ISOLEMENT,
    ]


def interroger(prompt: str, modele: str = MODELE_DEFAUT, delai_max: int = DELAI_MAX_DEFAUT) -> str:
    """Envoie ``prompt`` à ``claude -p`` et renvoie la réponse textuelle du modèle.

    Le prompt passe par l'entrée standard : il peut dépasser la taille maximale
    d'un argument de ligne de commande. Au-delà de ``delai_max`` secondes, l'appel
    est abandonné et :class:`ErreurLLM` est levée.
    """
    if shutil.which(COMMANDE) is None:
        raise ErreurLLM(
            f"commande {COMMANDE!r} introuvable : installez Claude Code "
            "(https://claude.com/claude-code) pour utiliser le tri par LLM"
        )

    try:
        processus = subprocess.run(
            construire_argv(modele),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=delai_max,
            check=False,
            env={**os.environ, **ENVIRONNEMENT},
        )
    except subprocess.TimeoutExpired as erreur:
        raise ErreurLLM(
            f"délai dépassé : pas de réponse de {COMMANDE} après {delai_max} s"
        ) from erreur

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


def creer_client(modele: str = MODELE_DEFAUT, delai_max: int = DELAI_MAX_DEFAUT) -> ClientLLM:
    """Renvoie un :data:`ClientLLM` figé sur un modèle et un délai, prêt à être injecté."""

    def client(prompt: str) -> str:
        return interroger(prompt, modele=modele, delai_max=delai_max)

    return client
