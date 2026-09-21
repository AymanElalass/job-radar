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


def test_effacer_tri_conserve_les_offres(historique):
    historique.enregistrer([offre("A"), offre("B")])
    historique.enregistrer_tri(
        [
            {"id": "A", "score": 70, "resume": "", "drapeaux": [], "verdict": "postuler"},
            {"id": "B", "score": 10, "resume": "", "drapeaux": [], "verdict": "non"},
        ],
        modele="haiku",
    )

    efface = historique.effacer_tri()

    assert efface == 2
    assert historique.compter_tries() == 0
    assert historique.compter() == 2
    # Les offres redeviennent triables.
    assert {o["id"] for o in historique.offres_a_trier()} == {"A", "B"}


def test_effacer_un_tri_vide(historique):
    assert historique.effacer_tri() == 0


def test_offres_triees_reunit_contenu_et_verdict(historique):
    historique.enregistrer([offre("A") | {"lieu": "59 - Lille"}])
    historique.enregistrer_tri(
        [{"id": "A", "score": 80, "resume": "bien", "drapeaux": ["rqth"], "verdict": "postuler"}],
        modele="haiku",
    )

    (triee,) = historique.offres_triees()

    assert triee["lieu"] == "59 - Lille"
    assert triee["score"] == 80
    assert triee["drapeaux"] == ["rqth"]
    assert triee["verdict"] == "postuler"


def test_offres_triees_filtrees_par_verdict_et_classees(historique):
    historique.enregistrer([offre("A"), offre("B"), offre("C")])
    historique.enregistrer_tri(
        [
            {"id": "A", "score": 40, "resume": "", "drapeaux": [], "verdict": "peut-etre"},
            {"id": "B", "score": 90, "resume": "", "drapeaux": [], "verdict": "postuler"},
            {"id": "C", "score": 5, "resume": "", "drapeaux": [], "verdict": "non"},
        ],
        modele="haiku",
    )

    retenues = historique.offres_triees(verdicts=("postuler", "peut-etre"))

    assert [o["id"] for o in retenues] == ["B", "A"]


def test_offres_triees_ignore_les_offres_sans_contenu(tmp_path):
    from job_radar.stockage import Historique

    with Historique(tmp_path / "offres.db") as base:
        base.connexion.execute(
            "INSERT INTO offres (id, intitule, vue_le) VALUES ('A', 'Testeur', '2026-09-20')"
        )
        base.connexion.commit()
        base.enregistrer_tri(
            [{"id": "A", "score": 80, "resume": "", "drapeaux": [], "verdict": "postuler"}],
            modele="haiku",
        )

        assert base.offres_triees() == []


# ------------------------------- actualisation des offres déjà connues


def test_actualiser_rafraichit_le_contenu(historique):
    historique.enregistrer([offre("A")])

    change = historique.actualiser([offre("A") | {"rome": "M1805", "intitule": "Testeur QA"}])

    (a_trier,) = historique.offres_a_trier()
    assert change == 1
    assert a_trier["rome"] == "M1805"
    assert a_trier["intitule"] == "Testeur QA"


def test_actualiser_conserve_la_date_de_premiere_vue(historique):
    historique.enregistrer([offre("A")])
    vue_le = historique.connexion.execute("SELECT vue_le FROM offres WHERE id = 'A'").fetchone()[0]

    historique.actualiser([offre("A") | {"rome": "M1805"}])

    assert (
        historique.connexion.execute("SELECT vue_le FROM offres WHERE id = 'A'").fetchone()[0]
        == vue_le
    )
    # L'offre ne redevient pas nouvelle.
    assert historique.filtrer_nouvelles([offre("A")]) == []


def test_actualiser_ne_touche_pas_au_statut_de_tri(historique):
    historique.enregistrer([offre("A")])
    historique.enregistrer_tri(
        [{"id": "A", "score": 70, "resume": "ok", "drapeaux": [], "verdict": "postuler"}],
        modele="sonnet",
    )

    historique.actualiser([offre("A") | {"rome": "M1805"}])

    assert historique.compter_tries() == 1
    assert historique.offres_a_trier() == []
    (triee,) = historique.offres_triees()
    assert triee["verdict"] == "postuler"
    # Le contenu rafraîchi est bien celui qui ressort.
    assert triee["rome"] == "M1805"


def test_actualiser_ignore_les_offres_inconnues(historique):
    historique.enregistrer([offre("A")])

    change = historique.actualiser([offre("B", "Autre poste")])

    assert change == 0
    assert historique.compter() == 1


def test_actualiser_ne_compte_que_les_changements_reels(historique):
    historique.enregistrer([offre("A")])

    assert historique.actualiser([offre("A")]) == 0
    assert historique.actualiser([offre("A") | {"rome": "M1805"}]) == 1
    assert historique.actualiser([offre("A") | {"rome": "M1805"}]) == 0


def test_actualiser_une_liste_vide(historique):
    assert historique.actualiser([]) == 0
