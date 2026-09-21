"""Tests de la sous-commande « collecter » : actualisation des offres déjà connues.

Aucun appel réseau : la collecte elle-même est remplacée par une doublure.
"""

import pytest

from job_radar import cli
from job_radar.client import reduire_offre
from job_radar.stockage import Historique


def brute(identifiant, **champs):
    """Offre telle que l'API la renvoie, avant réduction."""
    base = {
        "id": identifiant,
        "intitule": "Testeur QA",
        "lieuTravail": {"libelle": "59 - Lille"},
        "entreprise": {"nom": "ACME"},
        "typeContratLibelle": "CDI",
        "experienceLibelle": "Débutant accepté",
        "dateCreation": "2026-09-20T09:00:00.000Z",
        "description": "Vous testez des applications web.",
    }
    return base | champs


@pytest.fixture
def projet(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[recherche]\nmots_cles = ["testeur"]\n', encoding="utf-8")
    monkeypatch.setattr(cli, "lire_identifiants", lambda: ("identifiant", "secret"))
    monkeypatch.setattr(cli, "ClientFranceTravail", lambda *_args, **_kwargs: None)
    return {
        "config": config,
        "base": tmp_path / "offres.db",
        "sortie": tmp_path / "nouvelles.json",
    }


def arguments(projet, *extra):
    return [
        "collecter",
        "--config",
        str(projet["config"]),
        "--base",
        str(projet["base"]),
        "--sortie",
        str(projet["sortie"]),
        "--silencieux",
        *extra,
    ]


def collecter(projet, monkeypatch, brutes, *extra):
    """Lance une collecte dont le résultat est fixé par le test."""
    offres = [reduire_offre(offre) for offre in brutes]
    monkeypatch.setattr(cli, "collecter", lambda *_args, **_kwargs: offres)
    return cli.main(arguments(projet, *extra))


def offre_en_base(projet):
    with Historique(projet["base"]) as historique:
        (offre,) = historique.offres_a_trier()
        return offre


def test_collecte_actualise_le_code_rome_des_offres_connues(projet, monkeypatch, capsys):
    # Première collecte : l'API ne renvoyait pas encore le code ROME.
    assert collecter(projet, monkeypatch, [brute("A")]) == 0
    assert offre_en_base(projet)["rome"] is None

    # Seconde collecte : la même offre, cette fois avec son code.
    assert collecter(projet, monkeypatch, [brute("A", romeCode="M1805")]) == 0

    assert offre_en_base(projet)["rome"] == "M1805"
    assert "1 offre(s) déjà connue(s) ont été actualisée(s)." in capsys.readouterr().out


def test_l_offre_actualisee_n_est_pas_annoncee_comme_nouvelle(projet, monkeypatch, capsys):
    collecter(projet, monkeypatch, [brute("A")])
    capsys.readouterr()

    collecter(projet, monkeypatch, [brute("A", romeCode="M1805")])

    assert "dont 0 nouvelle(s)" in capsys.readouterr().out


def test_le_statut_de_tri_survit_a_une_nouvelle_collecte(projet, monkeypatch):
    collecter(projet, monkeypatch, [brute("A")])
    with Historique(projet["base"]) as historique:
        historique.enregistrer_tri(
            [{"id": "A", "score": 70, "resume": "ok", "drapeaux": [], "verdict": "postuler"}],
            modele="sonnet",
        )

    collecter(projet, monkeypatch, [brute("A", romeCode="M1805")])

    with Historique(projet["base"]) as historique:
        assert historique.offres_a_trier() == []
        (triee,) = historique.offres_triees()
        assert triee["verdict"] == "postuler"
        assert triee["rome"] == "M1805"


def test_sans_historique_n_actualise_rien(projet, monkeypatch):
    collecter(projet, monkeypatch, [brute("A")])

    collecter(projet, monkeypatch, [brute("A", romeCode="M1805")], "--sans-historique")

    assert offre_en_base(projet)["rome"] is None


def test_nouvelle_offre_enregistree_avec_son_code_rome(projet, monkeypatch):
    collecter(projet, monkeypatch, [brute("A", romeCode="K2111")])

    assert offre_en_base(projet)["rome"] == "K2111"
