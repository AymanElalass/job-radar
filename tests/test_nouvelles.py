"""Tests de la détection des offres jamais vues (base SQLite en mémoire, aucun réseau)."""

import pytest

from job_radar.stockage import Historique


def offre(identifiant: str, intitule: str = "Développeur Python") -> dict:
    return {
        "id": identifiant,
        "intitule": intitule,
        "entreprise": "ACME",
        "date_creation": "2026-09-15T09:12:41.000Z",
    }


@pytest.fixture
def historique(tmp_path):
    with Historique(tmp_path / "offres.db") as base:
        yield base


def test_toutes_les_offres_sont_nouvelles_au_premier_passage(historique):
    offres = [offre("A"), offre("B")]

    assert historique.filtrer_nouvelles(offres) == offres


def test_les_offres_enregistrees_ne_ressortent_plus(historique):
    historique.enregistrer([offre("A"), offre("B")])

    nouvelles = historique.filtrer_nouvelles([offre("A"), offre("B"), offre("C")])

    assert [o["id"] for o in nouvelles] == ["C"]


def test_second_passage_sans_nouveaute(historique):
    offres = [offre("A"), offre("B")]
    historique.enregistrer(historique.filtrer_nouvelles(offres))

    assert historique.filtrer_nouvelles(offres) == []


def test_doublons_dans_un_meme_lot_ecartes(historique):
    nouvelles = historique.filtrer_nouvelles([offre("A"), offre("A"), offre("B")])

    assert [o["id"] for o in nouvelles] == ["A", "B"]


def test_ordre_d_arrivee_preserve(historique):
    nouvelles = historique.filtrer_nouvelles([offre("C"), offre("A"), offre("B")])

    assert [o["id"] for o in nouvelles] == ["C", "A", "B"]


def test_enregistrer_est_idempotent(historique):
    historique.enregistrer([offre("A")])
    historique.enregistrer([offre("A")])

    assert historique.compter() == 1


def test_enregistrer_une_liste_vide_ne_fait_rien(historique):
    assert historique.enregistrer([]) == 0
    assert historique.compter() == 0


def test_historique_persiste_entre_deux_executions(tmp_path):
    chemin = tmp_path / "sous-dossier" / "offres.db"

    with Historique(chemin) as base:
        base.enregistrer([offre("A")])

    with Historique(chemin) as base:
        assert base.filtrer_nouvelles([offre("A"), offre("B")]) == [offre("B")]


def test_filtrage_sur_un_gros_lot_depassant_la_limite_de_parametres(historique):
    lot = [offre(f"ID{i}") for i in range(1200)]
    historique.enregistrer(lot[:600])

    nouvelles = historique.filtrer_nouvelles(lot)

    assert len(nouvelles) == 600
    assert nouvelles[0]["id"] == "ID600"
