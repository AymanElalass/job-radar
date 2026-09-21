"""Tests du tri : pré-filtre, lots, prompt, validation et robustesse.

Aucun appel réel au LLM : le client est une simple fonction factice.
"""

import json

import pytest

from faux_reseau import FauxLLM
from job_radar.llm import ErreurLLM
from job_radar.tri import (
    DRAPEAUX_LLM,
    DRAPEAUX_REDHIBITOIRES,
    DRAPEAUX_VALIDES,
    LONGUEUR_DESCRIPTION,
    MOTIF_DOUBLON,
    MOTIF_EXPERIENCE,
    MOTIF_LIBERALE,
    MOTIF_RQTH,
    MOTIF_STAGE,
    MOTIF_TJM,
    MOTS_SENIORITE,
    ErreurReponseLLM,
    ErreurTri,
    ajouter_drapeaux_deterministes,
    annees_experience,
    appliquer_regle_verdict,
    charger_criteres,
    construire_prompt,
    decouper_en_lots,
    drapeaux_deterministes,
    est_profession_liberale,
    est_remunere_au_jour,
    est_rqth,
    est_stage,
    exige_bac5,
    exige_experience_dans_le_texte,
    exige_experience_longue,
    exige_permis,
    fusionner,
    normaliser_intitule,
    normaliser_ville,
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
        offre("B", intitule="Analyste", contrat="Profession libérale"),
        offre("C", intitule="Intégrateur", experience="5 An(s)"),
        offre("D"),  # même intitulé, même entreprise et même ville que A
        offre("E", intitule="Recetteur", entreprise="Autre"),
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


# -------------------------------------------------- détections déterministes


@pytest.mark.parametrize(
    "champs",
    [
        {"entreprise": "Forums Talents Handicap"},
        {"entreprise": "TALENTS HANDICAP RECRUTEMENT"},
        {"entreprise": "Talents handicap"},
        {"url": "https://www.handicap-job.com/offre/12345"},
        {"url": "HTTPS://HANDICAP-JOB.COM/offre/1"},
    ],
)
def test_offre_rqth_detectee(champs):
    assert est_rqth(offre("A", **champs)) is True


@pytest.mark.parametrize(
    "champs",
    [
        {"entreprise": "ACME"},
        {"entreprise": None},
        {"url": "https://candidat.francetravail.fr/offres/detail/A"},
        {"entreprise": "Cabinet Handicap Conseil"},
    ],
)
def test_offre_non_rqth(champs):
    assert est_rqth(offre("A", **champs)) is False


@pytest.mark.parametrize(
    "champs",
    [
        # L'intitulé et l'URL sont pris au mot.
        {"intitule": "Stage Développeur Web (H/F)"},
        {"intitule": "STAGE QA"},
        {"intitule": "Offre de stages multiples"},
        {"intitule": "Stagiaire recette (H/F)"},
        {"url": "https://exemple.fr/offres/stage-testeur"},
        {"url": "https://handicap-job.com/detail/developpeur-cloud-stage.html"},
        # Dans la description, seules les tournures qui désignent l'offre elle-même.
        {"description": "En tant que stagiaire en développement Java, vos tâches…"},
        {"description": "Nous proposons un stage en recette applicative."},
        {"description": "Offre de stage de 6 mois à pourvoir."},
        {"description": "Un stage de 6 mois, conventionné par votre école."},
        {"description": "Le stagiaire sera accompagné par un référent."},
        {"description": "Type de contrat : stage"},
        {"description": "Stage de fin d'études en data."},
    ],
)
def test_stage_detecte(champs):
    assert est_stage(offre("A", **champs)) is True


@pytest.mark.parametrize(
    "champs",
    [
        {"intitule": "Testeur QA", "description": "Poste en CDI."},
        {"description": "Marché en stagnation, équipe stable."},
        {"intitule": "Chargé de recette", "description": "Vous montez sur scène ?"},
        {"intitule": None, "description": None, "url": None},
        # Cas réels que la détection ne doit pas écarter : un poste de formateur
        # parle des stagiaires qu'il encadre, et une offre junior cite le stage
        # parmi les premières expériences acceptées.
        {
            "intitule": "Formateur testeur levage/CACES (H/F)",
            "description": "Des supports de formation pour une meilleure "
            "compréhension de vos stagiaires.",
        },
        {
            "intitule": "Développeur Cobol - junior (H/F)",
            "description": "Débutant accepté ou première expérience (stage, "
            "alternance ou projet académique) en développement.",
        },
        {
            "intitule": "Chargé de recette (H/F)",
            "description": "Vous encadrerez nos stagiaires sur les campagnes de test.",
        },
    ],
)
def test_pas_un_stage(champs):
    assert est_stage(offre("A", **champs)) is False


def test_stage_ecarte_au_prefiltre():
    retenues, ecartees = prefiltrer([offre("A"), offre("B", intitule="Stage QA (H/F)")])

    assert [o["id"] for o in retenues] == ["A"]
    assert ecartees == [(offre("B", intitule="Stage QA (H/F)"), MOTIF_STAGE)]


def test_rqth_conservee_par_defaut():
    rqth = offre("B", intitule="Analyste", entreprise="Forums Talents Handicap")

    retenues, ecartees = prefiltrer([offre("A"), rqth])

    assert [o["id"] for o in retenues] == ["A", "B"]
    assert ecartees == []


def test_rqth_ecartee_si_demande():
    rqth = offre("B", intitule="Analyste", entreprise="Forums Talents Handicap")

    retenues, ecartees = prefiltrer([offre("A"), rqth], exclure_rqth=True)

    assert [o["id"] for o in retenues] == ["A"]
    assert ecartees == [(rqth, MOTIF_RQTH)]


def test_drapeau_rqth_ajoute_sans_llm():
    lot = [offre("A", entreprise="Forums Talents Handicap"), offre("B")]
    resultats = [
        {"id": "A", "score": 70, "resume": "", "drapeaux": [], "verdict": "postuler"},
        {"id": "B", "score": 40, "resume": "", "drapeaux": ["permis"], "verdict": "non"},
    ]

    enrichis = ajouter_drapeaux_deterministes(lot, resultats)

    assert enrichis[0]["drapeaux"] == ["rqth"]
    assert enrichis[1]["drapeaux"] == ["permis"]


def test_drapeau_rqth_non_duplique():
    lot = [offre("A", entreprise="Forums Talents Handicap")]
    resultats = [
        {"id": "A", "score": 70, "resume": "", "drapeaux": ["rqth"], "verdict": "postuler"}
    ]

    assert ajouter_drapeaux_deterministes(lot, resultats)[0]["drapeaux"] == ["rqth"]


def test_drapeau_rqth_pose_par_le_tri_d_un_lot():
    client = FauxLLM(
        reponses=[
            reponse({"id": "A", "score": 70, "resume": "ok", "drapeaux": [], "verdict": "postuler"})
        ]
    )

    resultats = trier_lot(client, CRITERES, [offre("A", entreprise="Forums Talents Handicap")])

    assert resultats[0]["drapeaux"] == ["rqth"]


# ------------------------------------------------------------------ prompt


def test_le_llm_ne_choisit_ni_rqth_ni_stage_deguise():
    prompt = construire_prompt(CRITERES, [offre("A")])

    # rqth est déterminé en Python, stage_deguise a été supprimé.
    assert "rqth" not in prompt
    assert "stage_deguise" not in prompt
    assert "rqth" not in DRAPEAUX_LLM
    assert "stage_deguise" not in DRAPEAUX_VALIDES


def test_chaque_drapeau_du_llm_est_defini_dans_le_prompt():
    prompt = construire_prompt(CRITERES, [offre("A")])

    for drapeau in DRAPEAUX_LLM:
        assert f'"{drapeau}" :' in prompt, f"{drapeau} n'est pas défini"


def test_le_prompt_exige_un_tri_severe():
    prompt = construire_prompt(CRITERES, [offre("A")])

    assert "SÉVÈRE" in prompt
    assert "chance réelle d'être retenu" in prompt
    for mot in MOTS_SENIORITE:
        assert mot in prompt


def test_le_prompt_contient_trois_exemples_notes():
    prompt = construire_prompt(CRITERES, [offre("A")])

    assert prompt.count("Exemple noté") == 3
    for verdict in ("postuler", "peut-etre", "non"):
        assert f"Exemple noté « {verdict} »" in prompt


def test_le_prompt_reste_valide_malgre_les_accolades_des_exemples():
    # Les exemples JSON contiennent des accolades : la substitution ne doit pas
    # les interpréter comme des champs à remplacer.
    prompt = construire_prompt("critères {particuliers}", [offre("A")])

    assert "critères {particuliers}" in prompt
    assert '"intitule": "Chargé de recette applicative (H/F)"' in prompt


def test_drapeau_supprime_ignore_dans_une_reponse():
    texte = reponse(
        {
            "id": "A",
            "score": 50,
            "resume": "",
            "drapeaux": ["stage_deguise", "permis"],
            "verdict": "non",
        }
    )

    assert valider_reponse(texte, ["A"])[0]["drapeaux"] == ["permis"]


# --------------------------------------------- permis, bac+5, expérience, TJM


@pytest.mark.parametrize(
    "description",
    [
        "Un permis B valide (obligatoire pour ce poste).",
        "Le permis B est nécessaire car quelques déplacements sont prévus.",
        "Permis de conduire exigé.",
        "Permis B obligatoire.",
        "Déplacements quotidiens : permis requis.",
    ],
)
def test_permis_exige_detecte(description):
    assert exige_permis(offre("A", description=description)) is True


@pytest.mark.parametrize(
    "description",
    [
        "Poste sédentaire, aucun déplacement.",
        "Permis B apprécié mais non obligatoire.",
        "Le permis n'est pas exigé pour ce poste.",
        "Sans permis, le site est accessible en tramway.",
        "Permis B souhaité.",
        # Rubrique des annonces agrégées : l'exigence porte sur la certification.
        "permis/certification :\n* certification ISTQB (requis)",
    ],
)
def test_permis_non_exige(description):
    assert exige_permis(offre("A", description=description)) is False


@pytest.mark.parametrize(
    "description",
    [
        "Formation bac+5 en informatique exigée.",
        "Master 2 requis en data science.",
        "Vous êtes diplômé(e) d'une école d'ingénieurs.",
        "De formation bac + 5, vous maîtrisez Java.",
        "Diplôme d'ingénieur obligatoire.",
        "Titulaire d'un mastère en informatique.",
    ],
)
def test_bac5_exige_detecte(description):
    assert exige_bac5(offre("A", description=description)) is True


@pytest.mark.parametrize(
    "description",
    [
        "Formation bac+2 ou bac+3 bienvenue.",
        "Aucun diplôme particulier n'est demandé.",
        # Une fourchette de niveaux n'exige pas le plus haut.
        "Profil bac+3 à bac+5 en informatique.",
        "Public en alternance (de bac à bac +5), et en formation continue.",
        "Formations reconnues par l'État, de niveau 4 à niveau 7 "
        "(bac, bac+2, bachelor/bac+3 ou mastère/bac+5).",
    ],
)
def test_bac5_non_exige(description):
    assert exige_bac5(offre("A", description=description)) is False


@pytest.mark.parametrize(
    ("description", "attendu"),
    [
        ("5 ans d'expérience minimum sur des projets data.", True),
        ("Expérience : 7 ans minimum.", True),
        ("Séniorité requise : 8 à 10 ans d'expérience minimum.", True),
        ("Minimum 4 ans dans un poste similaire.", True),
        ("Au moins 3 ans d'expérience.", True),
        ("3 ans minimum.", True),
        # Sous le seuil, ou sans durée chiffrée.
        ("2 ans minimum dans un poste similaire.", False),
        ("1 an minimum.", False),
        ("Expérience souhaitée, débutant accepté.", False),
        ("Un minimum de rigueur est attendu.", False),
    ],
)
def test_experience_minimum_dans_le_texte(description, attendu):
    assert exige_experience_dans_le_texte(offre("A", description=description)) is attendu


@pytest.mark.parametrize(
    "champs",
    [
        {"description": "Taux journalier (TJM) : 450"},
        {"description": "TJM selon profil."},
        {"description": "Rémunération : 480€ / jour."},
        {"description": "Entre 430 et 450 euros par jour."},
        {"salaire": "500 € / jour"},
    ],
)
def test_remuneration_au_jour_detectee(champs):
    assert est_remunere_au_jour(offre("A", **champs)) is True


@pytest.mark.parametrize(
    "champs",
    [
        {"salaire": "Annuel de 45000 Euros"},
        {"salaire": "Mensuel de 2200 Euros", "description": "CDI, 35 heures."},
        {"description": "Une journée de télétravail par semaine."},
    ],
)
def test_remuneration_salariee(champs):
    assert est_remunere_au_jour(offre("A", **champs)) is False


def test_tjm_ecarte_au_prefiltre():
    tjm = offre("B", intitule="Consultant data", description="Taux journalier (TJM) : 600")

    retenues, ecartees = prefiltrer([offre("A"), tjm])

    assert [o["id"] for o in retenues] == ["A"]
    assert ecartees == [(tjm, MOTIF_TJM)]


# ------------------------------------------------- dédoublonnage entre sources


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        ("Développeur Java (H/F)", "developpeur java"),
        ("DÉVELOPPEUR JAVA H/F", "developpeur java"),
        ("Développeur  Java   (F/H)", "developpeur java"),
        ("Développeur Java - (H/F)", "developpeur java"),
    ],
)
def test_normalisation_des_intitules(brut, attendu):
    assert normaliser_intitule(brut) == attendu


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        ("59 - Lille", "lille"),
        ("Lille", "lille"),
        ("92 - Issy-les-Moulineaux", "issy les moulineaux"),
        ("75 - PARIS", "paris"),
        ("Ile-de-France", "ile de france"),
        (None, ""),
    ],
)
def test_normalisation_des_villes(brut, attendu):
    assert normaliser_ville(brut) == attendu


