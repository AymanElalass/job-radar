"""Tests de la sous-commande ``job-radar trier`` (aucun appel réel au LLM)."""

import json

import pytest

from faux_reseau import FauxLLM
from job_radar import cli
from job_radar.stockage import Historique

CRITERES = "# Critères\nTesteur QA junior, pas de permis."


def offre(identifiant, **champs):
    base = {
        "id": identifiant,
        "intitule": f"Testeur QA {identifiant}",
        "entreprise": f"ACME {identifiant}",
        "lieu": "59 - Lille",
        "contrat": "CDI",
        "experience": "Débutant accepté",
        "salaire": None,
        "alternance": False,
        "date_creation": "2026-09-20T09:00:00.000Z",
        "url": f"https://exemple.fr/{identifiant}",
        "description": "Vous testez des applications web.",
    }
    return base | champs


@pytest.fixture
def projet(tmp_path):
    """Prépare un mini-projet : config, critères, base peuplée."""
    criteres = tmp_path / "criteres.md"
    criteres.write_text(CRITERES, encoding="utf-8")

    config = tmp_path / "config.toml"
    config.write_text(
        f"""
        [recherche]
        mots_cles = ["testeur"]

        [tri]
        criteres = "{criteres}"
        modele = "haiku"
        taille_lot = 2
        """,
        encoding="utf-8",
    )

    base = tmp_path / "offres.db"
    with Historique(base) as historique:
        historique.enregistrer([offre("A"), offre("B"), offre("C")])

    return {
        "config": config,
        "base": base,
        "sortie": tmp_path / "selection.json",
        "criteres": criteres,
    }


def reponse(*elements):
    return json.dumps(list(elements), ensure_ascii=False)


def arguments(projet, *extra):
    return [
        "trier",
        "--config",
        str(projet["config"]),
        "--base",
        str(projet["base"]),
        "--sortie",
        str(projet["sortie"]),
        *extra,
    ]


def test_simulation_n_appelle_pas_le_llm(projet, monkeypatch, capsys):
    def interdit(*_args, **_kwargs):
        raise AssertionError("le LLM ne doit pas être appelé en simulation")

    monkeypatch.setattr(cli, "creer_client", interdit)

    assert cli.main(arguments(projet, "--simulation")) == 0

    sortie = capsys.readouterr().out
    assert "Simulation" in sortie
    assert "3 offre(s) partiraient" in sortie
    assert "2 lot(s)" in sortie
    assert not projet["sortie"].exists()


def test_tri_complet_ecrit_la_selection(projet, monkeypatch, capsys):
    client = FauxLLM(
        reponses=[
            reponse(
                {
                    "id": "A",
                    "score": 90,
                    "resume": "Idéal",
                    "drapeaux": ["teletravail"],
                    "verdict": "postuler",
                },
                {"id": "B", "score": 55, "resume": "Bof", "drapeaux": [], "verdict": "peut-etre"},
            ),
            reponse(
                {"id": "C", "score": 10, "resume": "Non", "drapeaux": ["permis"], "verdict": "non"}
            ),
        ]
    )
    monkeypatch.setattr(cli, "creer_client", lambda *_args, **_kwargs: client)

    assert cli.main(arguments(projet)) == 0

    selection = json.loads(projet["sortie"].read_text(encoding="utf-8"))
    assert [offre["id"] for offre in selection] == ["A", "B"]
    assert selection[0]["score"] == 90
    assert selection[0]["url"] == "https://exemple.fr/A"

    sortie = capsys.readouterr().out
    assert "90" in sortie and "postuler" in sortie
    assert "2 offre(s) retenue(s) sur 3 triée(s)" in sortie


