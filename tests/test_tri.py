"""Tests du tri : pré-filtre, lots, prompt, validation et robustesse.

Aucun appel réel au LLM : le client est une simple fonction factice.
"""

import json

import pytest

from faux_reseau import FauxLLM
from job_radar.llm import ErreurLLM
from job_radar.tri import (
    LONGUEUR_DESCRIPTION,
    MOTIF_DOUBLON,
    MOTIF_EXPERIENCE,
    MOTIF_LIBERALE,
    ErreurReponseLLM,
    ErreurTri,
    annees_experience,
    charger_criteres,
    construire_prompt,
    decouper_en_lots,
    est_profession_liberale,
    exige_experience_longue,
    fusionner,
    prefiltrer,
    resumer_pour_llm,
    trier,
    trier_lot,
    valider_reponse,
)

CRITERES = "# Critères\nPas de permis, télétravail apprécié."


def offre(identifiant="A", **champs):
    base = {
        "id": identifiant,
        "intitule": "Testeur QA",
        "entreprise": "ACME",
        "lieu": "59 - Lille",
        "contrat": "CDI",
        "experience": "Débutant accepté",
        "salaire": "Annuel de 28000 Euros",
        "alternance": False,
        "date_creation": "2026-09-20T09:00:00.000Z",
        "url": "https://exemple.fr/offre",
        "description": "Vous testez des applications web.",
    }
    return base | champs


# ---------------------------------------------------------------- pré-filtre


@pytest.mark.parametrize(
    "contrat", ["Profession libérale", "PROFESSION LIBERALE", "profession liberale"]
)
def test_profession_liberale_detectee(contrat):
    assert est_profession_liberale(offre(contrat=contrat)) is True


def test_cdi_n_est_pas_une_profession_liberale():
    assert est_profession_liberale(offre(contrat="CDI")) is False


@pytest.mark.parametrize(
    ("libelle", "attendu"),
    [
        ("Débutant accepté", None),
        ("Expérience exigée", None),
        (None, None),
        ("1 An(s)", 1),
        ("2 An(s)", 2),
        ("3 An(s)", 3),
        ("5 An(s) - Dev d'applications web", 5),
        ("6 Mois", 0),
        ("24 Mois", 2),
        ("36 Mois", 3),
    ],
)
def test_lecture_des_annees_d_experience(libelle, attendu):
    assert annees_experience(libelle) == attendu


@pytest.mark.parametrize("libelle", ["3 An(s)", "5 An(s)", "36 Mois"])
def test_experience_longue_ecartee(libelle):
    assert exige_experience_longue(offre(experience=libelle)) is True


@pytest.mark.parametrize("libelle", ["Débutant accepté", "1 An(s)", "2 An(s)", "24 Mois"])
def test_experience_courte_conservee(libelle):
    assert exige_experience_longue(offre(experience=libelle)) is False


def test_prefiltre_ecarte_freelance_experience_et_doublons():
    offres = [
        offre("A"),
        offre("B", contrat="Profession libérale"),
        offre("C", experience="5 An(s)"),
        offre("D"),  # même intitulé et même entreprise que A
        offre("E", intitule="Testeur QA", entreprise="Autre"),
    ]

    retenues, ecartees = prefiltrer(offres)

    assert [o["id"] for o in retenues] == ["A", "E"]
    assert [(o["id"], motif) for o, motif in ecartees] == [
        ("B", MOTIF_LIBERALE),
        ("C", MOTIF_EXPERIENCE),
        ("D", MOTIF_DOUBLON),
    ]


def test_doublon_insensible_a_la_casse_et_aux_accents():
    offres = [
        offre("A", intitule="Développeur", entreprise="Acme"),
        offre("B", intitule="DEVELOPPEUR", entreprise="ACME"),
    ]

    retenues, ecartees = prefiltrer(offres)

    assert [o["id"] for o in retenues] == ["A"]
    assert ecartees[0][1] == MOTIF_DOUBLON


def test_prefiltre_sur_liste_vide():
    assert prefiltrer([]) == ([], [])


# ---------------------------------------------------------------------- lots


def test_decoupage_en_lots_de_vingt():
    offres = [offre(f"ID{i}") for i in range(45)]

    lots = list(decouper_en_lots(offres, 20))

    assert [len(lot) for lot in lots] == [20, 20, 5]


def test_decoupage_d_une_liste_vide():
    assert list(decouper_en_lots([], 20)) == []


def test_taille_de_lot_invalide():
    with pytest.raises(ValueError):
        list(decouper_en_lots([offre()], 0))


# -------------------------------------------------------------------- prompt


def test_seuls_les_champs_utiles_partent_au_llm():
    resume = resumer_pour_llm(offre())

    assert set(resume) == {
        "id",
        "intitule",
        "entreprise",
        "lieu",
        "contrat",
        "experience",
        "description",
    }
    # Ni salaire, ni url, ni date : inutiles au jugement.
    assert "salaire" not in resume
    assert "url" not in resume


