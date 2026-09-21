"""Interface en ligne de commande : lit la configuration, interroge l'API, affiche le neuf."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from job_radar.client import (
    PUBLIEE_DEPUIS_VALIDES,
    TAILLE_PAGE,
    ClientFranceTravail,
    ErreurAuthentification,
    ErreurRecherche,
    normaliser_mot_cle,
    reduire_offre,
)
from job_radar.llm import DELAI_MAX_DEFAUT, MODELE_DEFAUT, creer_client
from job_radar.stockage import CHEMIN_BASE_DEFAUT, Historique
from job_radar.tri import (
    DRAPEAUX_BONUS,
    DRAPEAUX_ELIMINATOIRES,
    TAILLE_LOT_DEFAUT,
    VERDICTS_RETENUS,
    ErreurTri,
    charger_criteres,
    decouper_en_lots,
    fusionner,
    prefiltrer,
    trier,
)

CHEMIN_CONFIG_DEFAUT = Path("config.toml")
CHEMIN_SORTIE_DEFAUT = Path("data/nouvelles.json")
CHEMIN_SELECTION_DEFAUT = Path("data/selection.json")


class ErreurConfiguration(RuntimeError):
    """Configuration ou identifiants invalides."""


def charger_config(chemin: str | Path = CHEMIN_CONFIG_DEFAUT) -> dict[str, Any]:
    """Charge et valide les critères de recherche depuis un fichier TOML."""
    chemin = Path(chemin)
    if not chemin.exists():
        raise ErreurConfiguration(f"Fichier de configuration introuvable : {chemin}")

    with chemin.open("rb") as fichier:
        brut = tomllib.load(fichier)

    recherche = brut.get("recherche", brut)
    mots_cles_brut = recherche.get("mots_cles") or []
    # Une liste de départements vide est volontaire : la recherche porte alors sur
    # toute la France, avec une seule requête par mot-clé.
    departements = recherche.get("departements") or []
    if not mots_cles_brut:
        raise ErreurConfiguration(
            f"{chemin} doit définir au moins un mot-clé dans la section [recherche]."
        )

    try:
        mots_cles = [normaliser_mot_cle(str(mot)) for mot in mots_cles_brut]
    except ValueError as erreur:
        raise ErreurConfiguration(f"{chemin} : {erreur}") from erreur

    publiee_depuis = int(recherche.get("publiee_depuis", 7))
    if publiee_depuis not in PUBLIEE_DEPUIS_VALIDES:
        raise ErreurConfiguration(
            f"publiee_depuis doit valoir l'une de {PUBLIEE_DEPUIS_VALIDES}, reçu {publiee_depuis}."
        )

    tri = brut.get("tri") or {}

    return {
        "mots_cles": mots_cles,
        "departements": [str(dept) for dept in departements],
        "publiee_depuis": publiee_depuis,
        "pages_max": max(1, int(recherche.get("pages_max", 1))),
        "tri": {
            # Le fichier de critères vit hors du dépôt : il est personnel.
            "criteres": str(tri["criteres"]) if tri.get("criteres") else None,
            "modele": str(tri.get("modele") or MODELE_DEFAUT),
            "taille_lot": max(1, int(tri.get("taille_lot", TAILLE_LOT_DEFAUT))),
            "delai_max": max(1, int(tri.get("delai_max", DELAI_MAX_DEFAUT))),
            "exclure_rqth": bool(tri.get("exclure_rqth", False)),
        },
    }


def lire_identifiants() -> tuple[str, str]:
    """Récupère FT_CLIENT_ID / FT_CLIENT_SECRET depuis l'environnement ou le fichier .env."""
    load_dotenv()
    client_id = os.getenv("FT_CLIENT_ID", "").strip()
    client_secret = os.getenv("FT_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ErreurConfiguration(
            "FT_CLIENT_ID et FT_CLIENT_SECRET sont requis : copiez .env.example vers .env "
            "et renseignez vos identifiants France Travail."
        )
    return client_id, client_secret