def test_doublon_entre_deux_sources_meme_ville():
    # La même annonce diffusée par deux intermédiaires : entreprises différentes,
    # intitulé et ville identiques.
    premiere = offre("A", intitule="Technicien support (H/F)", entreprise="MANPOWER")
    seconde = offre("B", intitule="Technicien Support H/F", entreprise="Randstad")

    retenues, ecartees = prefiltrer([premiere, seconde])

    assert [o["id"] for o in retenues] == ["A"]
    assert ecartees == [(seconde, MOTIF_DOUBLON)]


def test_meme_intitule_dans_deux_villes_conserve():
    lille = offre("A", intitule="Technicien support (H/F)", entreprise="MANPOWER")
    lyon = offre("B", intitule="Technicien support (H/F)", entreprise="Randstad", lieu="69 - Lyon")

    retenues, _ = prefiltrer([lille, lyon])

    assert [o["id"] for o in retenues] == ["A", "B"]


def test_doublon_meme_entreprise_autre_ville():
    paris = offre("A", intitule="Testeur QA", entreprise="ACME", lieu="75 - Paris")
    lyon = offre("B", intitule="Testeur QA", entreprise="ACME", lieu="69 - Lyon")

    retenues, ecartees = prefiltrer([paris, lyon])

    # Même intitulé et même entreprise : l'annonce est considérée comme republiée.
    assert [o["id"] for o in retenues] == ["A"]
    assert ecartees[0][1] == MOTIF_DOUBLON