def test_description_tronquee_a_800_caracteres():
    resume = resumer_pour_llm(offre(description="x" * 5000))

    assert len(resume["description"]) == LONGUEUR_DESCRIPTION == 800


def test_description_absente_toleree():
    assert resumer_pour_llm(offre(description=None))["description"] == ""


def test_prompt_contient_criteres_et_offres():
    prompt = construire_prompt(CRITERES, [offre("A"), offre("B")])

    assert CRITERES in prompt
    assert '"A"' in prompt and '"B"' in prompt
    assert "postuler" in prompt and "peut-etre" in prompt
    assert "2 offres" in prompt


# ----------------------------------------------------------------- validation


def reponse(*elements) -> str:
    return json.dumps(list(elements), ensure_ascii=False)


def test_reponse_valide():
    texte = reponse(
        {
            "id": "A",
            "score": 80,
            "resume": "Bon poste",
            "drapeaux": ["teletravail"],
            "verdict": "postuler",
        }
    )

    assert valider_reponse(texte, ["A"]) == [
        {
            "id": "A",
            "score": 80,
            "resume": "Bon poste",
            "drapeaux": ["teletravail"],
            "verdict": "postuler",
        }
    ]


def test_reponse_dans_un_bloc_de_code_acceptee():
    texte = (
        "```json\n"
        + reponse({"id": "A", "score": 10, "resume": "", "drapeaux": [], "verdict": "non"})
        + "\n```"
    )

    assert valider_reponse(texte, ["A"])[0]["verdict"] == "non"


def test_reponse_entouree_de_bavardage_acceptee():
    texte = (
        "Voici mon analyse :\n"
        + reponse(
            {"id": "A", "score": 55, "resume": "Peut-être", "drapeaux": [], "verdict": "peut-etre"}
        )
        + "\nJ'espère que cela aide."
    )

    assert valider_reponse(texte, ["A"])[0]["score"] == 55


def test_verdict_accentue_normalise():
    texte = reponse({"id": "A", "score": 50, "resume": "", "drapeaux": [], "verdict": "peut-être"})

    assert valider_reponse(texte, ["A"])[0]["verdict"] == "peut-etre"


def test_drapeaux_inconnus_ignores():
    texte = reponse(
        {
            "id": "A",
            "score": 50,
            "resume": "",
            "drapeaux": ["teletravail", "licorne"],
            "verdict": "peut-etre",
        }
    )

    assert valider_reponse(texte, ["A"])[0]["drapeaux"] == ["teletravail"]


def test_resume_ramene_a_deux_lignes():
    texte = reponse(
        {
            "id": "A",
            "score": 50,
            "resume": "un\ndeux\ntrois",
            "drapeaux": [],
            "verdict": "peut-etre",
        }
    )

    assert valider_reponse(texte, ["A"])[0]["resume"] == "un\ndeux"


@pytest.mark.parametrize(
    "texte",
    [
        "pas du JSON du tout",
        "[",
        "[]",
        '[{"id": "INCONNU", "score": 10, "verdict": "non"}]',
        '[{"id": "A", "verdict": "non"}]',
        '[{"id": "A", "score": "beaucoup", "verdict": "non"}]',
        '[{"id": "A", "score": 150, "verdict": "non"}]',
        '[{"id": "A", "score": -1, "verdict": "non"}]',
        '[{"id": "A", "score": 10, "verdict": "bof"}]',
        '[{"id": "A", "score": 10, "verdict": "non", "drapeaux": "permis"}]',
        '["une chaîne"]',
    ],
)
def test_reponses_invalides_refusees(texte):
    with pytest.raises(ErreurReponseLLM):
        valider_reponse(texte, ["A"])


# ------------------------------------------------------------- tri et reprise


VALIDE_A = reponse({"id": "A", "score": 70, "resume": "ok", "drapeaux": [], "verdict": "postuler"})


def test_une_seule_nouvelle_tentative_apres_reponse_invalide():
    client = FauxLLM(reponses=["n'importe quoi", VALIDE_A])

    resultats = trier_lot(client, CRITERES, [offre("A")])

    assert [r["id"] for r in resultats] == ["A"]
    assert client.appels == 2


def test_deux_echecs_de_suite_abandonnent_le_lot():
    client = FauxLLM(reponses=["raté", "raté encore"])

    with pytest.raises(ErreurReponseLLM):
        trier_lot(client, CRITERES, [offre("A")])

    # Une seule reprise, pas d'acharnement.
    assert client.appels == 2


def test_un_lot_en_erreur_ne_bloque_pas_les_autres():
    valide_b = reponse(
        {"id": "B", "score": 40, "resume": "bof", "drapeaux": ["permis"], "verdict": "non"}
    )
    client = FauxLLM(reponses=["cassé", "toujours cassé", valide_b])

    resultats, erreurs = trier(client, CRITERES, [offre("A"), offre("B")], taille_lot=1)

    assert [r["id"] for r in resultats] == ["B"]
    assert len(erreurs) == 1
    assert "lot 1/2" in erreurs[0]


