"""Tests de la réduction des offres brutes aux champs utiles (aucun appel réseau)."""

from job_radar.client import construire_range, reduire_offre

OFFRE_BRUTE = {
    "id": "190QJXZ",
    "intitule": "Développeur Python (H/F)",
    "description": "Vous rejoignez une équipe de quatre personnes...",
    "dateCreation": "2026-09-15T09:12:41.000Z",
    "lieuTravail": {"libelle": "75 - Paris (Dept.)", "codePostal": "75001"},
    "entreprise": {"nom": "ACME", "description": "PME de 40 personnes"},
    "typeContrat": "CDI",
    "romeCode": "M1805",
    "romeLibelle": "Études et développement informatique",
    "typeContratLibelle": "Contrat à durée indéterminée",
    "experienceLibelle": "2 An(s)",
    "salaire": {"libelle": "Annuel de 45000.0 Euros à 55000.0 Euros sur 12 mois"},
    "alternance": False,
    "origineOffre": {
        "origine": "1",
        "urlOrigine": "https://candidat.francetravail.fr/offres/recherche/detail/190QJXZ",
    },
    "nombrePostes": 2,
    "qualificationCode": "9",
}


def test_reduction_conserve_les_champs_utiles():
    offre = reduire_offre(OFFRE_BRUTE)

    assert offre == {
        "id": "190QJXZ",
        "intitule": "Développeur Python (H/F)",
        "lieu": "75 - Paris (Dept.)",
        "code_postal": "75001",
        "entreprise": "ACME",
        "contrat": "Contrat à durée indéterminée",
        "salaire": "Annuel de 45000.0 Euros à 55000.0 Euros sur 12 mois",
        "experience": "2 An(s)",
        "rome": "M1805",
        "rome_libelle": "Études et développement informatique",
        "alternance": False,
        "date_creation": "2026-09-15T09:12:41.000Z",
        "url": "https://candidat.francetravail.fr/offres/recherche/detail/190QJXZ",
        "description": "Vous rejoignez une équipe de quatre personnes...",
    }


def test_reduction_ecarte_les_champs_inutiles():
    offre = reduire_offre(OFFRE_BRUTE)

    assert "nombrePostes" not in offre
    assert "qualificationCode" not in offre
    assert "origineOffre" not in offre


def test_reduction_tolere_une_offre_incomplete():
    offre = reduire_offre({"id": "ABC123"})

    assert offre["id"] == "ABC123"
    assert offre["intitule"] is None
    assert offre["lieu"] is None
    assert offre["rome"] is None
    assert offre["entreprise"] is None
    assert offre["url"] is None
    assert offre["alternance"] is False


def test_reduction_tolere_des_sous_objets_nuls():
    offre = reduire_offre(
        {"id": "ABC123", "lieuTravail": None, "entreprise": None, "salaire": None}
    )

    assert offre["lieu"] is None
    assert offre["salaire"] is None


def test_url_du_partenaire_prioritaire_sur_france_travail():
    offre = reduire_offre(
        {
            "id": "ABC123",
            "origineOffre": {
                "origine": "2",
                "urlOrigine": "https://candidat.francetravail.fr/offres/detail/ABC123",
                "partenaires": [{"nom": "PartenaireX", "url": "https://partenairex.fr/offre/42"}],
            },
        }
    )

    assert offre["url"] == "https://partenairex.fr/offre/42"


def test_url_de_repli_si_partenaire_sans_lien():
    offre = reduire_offre(
        {
            "id": "ABC123",
            "origineOffre": {
                "urlOrigine": "https://candidat.francetravail.fr/offres/detail/ABC123",
                "partenaires": [{"nom": "PartenaireX"}],
            },
        }
    )

    assert offre["url"] == "https://candidat.francetravail.fr/offres/detail/ABC123"


def test_alternance_normalisee_en_booleen():
    assert reduire_offre({"id": "A", "alternance": True})["alternance"] is True
    assert reduire_offre({"id": "A", "alternance": None})["alternance"] is False


def test_contrat_de_repli_sur_le_code_si_libelle_absent():
    assert reduire_offre({"id": "A", "typeContrat": "MIS"})["contrat"] == "MIS"


def test_construire_range_suit_la_pagination_de_150():
    assert construire_range(0) == "0-149"
    assert construire_range(1) == "150-299"
    assert construire_range(3) == "450-599"
