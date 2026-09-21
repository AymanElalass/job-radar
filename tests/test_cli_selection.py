"""Tests de la sous-commande « job-radar selection » (aucun appel réseau ni LLM)."""

import pytest

from job_radar import cli
from job_radar.stockage import Historique

CRITERES = "# Critères\nTesteur QA junior."


def offre(identifiant, intitule, **champs):
    base = {
        "id": identifiant,
        "intitule": intitule,
        "entreprise": f"Entreprise {identifiant}",
        "lieu": "59 - Lille",
        "contrat": "CDI",
        "experience": "Débutant accepté",
        "salaire": None,
        "alternance": False,
        "date_creation": "2026-09-20T09:00:00.000Z",
        "url": f"https://exemple.fr/offres/{identifiant}",
        "description": "Vous testez des applications web.",
        "rome": "M1805",
    }
    return base | champs


def verdict(identifiant, score, verdict, drapeaux=()):
    return {
        "id": identifiant,
        "score": score,
        "resume": "Un résumé.",
        "drapeaux": list(drapeaux),
        "verdict": verdict,
    }


@pytest.fixture
def projet(tmp_path):
    """Base contenant une sélection issue de deux passages de tri."""
    criteres = tmp_path / "criteres.md"
    criteres.write_text(CRITERES, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[recherche]\nmots_cles = ["testeur"]\n\n[tri]\ncriteres = "{criteres}"\n',
        encoding="utf-8",
    )

    base = tmp_path / "offres.db"
    with Historique(base) as historique:
        historique.enregistrer(
            [
                offre("AAA111", "Testeur QA"),
                offre("BBB222", "Chargé de recette"),
                offre("CCC333", "Développeur Java"),
                offre("DDD444", "Technicien support"),
            ]
        )
        # Premier passage.
        historique.enregistrer_tri(
            [
                verdict("AAA111", 90, "postuler", ["teletravail_complet"]),
                verdict("CCC333", 10, "non"),
            ],
            modele="sonnet",
        )
        # Second passage, plus tard.
        historique.enregistrer_tri(
            [verdict("BBB222", 95, "postuler"), verdict("DDD444", 55, "peut-etre")],
            modele="sonnet",
        )

    return {"config": config, "base": base}


def arguments(projet, *extra):
    return ["selection", "--config", str(projet["config"]), "--base", str(projet["base"]), *extra]


def test_affiche_toute_la_selection_des_deux_passages(projet, capsys):
    assert cli.main(arguments(projet)) == 0

    sortie = capsys.readouterr().out
    for identifiant in ("AAA111", "BBB222", "DDD444"):
        assert identifiant in sortie
    # Le « non » du premier passage n'est pas une offre retenue.
    assert "CCC333" not in sortie
    assert "3 offre(s) : postuler, peut-etre." in sortie


def test_triee_par_score_decroissant(projet, capsys):
    cli.main(arguments(projet))

    sortie = capsys.readouterr().out
    positions = [sortie.index(i) for i in ("BBB222", "AAA111", "DDD444")]
    assert positions == sorted(positions)


def test_numero_et_lien_affiches(projet, capsys):
    cli.main(arguments(projet))

    sortie = capsys.readouterr().out
    assert "AAA111" in sortie
    # Le lien est en clair, donc copiable même sans terminal cliquable.
    assert "https://exemple.fr/offres/AAA111" in sortie


def test_filtre_par_verdict(projet, capsys):
    assert cli.main(arguments(projet, "--verdict", "postuler")) == 0

    sortie = capsys.readouterr().out
    assert "AAA111" in sortie and "BBB222" in sortie
    assert "DDD444" not in sortie
    assert "2 offre(s) : postuler." in sortie


def test_verdict_non_consultable(projet, capsys):
    cli.main(arguments(projet, "--verdict", "non"))

    sortie = capsys.readouterr().out
    assert "CCC333" in sortie
    assert "AAA111" not in sortie


def test_verdict_inconnu_refuse(projet):
    with pytest.raises(SystemExit):
        cli.main(arguments(projet, "--verdict", "peut-être"))


def test_selection_vide(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('[recherche]\nmots_cles = ["testeur"]\n', encoding="utf-8")

    code = cli.main(["selection", "--config", str(config), "--base", str(tmp_path / "vide.db")])

    assert code == 0
    assert "Aucune offre" in capsys.readouterr().out


def test_drapeau_mis_en_valeur_non_coupe(projet, capsys):
    cli.main(arguments(projet, "--verdict", "postuler"))

    # La colonne est assez large pour ne pas replier le drapeau en pleine page.
    assert "teletravail_complet" in capsys.readouterr().out


def test_base_de_la_configuration_utilisee(tmp_path, capsys):
    criteres = tmp_path / "criteres.md"
    criteres.write_text(CRITERES, encoding="utf-8")
    base = tmp_path / "ailleurs.db"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[recherche]\nmots_cles = ["testeur"]\n\n[chemins]\nbase = "{base}"\n\n'
        f'[tri]\ncriteres = "{criteres}"\n',
        encoding="utf-8",
    )
    with Historique(base) as historique:
        historique.enregistrer([offre("ZZZ999", "Testeur")])
        historique.enregistrer_tri([verdict("ZZZ999", 80, "postuler")], modele="sonnet")

    cli.main(["selection", "--config", str(config)])

    assert "ZZZ999" in capsys.readouterr().out


def test_resume_abandonne_dans_un_terminal_etroit():
    """Huit colonnes dont un lien ne tiennent pas dans un terminal étroit."""
    from rich.console import Console

    largeurs = {}
    for largeur in (110, 160):
        console = Console(width=largeur, record=True)
        cli.afficher_tri(
            console,
            [offre("AAA111", "Testeur QA") | verdict("AAA111", 90, "postuler")],
            avec_lien=True,
        )
        largeurs[largeur] = console.export_text()

    assert "Résumé" not in largeurs[110]
    assert "Résumé" in largeurs[160]
    # Le numéro et le lien restent présents dans les deux cas.
    for texte in largeurs.values():
        assert "AAA111" in texte
