"""Tests de la configuration et de l'orchestration des recherches (aucun appel réseau)."""

import pytest

from faux_reseau import FauxClient
from job_radar.cli import ErreurConfiguration, charger_config, collecter
from job_radar.client import ErreurAuthentification, ErreurRecherche


def ecrire_config(tmp_path, contenu: str):
    chemin = tmp_path / "config.toml"
    chemin.write_text(contenu, encoding="utf-8")
    return chemin


# ------------------------------------------------------- lecture du fichier


def test_config_complete(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = ["python", "data engineer"]
        departements = ["75", "92"]
        publiee_depuis = 3
        pages_max = 2
        """,
    )

    config = charger_config(chemin)

    assert config == {
        "mots_cles": ["python", "data engineer"],
        "departements": ["75", "92"],
        "publiee_depuis": 3,
        "pages_max": 2,
    }


def test_liste_de_departements_vide_acceptee(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = ["python"]
        departements = []
        """,
    )

    assert charger_config(chemin)["departements"] == []


def test_departements_absents_acceptes(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = ["python"]
        """,
    )

    assert charger_config(chemin)["departements"] == []


def test_mots_cles_obligatoires(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = []
        departements = ["75"]
        """,
    )

    with pytest.raises(ErreurConfiguration, match="au moins un mot-clé"):
        charger_config(chemin)


def test_mot_cle_invalide_refuse_des_la_configuration(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = ["data, python"]
        """,
    )

    with pytest.raises(ErreurConfiguration, match="virgule"):
        charger_config(chemin)


def test_mots_cles_normalises_a_la_lecture(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = ["  alternance   data "]
        """,
    )

    assert charger_config(chemin)["mots_cles"] == ["alternance data"]


def test_publiee_depuis_invalide_refuse(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = ["python"]
        publiee_depuis = 5
        """,
    )

    with pytest.raises(ErreurConfiguration, match="publiee_depuis"):
        charger_config(chemin)


def test_config_introuvable(tmp_path):
    with pytest.raises(ErreurConfiguration, match="introuvable"):
        charger_config(tmp_path / "absent.toml")


# ------------------------------------------------------ orchestration
def config(**surcharges):
    base = {"mots_cles": ["python"], "departements": [], "publiee_depuis": 7, "pages_max": 1}
    return base | surcharges


def test_une_seule_requete_par_mot_cle_sans_departement():
    client = FauxClient()

    collecter(client, config(mots_cles=["python", "data engineer"]), bavard=False)

    assert client.appels == [
        {"mots_cles": "python", "departement": None, "publiee_depuis": 7, "page": 0},
        {"mots_cles": "data engineer", "departement": None, "publiee_depuis": 7, "page": 0},
    ]


def test_une_requete_par_couple_mot_cle_departement():
    client = FauxClient()

    collecter(client, config(mots_cles=["python", "data"], departements=["75", "92"]), bavard=False)

    couples = [(appel["mots_cles"], appel["departement"]) for appel in client.appels]
    assert couples == [("python", "75"), ("python", "92"), ("data", "75"), ("data", "92")]


def test_dedoublonnage_par_identifiant_entre_recherches():
    client = FauxClient(
        resultats_par_appel=[
            [{"id": "A", "intitule": "Poste A"}, {"id": "B", "intitule": "Poste B"}],
            [{"id": "B", "intitule": "Poste B"}, {"id": "C", "intitule": "Poste C"}],
        ]
    )

    offres = collecter(client, config(mots_cles=["python", "data"]), bavard=False)

    assert [offre["id"] for offre in offres] == ["A", "B", "C"]


def test_offres_sans_identifiant_ignorees():
    client = FauxClient(resultats_par_appel=[[{"intitule": "Poste sans id"}]])

    assert collecter(client, config(), bavard=False) == []


def test_echec_authentification_interrompt_tout_de_suite():
    client = FauxClient(erreur=ErreurAuthentification("authentification refusée (400)"))

    with pytest.raises(ErreurAuthentification):
        collecter(
            client, config(mots_cles=["python", "data"], departements=["75", "92"]), bavard=False
        )

    # Une seule tentative : pas de nouvel essai pour chaque couple mot-clé / département.
    assert len(client.appels) == 1


def test_echec_d_une_recherche_n_interrompt_pas_les_suivantes(capsys):
    class ClientCapricieux(FauxClient):
        def rechercher(self, mots_cles, departement=None, publiee_depuis=7, page=0):
            super().rechercher(mots_cles, departement, publiee_depuis, page)
            if departement == "75":
                raise ErreurRecherche("recherche en échec (500)")
            return [{"id": f"{mots_cles}-{departement}", "intitule": "Poste"}]

    client = ClientCapricieux()
    offres = collecter(client, config(departements=["75", "92"]), bavard=False)

    assert [offre["id"] for offre in offres] == ["python-92"]
    assert len(client.appels) == 2
    assert "recherche en échec" in capsys.readouterr().err