def test_les_offres_deja_triees_ne_repartent_pas(projet, monkeypatch):
    with Historique(projet["base"]) as historique:
        historique.enregistrer_tri(
            [{"id": "A", "score": 90, "resume": "", "drapeaux": [], "verdict": "postuler"}],
            modele="haiku",
        )

    client = FauxLLM(
        reponses=[
            reponse(
                {"id": "B", "score": 50, "resume": "", "drapeaux": [], "verdict": "peut-etre"},
                {"id": "C", "score": 20, "resume": "", "drapeaux": [], "verdict": "non"},
            )
        ]
    )
    monkeypatch.setattr(cli, "creer_client", lambda *_args, **_kwargs: client)

    assert cli.main(arguments(projet)) == 0

    envoyes = client.prompts[0]
    assert '"B"' in envoyes and '"C"' in envoyes
    assert '"A"' not in envoyes


def test_option_limite(projet, monkeypatch, capsys):
    monkeypatch.setattr(cli, "creer_client", lambda *_a, **_k: FauxLLM())

    assert cli.main(arguments(projet, "--limite", "1", "--simulation")) == 0

    assert "1 offre(s) partiraient" in capsys.readouterr().out


def test_prefiltre_detaille_les_offres_ecartees(projet, monkeypatch, capsys):
    with Historique(projet["base"]) as historique:
        historique.enregistrer(
            [
                offre("D", contrat="Profession libérale"),
                offre("E", experience="5 An(s)"),
            ]
        )
    monkeypatch.setattr(cli, "creer_client", lambda *_a, **_k: FauxLLM())

    cli.main(arguments(projet, "--simulation"))

    sortie = capsys.readouterr().out
    assert "2 écartée(s) sans" in sortie
    assert "profession libérale" in sortie
    assert "3 ans d'expérience ou plus" in sortie