# ------------------------------------------- drapeaux déterministes et verdict


def test_drapeaux_deterministes_cumules():
    complete = offre(
        "A",
        entreprise="Forums Talents Handicap",
        description="Permis B obligatoire. Formation bac+5 exigée. 5 ans minimum.",
    )

    assert drapeaux_deterministes(complete) == ["rqth", "permis", "bac5", "experience"]


def test_aucun_drapeau_deterministe_sur_une_offre_neutre():
    assert drapeaux_deterministes(offre("A")) == []


def test_drapeaux_du_modele_et_de_python_se_cumulent():
    lot = [offre("A", description="Permis B obligatoire.")]
    resultats = [
        {"id": "A", "score": 60, "resume": "", "drapeaux": ["teletravail"], "verdict": "peut-etre"}
    ]

    (enrichi,) = ajouter_drapeaux_deterministes(lot, resultats)

    assert enrichi["drapeaux"] == ["teletravail", "permis"]


@pytest.mark.parametrize("drapeau", ["permis", "telephone", "bac5", "freelance"])
def test_drapeau_redhibitoire_impose_le_verdict_non(drapeau):
    resultats = [
        {"id": "A", "score": 95, "resume": "", "drapeaux": [drapeau], "verdict": "postuler"}
    ]

    (resultat,) = appliquer_regle_verdict(resultats)

    assert resultat["verdict"] == "non"
    # Le score du modèle est conservé : il dit l'intérêt, pas l'accessibilité.
    assert resultat["score"] == 95