def collecter(
    client: ClientFranceTravail,
    config: dict[str, Any],
    bavard: bool = True,
) -> list[dict[str, Any]]:
    """Lance une recherche par couple mot-clé / département et dédoublonne par identifiant.

    Sans département configuré, une seule recherche par mot-clé est lancée sur toute
    la France. Une recherche en échec est signalée sans interrompre les suivantes ;
    un refus d'authentification, lui, remonte immédiatement (`ErreurAuthentification`).
    """
    offres: dict[str, dict[str, Any]] = {}
    # `None` = pas de filtre géographique, donc une requête pour toute la France.
    zones: list[str | None] = list(config["departements"]) or [None]

    for mot in config["mots_cles"]:
        for departement in zones:
            for page in range(config["pages_max"]):
                try:
                    resultats = client.rechercher(
                        mots_cles=mot,
                        departement=departement,
                        publiee_depuis=config["publiee_depuis"],
                        page=page,
                    )
                except ErreurRecherche as erreur:
                    print(f"  ! {erreur}", file=sys.stderr)
                    break

                if bavard:
                    zone = f"dép. {departement}" if departement else "France entière"
                    print(f"  {mot!r} / {zone} / page {page + 1} : {len(resultats)} offres")

                for brute in resultats:
                    offre = reduire_offre(brute)
                    if offre["id"]:
                        offres.setdefault(offre["id"], offre)

                # Page incomplète : inutile de demander la suivante.
                if len(resultats) < TAILLE_PAGE:
                    break

    return list(offres.values())


def afficher(offres: list[dict[str, Any]]) -> None:
    """Affiche les nouvelles offres de façon lisible dans le terminal."""
    for offre in offres:
        entreprise = offre["entreprise"] or "entreprise non précisée"
        print(f"\n• {offre['intitule']} — {entreprise}")
        details = [offre["lieu"], offre["contrat"], offre["salaire"], offre["experience"]]
        print("  " + " | ".join(detail for detail in details if detail))
        if offre["alternance"]:
            print("  (alternance)")
        if offre["url"]:
            print(f"  {offre['url']}")