def test_criteres_manquants_signales(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('[recherche]\nmots_cles = ["testeur"]\n', encoding="utf-8")

    code = cli.main(["trier", "--config", str(config), "--base", str(tmp_path / "db.sqlite")])

    assert code == 2
    assert "[tri].criteres" in capsys.readouterr().err


def test_fichier_de_criteres_introuvable_signale(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text(
        '[recherche]\nmots_cles = ["testeur"]\n\n[tri]\ncriteres = "/inexistant/criteres.md"\n',
        encoding="utf-8",
    )

    code = cli.main(["trier", "--config", str(config), "--base", str(tmp_path / "db.sqlite")])

    assert code == 2
    assert "introuvable" in capsys.readouterr().err


def test_modele_de_la_ligne_de_commande_prioritaire(projet, monkeypatch, capsys):
    monkeypatch.setattr(cli, "creer_client", lambda *_a, **_k: FauxLLM())

    cli.main(arguments(projet, "--simulation", "--modele", "sonnet"))

    assert "sonnet" in capsys.readouterr().out


def test_aucune_offre_a_trier(tmp_path, projet, monkeypatch, capsys):
    with Historique(projet["base"]) as historique:
        historique.enregistrer_tri(
            [
                {"id": ident, "score": 1, "resume": "", "drapeaux": [], "verdict": "non"}
                for ident in ("A", "B", "C")
            ],
            modele="haiku",
        )
    monkeypatch.setattr(cli, "creer_client", lambda *_a, **_k: FauxLLM())

    assert cli.main(arguments(projet)) == 0
    assert "Aucune offre à trier" in capsys.readouterr().out


def test_commande_par_defaut_reste_la_collecte():
    assert cli._argv_normalise([]) == ["collecter"]
    assert cli._argv_normalise(["--silencieux"]) == ["collecter", "--silencieux"]
    assert cli._argv_normalise(["trier", "--limite", "5"]) == ["trier", "--limite", "5"]
    assert cli._argv_normalise(["collecter"]) == ["collecter"]
    assert cli._argv_normalise(["--help"]) == ["--help"]


def test_reinitialiser_efface_les_resultats_et_retrie(projet, monkeypatch, capsys):
    with Historique(projet["base"]) as historique:
        historique.enregistrer_tri(
            [
                {"id": ident, "score": 1, "resume": "", "drapeaux": [], "verdict": "non"}
                for ident in ("A", "B", "C")
            ],
            modele="haiku",
        )

    client = FauxLLM(
        reponses=[
            reponse(
                {"id": "A", "score": 80, "resume": "", "drapeaux": [], "verdict": "postuler"},
                {"id": "B", "score": 50, "resume": "", "drapeaux": [], "verdict": "peut-etre"},
            ),
            reponse({"id": "C", "score": 5, "resume": "", "drapeaux": [], "verdict": "non"}),
        ]
    )
    monkeypatch.setattr(cli, "creer_client", lambda *_a, **_k: client)

    assert cli.main(arguments(projet, "--reinitialiser")) == 0

    sortie = capsys.readouterr().out
    assert "3 résultat(s) de tri effacé(s)" in sortie
    # Les trois offres sont bien reparties au tri, avec de nouveaux verdicts.
    with Historique(projet["base"]) as historique:
        assert historique.compter_tries() == 3
        assert historique.compter() == 3


def test_reinitialiser_en_simulation_n_efface_rien(projet, monkeypatch, capsys):
    with Historique(projet["base"]) as historique:
        historique.enregistrer_tri(
            [{"id": "A", "score": 1, "resume": "", "drapeaux": [], "verdict": "non"}],
            modele="haiku",
        )
    monkeypatch.setattr(cli, "creer_client", lambda *_a, **_k: FauxLLM())

    assert cli.main(arguments(projet, "--reinitialiser", "--simulation")) == 0

    assert "n'ont pas été effacés" in capsys.readouterr().out
    with Historique(projet["base"]) as historique:
        assert historique.compter_tries() == 1


def test_offres_rqth_ecartees_si_la_config_le_demande(tmp_path, monkeypatch, capsys):
    criteres = tmp_path / "criteres.md"
    criteres.write_text(CRITERES, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
        [recherche]
        mots_cles = ["testeur"]

        [tri]
        criteres = "{criteres}"
        exclure_rqth = true
        """,
        encoding="utf-8",
    )
    base = tmp_path / "offres.db"
    with Historique(base) as historique:
        historique.enregistrer([offre("A"), offre("B", entreprise="Forums Talents Handicap")])
    monkeypatch.setattr(cli, "creer_client", lambda *_a, **_k: FauxLLM())

    cli.main(["trier", "--config", str(config), "--base", str(base), "--simulation"])

    sortie = capsys.readouterr().out
    assert "1 retenue(s)" in sortie
    assert "réservée aux travailleurs handicapés" in sortie


def test_offres_rqth_conservees_par_defaut(projet, monkeypatch, capsys):
    with Historique(projet["base"]) as historique:
        historique.enregistrer([offre("D", entreprise="Forums Talents Handicap")])
    monkeypatch.setattr(cli, "creer_client", lambda *_a, **_k: FauxLLM())

    cli.main(arguments(projet, "--simulation"))

    assert "4 offre(s) partiraient" in capsys.readouterr().out


def test_la_selection_cumule_les_passages_partiels(projet, monkeypatch, capsys):
    """Un tri par tranches ne doit pas écraser la sélection des tranches précédentes."""
    premier = FauxLLM(
        reponses=[
            reponse(
                {"id": "A", "score": 90, "resume": "", "drapeaux": [], "verdict": "postuler"},
                {"id": "B", "score": 60, "resume": "", "drapeaux": [], "verdict": "peut-etre"},
            )
        ]
    )
    monkeypatch.setattr(cli, "creer_client", lambda *_a, **_k: premier)
    assert cli.main(arguments(projet, "--limite", "2")) == 0

    # Deuxième passage : une seule offre, qui n'est pas retenue.
    second = FauxLLM(
        reponses=[reponse({"id": "C", "score": 5, "resume": "", "drapeaux": [], "verdict": "non"})]
    )
    monkeypatch.setattr(cli, "creer_client", lambda *_a, **_k: second)
    assert cli.main(arguments(projet, "--limite", "2")) == 0

    selection = json.loads(projet["sortie"].read_text(encoding="utf-8"))
    assert [offre["id"] for offre in selection] == ["A", "B"]
    sortie = capsys.readouterr().out
    assert "0 offre(s) retenue(s) sur 1 triée(s) dans ce passage ; 2 au total" in sortie
