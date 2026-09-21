"""Tests du module LLM : ligne de commande, isolement, délai, enveloppe JSON.

Aucun appel réel : ``subprocess.run`` est remplacé par une doublure.
"""

import json
import subprocess

import pytest

from job_radar import llm


class FauxProcessus:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def enveloppe(resultat: str) -> str:
    return json.dumps({"type": "result", "is_error": False, "result": resultat})


@pytest.fixture
def appel(monkeypatch):
    """Capture l'appel système au lieu de lancer « claude »."""
    capture = {}

    def faux_run(argv, **kwargs):
        capture["argv"] = argv
        capture["kwargs"] = kwargs
        return FauxProcessus(stdout=enveloppe("réponse du modèle"))

    monkeypatch.setattr(llm.shutil, "which", lambda _commande: "/usr/bin/claude")
    monkeypatch.setattr(llm.subprocess, "run", faux_run)
    return capture


# ------------------------------------------------------- ligne de commande


def test_appel_de_base(appel):
    assert llm.interroger("bonjour", modele="haiku") == "réponse du modèle"

    argv = appel["argv"]
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--output-format") + 1] == "json"


def test_prompt_envoye_sur_l_entree_standard(appel):
    llm.interroger("mon prompt")

    assert appel["kwargs"]["input"] == "mon prompt"


def test_aucun_outil_disponible(appel):
    llm.interroger("bonjour")

    argv = appel["argv"]
    # `--tools ""` : le modèle ne peut que répondre du texte.
    assert argv[argv.index("--tools") + 1] == ""


def test_rien_ne_peut_demander_une_autorisation(appel):
    llm.interroger("bonjour")

    argv = appel["argv"]
    assert argv[argv.index("--permission-prompts") + 1] == "none"


@pytest.mark.parametrize(
    "option",
    [
        "--strict-mcp-config",  # aucun serveur MCP de l'utilisateur
        "--disable-slash-commands",  # aucune skill
        "--no-session-persistence",  # aucune session écrite
        "--safe-mode",  # ni CLAUDE.md, ni hooks, ni plugins
    ],
)
def test_options_d_isolement_presentes(appel, option):
    llm.interroger("bonjour")

    assert option in appel["argv"]


def test_prompt_systeme_minimal(appel):
    llm.interroger("bonjour")

    argv = appel["argv"]
    assert argv[argv.index("--system-prompt") + 1] == llm.PROMPT_SYSTEME


def test_raisonnement_etendu_desactive(appel):
    llm.interroger("bonjour")

    assert appel["kwargs"]["env"]["MAX_THINKING_TOKENS"] == "0"


def test_environnement_existant_conserve(appel, monkeypatch):
    monkeypatch.setenv("PATH", "/chemin/de/test")

    llm.interroger("bonjour")

    assert appel["kwargs"]["env"]["PATH"] == "/chemin/de/test"


# ---------------------------------------------------------------- délai


def test_delai_transmis_au_sous_processus(appel):
    llm.interroger("bonjour", delai_max=42)

    assert appel["kwargs"]["timeout"] == 42


def test_modele_par_defaut(appel):
    llm.interroger("bonjour")

    argv = appel["argv"]
    assert argv[argv.index("--model") + 1] == llm.MODELE_DEFAUT == "sonnet"


def test_delai_par_defaut_de_180_secondes(appel):
    llm.interroger("bonjour")

    assert appel["kwargs"]["timeout"] == llm.DELAI_MAX_DEFAUT == 180


def test_delai_depasse_leve_une_erreur_explicite(monkeypatch):
    def run_qui_expire(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=180)

    monkeypatch.setattr(llm.shutil, "which", lambda _c: "/usr/bin/claude")
    monkeypatch.setattr(llm.subprocess, "run", run_qui_expire)

    with pytest.raises(llm.ErreurLLM, match="délai dépassé"):
        llm.interroger("bonjour", delai_max=180)


# ------------------------------------------------------- enveloppe et erreurs


def test_commande_absente_signalee(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _c: None)

    with pytest.raises(llm.ErreurLLM, match="introuvable"):
        llm.interroger("bonjour")


def test_code_de_retour_non_nul_signale(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _c: "/usr/bin/claude")
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda *_a, **_k: FauxProcessus(returncode=1, stderr="authentification requise"),
    )

    with pytest.raises(llm.ErreurLLM, match="authentification requise"):
        llm.interroger("bonjour")


def test_enveloppe_illisible_signalee(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _c: "/usr/bin/claude")
    monkeypatch.setattr(
        llm.subprocess, "run", lambda *_a, **_k: FauxProcessus(stdout="pas du JSON")
    )

    with pytest.raises(llm.ErreurLLM, match="illisible"):
        llm.interroger("bonjour")


def test_erreur_signalee_par_la_cli(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _c: "/usr/bin/claude")
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda *_a, **_k: FauxProcessus(
            stdout=json.dumps({"is_error": True, "result": "quota dépassé"})
        ),
    )

    with pytest.raises(llm.ErreurLLM, match="signale une erreur"):
        llm.interroger("bonjour")


def test_client_fige_modele_et_delai(appel):
    client = llm.creer_client("sonnet", delai_max=60)

    assert client("bonjour") == "réponse du modèle"
    assert appel["argv"][appel["argv"].index("--model") + 1] == "sonnet"
    assert appel["kwargs"]["timeout"] == 60
