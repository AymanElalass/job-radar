"""Tests de la configuration et de l'orchestration des recherches (aucun appel réseau)."""

from pathlib import Path

import pytest

from faux_reseau import FauxClient
from job_radar.cli import ErreurConfiguration, charger_config, collecter
from job_radar.client import TAILLE_PAGE, ErreurAuthentification, ErreurRecherche


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

    assert config["recherches"] == [
        {
            "mots_cles": ["python", "data engineer"],
            "departements": ["75", "92"],
            "commune": None,
            "distance": None,
            "publiee_depuis": 3,
            "pages_max": 2,
            "teletravail_complet": False,
        }
    ]
    assert config["chemins"] == {"base": None, "nouvelles": None, "selection": None}
    assert config["tri"] == {
        "criteres": None,
        "modele": "sonnet",
        "taille_lot": 20,
        "delai_max": 180,
        "exclure_rqth": False,
        "codes_rome": ["M18", "K2107", "K2111"],
        "experience_max": 2,
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

    assert charger_config(chemin)["recherches"][0]["departements"] == []


def test_departements_absents_acceptes(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = ["python"]
        """,
    )

    assert charger_config(chemin)["recherches"][0]["departements"] == []


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

    assert charger_config(chemin)["recherches"][0]["mots_cles"] == ["alternance data"]


def test_section_tri_lue(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = ["python"]

        [tri]
        criteres = "~/Documents/cv/criteres-tri.md"
        modele = "sonnet"
        taille_lot = 10
        delai_max = 90
        exclure_rqth = true
        codes_rome = ["M1805", "K2111"]
        """,
    )

    assert charger_config(chemin)["tri"] == {
        "criteres": "~/Documents/cv/criteres-tri.md",
        "modele": "sonnet",
        "taille_lot": 10,
        "delai_max": 90,
        "exclure_rqth": True,
        "codes_rome": ["M1805", "K2111"],
        "experience_max": 2,
    }


def test_section_tri_absente_donne_des_valeurs_par_defaut(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = ["python"]
        """,
    )

    tri = charger_config(chemin)["tri"]

    assert tri["criteres"] is None
    assert tri["modele"] == "sonnet"
    assert tri["taille_lot"] == 20
    assert tri["delai_max"] == 180
    assert tri["exclure_rqth"] is False
    assert tri["codes_rome"] == ["M18", "K2107", "K2111"]


def test_filtre_rome_desactivable(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = ["python"]

        [tri]
        codes_rome = []
        """,
    )

    assert charger_config(chemin)["tri"]["codes_rome"] == []


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
    """Configuration d'une seule recherche, pour tester l'orchestration."""
    recherche = {
        "mots_cles": ["python"],
        "departements": [],
        "commune": None,
        "distance": None,
        "publiee_depuis": 7,
        "pages_max": 1,
        "teletravail_complet": False,
    } | surcharges
    return {"recherches": [recherche]}


def test_une_seule_requete_par_mot_cle_sans_departement():
    client = FauxClient()

    collecter(client, config(mots_cles=["python", "data engineer"]), bavard=False)

    assert [(a["mots_cles"], a["departement"], a["commune"]) for a in client.appels] == [
        ("python", None, None),
        ("data engineer", None, None),
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
        def rechercher(self, mots_cles=None, departement=None, **kwargs):
            super().rechercher(mots_cles, departement, **kwargs)
            if departement == "75":
                raise ErreurRecherche("recherche en échec (500)")
            return [{"id": f"{mots_cles}-{departement}", "intitule": "Poste"}]

    client = ClientCapricieux()
    offres = collecter(client, config(departements=["75", "92"]), bavard=False)

    assert [offre["id"] for offre in offres] == ["python-92"]
    assert len(client.appels) == 2
    assert "recherche en échec" in capsys.readouterr().err


# -------------------------------------------- plusieurs recherches, commune


def test_plusieurs_recherches(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [[recherche]]
        commune = "66008"
        distance = 30
        publiee_depuis = 14
        pages_max = 7

        [[recherche]]
        mots_cles = ["full remote"]
        publiee_depuis = 14
        """,
    )

    recherches = charger_config(chemin)["recherches"]

    assert len(recherches) == 2
    assert recherches[0] == {
        "mots_cles": [],
        "departements": [],
        "commune": "66008",
        "distance": 30,
        "publiee_depuis": 14,
        "pages_max": 7,
        "teletravail_complet": False,
    }
    assert recherches[1]["mots_cles"] == ["full remote"]
    assert recherches[1]["commune"] is None


def test_commune_sans_mot_cle_acceptee(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        commune = "66008"
        distance = 30
        """,
    )

    (recherche,) = charger_config(chemin)["recherches"]

    assert recherche["mots_cles"] == []
    assert recherche["commune"] == "66008"


def test_recherche_sans_mot_cle_ni_commune_refusee(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        departements = ["66"]
        """,
    )

    with pytest.raises(ErreurConfiguration, match="au moins un mot-clé ou une commune"):
        charger_config(chemin)


def test_distance_sans_commune_refusee(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        mots_cles = ["python"]
        distance = 30
        """,
    )

    with pytest.raises(ErreurConfiguration, match="distance"):
        charger_config(chemin)


def test_distance_negative_refusee(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        commune = "66008"
        distance = -5
        """,
    )

    with pytest.raises(ErreurConfiguration, match="positive"):
        charger_config(chemin)


def test_distance_nulle_acceptee(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        commune = "66008"
        distance = 0
        """,
    )

    assert charger_config(chemin)["recherches"][0]["distance"] == 0


def test_chemins_et_plafond_d_experience(tmp_path):
    chemin = ecrire_config(
        tmp_path,
        """
        [recherche]
        commune = "66008"

        [chemins]
        base = "data/argeles.db"
        nouvelles = "data/nouvelles-argeles.json"
        selection = "data/selection-argeles.json"

        [tri]
        codes_rome = []
        experience_max = 5
        """,
    )

    config = charger_config(chemin)

    assert config["chemins"]["base"] == Path("data/argeles.db")
    assert config["chemins"]["selection"] == Path("data/selection-argeles.json")
    assert config["tri"]["codes_rome"] == []
    assert config["tri"]["experience_max"] == 5


def test_une_seule_requete_sans_mot_cle_autour_d_une_commune():
    client = FauxClient()

    collecter(client, config(mots_cles=[], commune="66008", distance=30), bavard=False)

    assert client.appels == [
        {
            "mots_cles": None,
            "departement": None,
            "commune": "66008",
            "distance": 30,
            "publiee_depuis": 7,
            "page": 0,
        }
    ]


def test_toutes_les_recherches_sont_lancees_et_dedoublonnees():
    client = FauxClient(
        resultats_par_appel=[
            [{"id": "A", "intitule": "Poste A"}],
            [{"id": "A", "intitule": "Poste A"}, {"id": "B", "intitule": "Poste B"}],
        ]
    )
    configuration = {
        "recherches": [
            {
                "mots_cles": [],
                "departements": [],
                "commune": "66008",
                "distance": 30,
                "publiee_depuis": 14,
                "pages_max": 1,
                "teletravail_complet": False,
            },
            {
                "mots_cles": ["full remote"],
                "departements": [],
                "commune": None,
                "distance": None,
                "publiee_depuis": 14,
                "pages_max": 1,
                "teletravail_complet": True,
            },
        ]
    }

    offres = collecter(client, configuration, bavard=False)

    assert [o["id"] for o in offres] == ["A", "B"]
    assert [(a["commune"], a["mots_cles"]) for a in client.appels] == [
        ("66008", None),
        (None, "full remote"),
    ]


# ------------------------------------ provenance et saturation de la pagination


def test_provenance_teletravail_marquee_sur_les_offres():
    client = FauxClient(
        resultats_par_appel=[
            [{"id": "A", "intitule": "Poste local"}],
            [{"id": "B", "intitule": "Poste remote"}],
        ]
    )
    configuration = {
        "recherches": [
            {
                "mots_cles": [],
                "departements": [],
                "commune": "66008",
                "distance": 15,
                "publiee_depuis": 14,
                "pages_max": 1,
                "teletravail_complet": False,
            },
            {
                "mots_cles": ["full remote"],
                "departements": [],
                "commune": None,
                "distance": None,
                "publiee_depuis": 14,
                "pages_max": 1,
                "teletravail_complet": True,
            },
        ]
    }

    offres = {o["id"]: o for o in collecter(client, configuration, bavard=False)}

    assert offres["A"]["teletravail_complet_exige"] is False
    assert offres["B"]["teletravail_complet_exige"] is True


def test_offre_trouvee_par_deux_recherches_garde_la_premiere_provenance():
    # La recherche géographique passe avant : une offre locale qui ressort aussi
    # sur « full remote » n'est pas soumise à la règle du télétravail.
    client = FauxClient(
        resultats_par_appel=[
            [{"id": "A", "intitule": "Poste"}],
            [{"id": "A", "intitule": "Poste"}],
        ]
    )
    configuration = {
        "recherches": [
            {
                "mots_cles": [],
                "departements": [],
                "commune": "66008",
                "distance": 15,
                "publiee_depuis": 14,
                "pages_max": 1,
                "teletravail_complet": False,
            },
            {
                "mots_cles": ["full remote"],
                "departements": [],
                "commune": None,
                "distance": None,
                "publiee_depuis": 14,
                "pages_max": 1,
                "teletravail_complet": True,
            },
        ]
    }

    (offre,) = collecter(client, configuration, bavard=False)

    assert offre["teletravail_complet_exige"] is False


def test_saturation_signalee(capsys):
    # Deux pages pleines alors que pages_max valait 2 : il reste des offres.
    pleine = [{"id": f"ID{i}", "intitule": "Poste"} for i in range(TAILLE_PAGE)]
    suivante = [{"id": f"ID{i}", "intitule": "Poste"} for i in range(TAILLE_PAGE, 2 * TAILLE_PAGE)]
    client = FauxClient(resultats_par_appel=[pleine, suivante])
    client.dernier_total = 1834

    collecter(client, config(mots_cles=[], commune="66008", distance=15, pages_max=2), bavard=False)

    sortie = capsys.readouterr().out
    assert "saturation" in sortie
    assert "commune 66008 (15 km)" in sortie
    assert "300 offre(s)" in sortie
    assert "1834" in sortie


def test_pas_de_saturation_quand_la_derniere_page_est_incomplete(capsys):
    pleine = [{"id": f"ID{i}", "intitule": "Poste"} for i in range(TAILLE_PAGE)]
    client = FauxClient(resultats_par_appel=[pleine, [{"id": "DERNIERE", "intitule": "Poste"}]])

    collecter(client, config(commune="66008", pages_max=3), bavard=False)

    assert "saturation" not in capsys.readouterr().out


def test_pas_de_saturation_sur_une_seule_page_incomplete(capsys):
    client = FauxClient(resultats_par_appel=[[{"id": "A", "intitule": "Poste"}]])

    collecter(client, config(), bavard=False)

    assert "saturation" not in capsys.readouterr().out