@pytest.mark.parametrize("drapeaux", [[], ["experience"], ["rqth"], ["teletravail", "alternance"]])
def test_verdict_conserve_sans_drapeau_redhibitoire(drapeaux):
    resultats = [
        {"id": "A", "score": 80, "resume": "", "drapeaux": drapeaux, "verdict": "postuler"}
    ]

    assert appliquer_regle_verdict(resultats)[0]["verdict"] == "postuler"


def test_regle_de_verdict_appliquee_par_le_tri_d_un_lot():
    client = FauxLLM(
        reponses=[
            reponse({"id": "A", "score": 90, "resume": "ok", "drapeaux": [], "verdict": "postuler"})
        ]
    )
    lot = [offre("A", description="Le permis B est obligatoire pour ce poste.")]

    (resultat,) = trier_lot(client, CRITERES, lot)

    assert resultat["drapeaux"] == ["permis"]
    assert resultat["verdict"] == "non"
    assert resultat["score"] == 90


def test_le_prompt_annonce_la_regle_des_drapeaux_redhibitoires():
    prompt = construire_prompt(CRITERES, [offre("A")])

    assert 'classée "non"' in prompt
    for drapeau in DRAPEAUX_REDHIBITOIRES:
        assert f'"{drapeau}"' in prompt