def ecrire_json(offres: list[dict[str, Any]], chemin: str | Path) -> None:
    """Écrit les nouvelles offres dans un fichier JSON indenté."""
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(
        json.dumps(offres, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def construire_parseur() -> argparse.ArgumentParser:
    parseur = argparse.ArgumentParser(
        prog="job-radar",
        description="Veille d'offres d'emploi via l'API France Travail (Offres d'emploi v2).",
    )
    sous_parseurs = parseur.add_subparsers(dest="commande")

    commun = argparse.ArgumentParser(add_help=False)
    commun.add_argument(
        "--config",
        default=CHEMIN_CONFIG_DEFAUT,
        type=Path,
        help=f"fichier TOML des critères (défaut : {CHEMIN_CONFIG_DEFAUT})",
    )
    commun.add_argument(
        "--base",
        default=CHEMIN_BASE_DEFAUT,
        type=Path,
        help=f"base SQLite de l'historique (défaut : {CHEMIN_BASE_DEFAUT})",
    )

    collecte = sous_parseurs.add_parser(
        "collecter",
        parents=[commun],
        help="interroger France Travail et afficher les offres jamais vues (par défaut)",
    )
    collecte.add_argument(
        "--sortie",
        default=CHEMIN_SORTIE_DEFAUT,
        type=Path,
        help=f"fichier JSON des nouvelles offres (défaut : {CHEMIN_SORTIE_DEFAUT})",
    )
    collecte.add_argument(
        "--sans-historique",
        action="store_true",
        help="afficher les offres sans rien enregistrer dans l'historique",
    )
    collecte.add_argument(
        "--silencieux",
        action="store_true",
        help="ne pas détailler la progression des recherches",
    )

    tri = sous_parseurs.add_parser(
        "trier",
        parents=[commun],
        help="trier par LLM les offres collectées et pas encore triées",
    )
    tri.add_argument(
        "--sortie",
        default=CHEMIN_SELECTION_DEFAUT,
        type=Path,
        help=f"fichier JSON de la sélection (défaut : {CHEMIN_SELECTION_DEFAUT})",
    )
    tri.add_argument(
        "--limite",
        type=int,
        default=None,
        metavar="N",
        help="ne trier que les N offres les plus récentes",
    )
    tri.add_argument(
        "--simulation",
        action="store_true",
        help="annoncer ce qui partirait au LLM, sans rien envoyer",
    )
    tri.add_argument(
        "--reinitialiser",
        action="store_true",
        help="effacer les résultats de tri existants (les offres sont conservées) "
        "avant de retrier, par exemple après un changement de critères",
    )
    tri.add_argument(
        "--modele",
        default=None,
        help="modèle à utiliser, au lieu de celui de config.toml",
    )
    tri.add_argument(
        "--importer",
        dest="importer_json",
        type=Path,
        default=None,
        metavar="FICHIER",
        help=(
            "compléter l'historique avec le contenu d'un export JSON (bases créées avant l'étape 2)"
        ),
    )
    return parseur


#: Sous-commandes reconnues ; toute autre entrée est traitée comme « collecter ».
COMMANDES = ("collecter", "trier")


def _argv_normalise(argv: list[str]) -> list[str]:
    """Ajoute la sous-commande par défaut pour garder ``job-radar`` seul fonctionnel."""
    if argv and (argv[0] in COMMANDES or argv[0] in ("-h", "--help")):
        return argv
    return ["collecter", *argv]


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée de la commande ``job-radar``."""
    argv = _argv_normalise(list(argv if argv is not None else sys.argv[1:]))
    args = construire_parseur().parse_args(argv)

    try:
        config = charger_config(args.config)
    except ErreurConfiguration as erreur:
        print(f"Erreur : {erreur}", file=sys.stderr)
        return 2

    if args.commande == "trier":
        return commande_trier(args, config)
    return commande_collecter(args, config)


def commande_collecter(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Collecte les offres et n'affiche que celles jamais vues."""
    try:
        client_id, client_secret = lire_identifiants()
    except ErreurConfiguration as erreur:
        print(f"Erreur : {erreur}", file=sys.stderr)
        return 2

    bavard = not args.silencieux
    if bavard:
        zone = (
            f"{len(config['departements'])} département(s)"
            if config["departements"]
            else "France entière"
        )
        print(
            f"Recherche : {len(config['mots_cles'])} mot(s)-clé × {zone}, "
            f"offres publiées depuis {config['publiee_depuis']} jour(s), "
            "triées par date de création décroissante"
        )

    client = ClientFranceTravail(client_id, client_secret)
    try:
        offres = collecter(client, config, bavard=bavard)
    except ErreurAuthentification as erreur:
        # Inutile de poursuivre : aucune recherche ne peut aboutir sans jeton valide.
        print(
            f"Erreur : {erreur}\nVérifiez FT_CLIENT_ID et FT_CLIENT_SECRET dans .env, "
            "ainsi que la souscription de votre application à l'API Offres d'emploi v2.",
            file=sys.stderr,
        )
        return 3

    with Historique(args.base) as historique:
        nouvelles = historique.filtrer_nouvelles(offres)
        if not args.sans_historique:
            historique.enregistrer(nouvelles)

    print(f"\n{len(offres)} offre(s) récupérée(s), dont {len(nouvelles)} nouvelle(s).")
    if nouvelles:
        afficher(nouvelles)
        ecrire_json(nouvelles, args.sortie)
        print(f"\nDétail complet écrit dans {args.sortie}")
        print("Triez-les avec : job-radar trier")

    return 0


LARGEUR_HORS_TERMINAL = 150


def creer_console() -> Console:
    """Console rich adaptée à la sortie.

    Hors terminal (redirection, cron), rich se rabat sur 80 colonnes et le tableau
    devient illisible : on force alors une largeur confortable.
    """
    if sys.stdout.isatty():
        return Console()
    return Console(width=int(os.environ.get("COLUMNS") or LARGEUR_HORS_TERMINAL))


def commande_trier(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Trie les offres collectées et pas encore triées : pré-filtre, puis LLM."""
    console = creer_console()
    reglages = config["tri"]
    modele = args.modele or reglages["modele"]

    if not reglages["criteres"]:
        print(
            "Erreur : renseignez [tri].criteres dans config.toml (chemin du fichier "
            "Markdown de critères, voir criteres.example.md).",
            file=sys.stderr,
        )
        return 2

    try:
        criteres = charger_criteres(reglages["criteres"])
    except ErreurTri as erreur:
        print(f"Erreur : {erreur}", file=sys.stderr)
        return 2

    with Historique(args.base) as historique:
        if args.importer_json is not None:
            complete = historique.importer_contenu(_lire_json(args.importer_json))
            console.print(f"{complete} offre(s) complétée(s) depuis {args.importer_json}.")

        if args.reinitialiser:
            if args.simulation:
                # Effacer serait une vraie perte : la simulation n'en prend pas le risque.
                console.print(
                    f"[yellow]Simulation : les {historique.compter_tries()} résultat(s) "
                    "de tri existants n'ont pas été effacés.[/yellow]"
                )
            else:
                console.print(
                    f"{historique.effacer_tri()} résultat(s) de tri effacé(s) ; "
                    "les offres sont conservées."
                )

        a_trier = historique.offres_a_trier(limite=args.limite)
        sans_contenu = historique.compter_sans_contenu()

        if sans_contenu:
            console.print(
                f"[yellow]{sans_contenu} offre(s) de l'historique sont stockées sans leur "
                "contenu et ne peuvent pas être triées ; relancez-les avec "
                "[bold]--importer data/nouvelles.json[/bold].[/yellow]"
            )

        if not a_trier:
            if sans_contenu:
                console.print("Aucune offre triable : voir le message ci-dessus.")
            else:
                console.print("Aucune offre à trier : tout est déjà passé au tri.")
            return 0

        retenues, ecartees = prefiltrer(a_trier, exclure_rqth=reglages["exclure_rqth"])
        _afficher_prefiltre(console, len(a_trier), retenues, ecartees)

        if not retenues:
            return 0

        lots = list(decouper_en_lots(retenues, reglages["taille_lot"]))
        if args.simulation:
            console.print(
                f"\n[bold]Simulation[/bold] : {len(retenues)} offre(s) partiraient au "
                f"modèle [bold]{modele}[/bold] en {len(lots)} lot(s) de "
                f"{reglages['taille_lot']} au maximum, avec un délai de "
                f"{reglages['delai_max']} s par lot. Rien n'a été envoyé."
            )
            return 0

        def debut_de_lot(numero: int, total: int, taille: int) -> None:
            console.print(f"  lot {numero}/{total} ({taille} offres) → {modele}…")

        def fin_de_lot(
            numero: int, total: int, duree: float, notees: int, motif: str | None
        ) -> None:
            if motif is None:
                console.print(
                    f"  lot {numero}/{total} : {notees} offre(s) notée(s) en {duree:.1f} s"
                )
            else:
                console.print(
                    f"  [red]lot {numero}/{total} : abandonné après {duree:.1f} s[/red] ({motif})"
                )

        console.print(
            f"\nTri de {len(retenues)} offre(s) par [bold]{modele}[/bold] en "
            f"{len(lots)} lot(s), {reglages['delai_max']} s au plus par lot :"
        )
        depart = time.monotonic()
        resultats, erreurs = trier(
            creer_client(modele, delai_max=reglages["delai_max"]),
            criteres,
            retenues,
            taille_lot=reglages["taille_lot"],
            rappel_debut=debut_de_lot,
            rappel_fin=fin_de_lot,
        )
        console.print(f"  total : {time.monotonic() - depart:.1f} s")
        historique.enregistrer_tri(resultats, modele)
        # La sélection est relue depuis la base : un passage partiel (--limite)
        # complète le fichier au lieu de l'écraser avec son seul lot.
        selection = historique.offres_triees(verdicts=VERDICTS_RETENUS)

    for erreur in erreurs:
        print(f"  ! {erreur}", file=sys.stderr)

    classees = fusionner(retenues, resultats)
    afficher_tri(console, classees)

    ecrire_json(selection, args.sortie)
    retenues_du_passage = sum(1 for o in classees if o["verdict"] in VERDICTS_RETENUS)
    console.print(
        f"\n{retenues_du_passage} offre(s) retenue(s) sur {len(classees)} triée(s) "
        f"dans ce passage ; {len(selection)} au total → {args.sortie}"
    )
    return 1 if erreurs and not resultats else 0


def _lire_json(chemin: Path) -> list[dict[str, Any]]:
    """Lit un export JSON d'offres réduites."""
    charge = json.loads(Path(chemin).read_text(encoding="utf-8"))
    if not isinstance(charge, list):
        raise ErreurConfiguration(f"{chemin} ne contient pas une liste d'offres")
    return charge


def _afficher_prefiltre(
    console: Console,
    total: int,
    retenues: list[dict[str, Any]],
    ecartees: list[tuple[dict[str, Any], str]],
) -> None:
    """Détaille ce que le pré-filtre a écarté, et pourquoi."""
    console.print(
        f"{total} offre(s) à trier : [bold]{len(retenues)}[/bold] retenue(s) par le "
        f"pré-filtre, {len(ecartees)} écartée(s) sans appel au LLM."
    )
    for motif, nombre in Counter(motif for _, motif in ecartees).most_common():
        console.print(f"  - {nombre} × {motif}")


def _drapeaux_colores(drapeaux: list[str]) -> str:
    """Colore les drapeaux : rouge si éliminatoire, vert si valorisant."""
    morceaux = []
    for drapeau in drapeaux:
        if drapeau in DRAPEAUX_ELIMINATOIRES:
            morceaux.append(f"[red]{drapeau}[/red]")
        elif drapeau in DRAPEAUX_BONUS:
            morceaux.append(f"[bold green]{drapeau}[/bold green]")
        else:
            morceaux.append(drapeau)
    return " ".join(morceaux)


COULEURS_VERDICT = {"postuler": "green", "peut-etre": "yellow", "non": "dim"}


def afficher_tri(console: Console, offres: list[dict[str, Any]]) -> None:
    """Affiche les offres triées, meilleur score d'abord."""
    if not offres:
        return

    table = Table(
        title="Offres triées par score décroissant",
        header_style="bold",
        show_lines=True,
        expand=True,
    )
    table.add_column("Score", justify="right", width=5)
    table.add_column("Verdict", width=9)
    table.add_column("Intitulé", max_width=32, overflow="fold")
    table.add_column("Entreprise", max_width=18, overflow="fold")
    table.add_column("Lieu", max_width=14, overflow="fold")
    table.add_column("Drapeaux", max_width=20, overflow="fold")
    table.add_column("Résumé", ratio=1, min_width=30, overflow="fold")

    for offre in offres:
        couleur = COULEURS_VERDICT.get(offre["verdict"], "")
        style = f"[{couleur}]" if couleur else ""
        fin = f"[/{couleur}]" if couleur else ""
        table.add_row(
            f"{style}{offre['score']}{fin}",
            f"{style}{offre['verdict']}{fin}",
            offre.get("intitule") or "",
            offre.get("entreprise") or "",
            offre.get("lieu") or "",
            _drapeaux_colores(offre.get("drapeaux") or []),
            offre.get("resume") or "",
        )

    console.print()
    console.print(table)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
