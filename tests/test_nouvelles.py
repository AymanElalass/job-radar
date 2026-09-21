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


# ---------------------------------------------------- offres à trier (étape 2)


def test_offres_a_trier_rend_le_contenu_complet(historique):
    historique.enregistrer([offre("A") | {"lieu": "59 - Lille", "description": "Poste"}])

    (a_trier,) = historique.offres_a_trier()

    assert a_trier["lieu"] == "59 - Lille"
    assert a_trier["description"] == "Poste"


def test_offre_deja_triee_n_est_plus_a_trier(historique):
    historique.enregistrer([offre("A"), offre("B")])
    historique.enregistrer_tri(
        [{"id": "A", "score": 70, "resume": "ok", "drapeaux": [], "verdict": "postuler"}],
        modele="haiku",
    )

    assert [o["id"] for o in historique.offres_a_trier()] == ["B"]
    assert historique.compter_tries() == 1


def test_limite_du_nombre_d_offres_a_trier(historique):
    historique.enregistrer([offre(f"ID{i}") for i in range(10)])

    assert len(historique.offres_a_trier(limite=3)) == 3


def test_offres_a_trier_les_plus_recentes_d_abord(historique):
    historique.enregistrer(
        [
            offre("VIEILLE") | {"date_creation": "2026-09-01T00:00:00.000Z"},
            offre("RECENTE") | {"date_creation": "2026-09-20T00:00:00.000Z"},
        ]
    )

    assert [o["id"] for o in historique.offres_a_trier(limite=1)] == ["RECENTE"]


def test_enregistrer_tri_est_idempotent(historique):
    historique.enregistrer([offre("A")])
    resultat = {"id": "A", "score": 70, "resume": "ok", "drapeaux": ["permis"], "verdict": "non"}

    historique.enregistrer_tri([resultat], modele="haiku")
    historique.enregistrer_tri([resultat | {"score": 80}], modele="haiku")

    assert historique.compter_tries() == 1


def test_enregistrer_tri_d_une_liste_vide(historique):
    assert historique.enregistrer_tri([], modele="haiku") == 0


def test_offres_sans_contenu_non_triables_puis_importees(tmp_path):
    from job_radar.stockage import Historique

    chemin = tmp_path / "offres.db"
    # Base d'avant l'étape 2 : le contenu des offres n'était pas conservé.
    with Historique(chemin) as base:
        base.connexion.execute(
            "INSERT INTO offres (id, intitule, vue_le) VALUES ('A', 'Testeur', '2026-09-20')"
        )
        base.connexion.commit()
        assert base.offres_a_trier() == []
        assert base.compter_sans_contenu() == 1

        complete = base.importer_contenu([offre("A") | {"lieu": "59 - Lille"}, offre("B")])

        assert complete == 2
        assert base.compter_sans_contenu() == 0
        assert {o["id"] for o in base.offres_a_trier()} == {"A", "B"}
