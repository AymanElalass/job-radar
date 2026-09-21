"""Tests de la requête de recherche : tri, département optionnel, mots-clés, authentification.

Aucun appel réseau : le client est monté sur une session HTTP factice.
"""

import pytest

from faux_reseau import FausseReponse, FausseSession
from job_radar.client import (
    SORT_DATE_DECROISSANTE,
    ClientFranceTravail,
    ErreurAuthentification,
    ErreurRecherche,
    decrire_recherche,
    normaliser_mot_cle,
)


def client_avec(session: FausseSession) -> ClientFranceTravail:
    return ClientFranceTravail("identifiant", "secret", session=session)


# --------------------------------------------------------------------- tri


def test_recherche_triee_par_date_de_creation_decroissante():
    session = FausseSession()

    client_avec(session).rechercher("python", departement="75")

    assert session.appels_recherche[0]["params"]["sort"] == SORT_DATE_DECROISSANTE
    assert SORT_DATE_DECROISSANTE == 1


def test_le_tri_est_envoye_sur_chaque_page():
    session = FausseSession()
    client = client_avec(session)

    client.rechercher("python", departement="75", page=0)
    client.rechercher("python", departement="75", page=1)

    plages = [appel["params"]["range"] for appel in session.appels_recherche]
    tris = {appel["params"]["sort"] for appel in session.appels_recherche}
    assert plages == ["0-149", "150-299"]
    assert tris == {SORT_DATE_DECROISSANTE}


# ------------------------------------------------------- portée géographique


def test_departement_transmis_quand_il_est_precise():
    session = FausseSession()

    client_avec(session).rechercher("python", departement="92")

    assert session.appels_recherche[0]["params"]["departement"] == "92"


def test_parametre_departement_absent_pour_une_recherche_nationale():
    session = FausseSession()

    client_avec(session).rechercher("python", departement=None)

    assert "departement" not in session.appels_recherche[0]["params"]


def test_departement_absent_par_defaut():
    session = FausseSession()

    client_avec(session).rechercher("python")

    assert "departement" not in session.appels_recherche[0]["params"]


# ------------------------------------------------------------------ mots-clés


def test_mot_cle_compose_de_plusieurs_mots_accepte_tel_quel():
    session = FausseSession()

    client_avec(session).rechercher("alternance data", departement="75")

    # L'espace fait partie des caractères autorisés : l'expression part inchangée,
    # l'encodage de l'URL étant assuré par requests.
    assert session.appels_recherche[0]["params"]["motsCles"] == "alternance data"


def test_mot_cle_normalise_avant_envoi():
    session = FausseSession()

    client_avec(session).rechercher("  alternance   data  ")

    assert session.appels_recherche[0]["params"]["motsCles"] == "alternance data"


@pytest.mark.parametrize(
    "mot",
    ["data engineer", "développeur python", "chargé d'affaires", "back-end", "c++", "node.js"],
)
def test_mots_cles_valides(mot):
    assert normaliser_mot_cle(mot) == mot


def test_mot_cle_trop_court_refuse():
    with pytest.raises(ValueError, match="trop court"):
        normaliser_mot_cle("a")


def test_mot_cle_avec_virgule_refuse_car_la_virgule_separe_les_mots_cles():
    with pytest.raises(ValueError, match="virgule"):
        normaliser_mot_cle("data, python")


def test_mot_cle_avec_caractere_interdit_refuse():
    with pytest.raises(ValueError, match="non autorisé"):
        normaliser_mot_cle("python (junior)")


def test_mot_cle_invalide_refuse_avant_tout_appel_reseau():
    session = FausseSession()

    with pytest.raises(ValueError):
        client_avec(session).rechercher("x")

    assert session.appels_recherche == []


# ------------------------------------------------------------- codes de retour


def test_204_signifie_aucun_resultat():
    session = FausseSession(reponses_recherche=[FausseReponse(204)])

    assert client_avec(session).rechercher("python") == []


@pytest.mark.parametrize("statut", [200, 206])
def test_200_et_206_renvoient_les_resultats(statut):
    session = FausseSession(
        reponses_recherche=[FausseReponse(statut, {"resultats": [{"id": "A"}]})]
    )

    assert client_avec(session).rechercher("python") == [{"id": "A"}]


def test_erreur_de_recherche_mentionne_la_zone_nationale():
    session = FausseSession(reponses_recherche=[FausseReponse(400, texte="Bad Request")])

    with pytest.raises(ErreurRecherche, match="France entière"):
        client_avec(session).rechercher("python")


def test_publiee_depuis_invalide_refuse():
    with pytest.raises(ValueError, match="publiee_depuis"):
        client_avec(FausseSession()).rechercher("python", publiee_depuis=5)


# ---------------------------------------------------------- authentification


def test_echec_authentification_leve_une_erreur_dediee():
    session = FausseSession(reponse_token=FausseReponse(400, texte="invalid_client"))

    with pytest.raises(ErreurAuthentification, match="authentification refusée"):
        client_avec(session).rechercher("python")

    # La recherche n'est jamais tentée sans jeton valide.
    assert session.appels_recherche == []


def test_jeton_reutilise_entre_deux_recherches():
    session = FausseSession()
    client = client_avec(session)

    client.rechercher("python")
    client.rechercher("data")

    assert len(session.appels_token) == 1
    assert len(session.appels_recherche) == 2


# ------------------------------------------------- recherche autour d'une commune


def test_commune_et_distance_transmises():
    session = FausseSession()

    client_avec(session).rechercher(commune="66008", distance=30)

    parametres = session.appels_recherche[0]["params"]
    assert parametres["commune"] == "66008"
    assert parametres["distance"] == 30


def test_recherche_sans_mot_cle():
    session = FausseSession()

    client_avec(session).rechercher(commune="66008", distance=30)

    # Pas de motsCles : toutes les offres de la zone sont demandées.
    assert "motsCles" not in session.appels_recherche[0]["params"]


def test_distance_nulle_transmise():
    # distance=0 signifie « la commune seule », ce n'est pas une absence de valeur.
    session = FausseSession()

    client_avec(session).rechercher(commune="66008", distance=0)

    assert session.appels_recherche[0]["params"]["distance"] == 0


def test_commune_absente_par_defaut():
    session = FausseSession()

    client_avec(session).rechercher("python")

    parametres = session.appels_recherche[0]["params"]
    assert "commune" not in parametres
    assert "distance" not in parametres


def test_mot_cle_et_commune_cumulables():
    session = FausseSession()

    client_avec(session).rechercher("testeur", commune="66008", distance=10)

    parametres = session.appels_recherche[0]["params"]
    assert parametres["motsCles"] == "testeur"
    assert parametres["commune"] == "66008"


def test_erreur_de_recherche_decrit_la_commune():
    session = FausseSession(reponses_recherche=[FausseReponse(400, texte="Bad Request")])

    with pytest.raises(ErreurRecherche, match="commune 66008"):
        client_avec(session).rechercher(commune="66008", distance=30)


@pytest.mark.parametrize(
    ("arguments", "attendu"),
    [
        ({"mots_cles": "python", "departement": "75"}, "'python' / dép. 75"),
        ({"commune": "66008", "distance": 30}, "toutes offres / commune 66008 (30 km)"),
        ({"commune": "66008"}, "toutes offres / commune 66008"),
        ({"mots_cles": "full remote"}, "'full remote' / France entière"),
        ({}, "toutes offres / France entière"),
    ],
)
def test_description_d_une_recherche(arguments, attendu):
    assert decrire_recherche(**arguments) == attendu