def test_tri_de_plusieurs_lots():
    valide_b = reponse(
        {"id": "B", "score": 90, "resume": "top", "drapeaux": [], "verdict": "postuler"}
    )
    client = FauxLLM(reponses=[VALIDE_A, valide_b])

    resultats, erreurs = trier(client, CRITERES, [offre("A"), offre("B")], taille_lot=1)

    assert sorted(r["id"] for r in resultats) == ["A", "B"]
    assert erreurs == []
    assert client.appels == 2


def test_progression_rapportee_avant_chaque_lot():
    etapes = []
    client = FauxLLM(
        reponses=[
            VALIDE_A,
            reponse({"id": "B", "score": 1, "resume": "", "drapeaux": [], "verdict": "non"}),
        ]
    )

    trier(
        client,
        CRITERES,
        [offre("A"), offre("B")],
        taille_lot=1,
        rappel_debut=lambda numero, total, taille: etapes.append((numero, total, taille)),
    )

    assert etapes == [(1, 2, 1), (2, 2, 1)]


# ------------------------------------------------------------------ fusion


def test_fusion_triee_par_score_decroissant():
    offres = [offre("A"), offre("B"), offre("C")]
    resultats = [
        {"id": "A", "score": 40, "resume": "", "drapeaux": [], "verdict": "non"},
        {"id": "B", "score": 90, "resume": "", "drapeaux": [], "verdict": "postuler"},
        {"id": "C", "score": 60, "resume": "", "drapeaux": [], "verdict": "peut-etre"},
    ]

    classees = fusionner(offres, resultats)

    assert [o["id"] for o in classees] == ["B", "C", "A"]
    # L'offre garde ses champs d'origine en plus du verdict.
    assert classees[0]["entreprise"] == "ACME"
    assert classees[0]["url"] == "https://exemple.fr/offre"


def test_fusion_ignore_un_resultat_sans_offre():
    assert fusionner([offre("A")], [{"id": "Z", "score": 10, "verdict": "non"}]) == []


# ---------------------------------------------------------------- critères


def test_criteres_charges_depuis_le_fichier(tmp_path):
    chemin = tmp_path / "criteres.md"
    chemin.write_text(CRITERES, encoding="utf-8")

    assert charger_criteres(chemin) == CRITERES


def test_fichier_de_criteres_introuvable(tmp_path):
    with pytest.raises(ErreurTri, match="introuvable"):
        charger_criteres(tmp_path / "absent.md")


def test_fichier_de_criteres_vide(tmp_path):
    chemin = tmp_path / "criteres.md"
    chemin.write_text("   \n", encoding="utf-8")

    with pytest.raises(ErreurTri, match="vide"):
        charger_criteres(chemin)


# --------------------------------------------------- délai dépassé par lot


class ClientLent:
    """Client LLM qui échoue comme un appel dont le délai est dépassé."""

    def __init__(self, echecs: int) -> None:
        self.echecs = echecs
        self.appels = 0

    def __call__(self, prompt: str) -> str:
        self.appels += 1
        if self.appels <= self.echecs:
            raise ErreurLLM("délai dépassé : pas de réponse de claude après 180 s")
        return reponse(
            {"id": "B", "score": 60, "resume": "ok", "drapeaux": [], "verdict": "peut-etre"}
        )


def test_lot_abandonne_sur_delai_depasse_les_suivants_continuent():
    client = ClientLent(echecs=1)

    resultats, erreurs = trier(client, CRITERES, [offre("A"), offre("B")], taille_lot=1)

    assert [r["id"] for r in resultats] == ["B"]
    assert len(erreurs) == 1
    assert "délai dépassé" in erreurs[0]
    assert "lot 1/2" in erreurs[0]


def test_pas_de_seconde_tentative_apres_un_delai_depasse():
    client = ClientLent(echecs=2)

    resultats, erreurs = trier(client, CRITERES, [offre("A")], taille_lot=1)

    # Réessayer un appel trop lent ne ferait qu'attendre deux fois.
    assert client.appels == 1
    assert resultats == []
    assert len(erreurs) == 1


def test_duree_rapportee_apres_chaque_lot():
    bilans = []
    client = FauxLLM(reponses=[VALIDE_A])

    trier(
        client,
        CRITERES,
        [offre("A")],
        taille_lot=1,
        rappel_fin=lambda numero, total, duree, notees, motif: bilans.append(
            (numero, total, notees, motif, duree)
        ),
    )

    (numero, total, notees, motif, duree) = bilans[0]
    assert (numero, total, notees, motif) == (1, 1, 1, None)
    assert duree >= 0.0


def test_motif_rapporte_quand_le_lot_est_abandonne():
    bilans = []

    trier(
        ClientLent(echecs=1),
        CRITERES,
        [offre("A")],
        taille_lot=1,
        rappel_fin=lambda numero, total, duree, notees, motif: bilans.append((notees, motif)),
    )

    notees, motif = bilans[0]
    assert notees == 0
    assert "délai dépassé" in motif
