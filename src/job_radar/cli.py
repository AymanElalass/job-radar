"""Interface en ligne de commande : lit la configuration, interroge l'API, affiche le neuf."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from job_radar.client import (
    PUBLIEE_DEPUIS_VALIDES,
    TAILLE_PAGE,
    ClientFranceTravail,
    ErreurAuthentification,
    ErreurRecherche,
    normaliser_mot_cle,
    reduire_offre,
)
from job_radar.stockage import CHEMIN_BASE_DEFAUT, Historique

CHEMIN_CONFIG_DEFAUT = Path("config.toml")
CHEMIN_SORTIE_DEFAUT = Path("data/nouvelles.json")


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

    return {
        "mots_cles": mots_cles,
        "departements": [str(dept) for dept in departements],
        "publiee_depuis": publiee_depuis,
        "pages_max": max(1, int(recherche.get("pages_max", 1))),
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
    parseur.add_argument(
        "--config",
        default=CHEMIN_CONFIG_DEFAUT,
        type=Path,
        help=f"fichier TOML des critères (défaut : {CHEMIN_CONFIG_DEFAUT})",
    )
    parseur.add_argument(
        "--base",
        default=CHEMIN_BASE_DEFAUT,
        type=Path,
        help=f"base SQLite de l'historique (défaut : {CHEMIN_BASE_DEFAUT})",
    )
    parseur.add_argument(
        "--sortie",
        default=CHEMIN_SORTIE_DEFAUT,
        type=Path,
        help=f"fichier JSON des nouvelles offres (défaut : {CHEMIN_SORTIE_DEFAUT})",
    )
    parseur.add_argument(
        "--sans-historique",
        action="store_true",
        help="afficher les offres sans rien enregistrer dans l'historique",
    )
    parseur.add_argument(
        "--silencieux",
        action="store_true",
        help="ne pas détailler la progression des recherches",
    )
    return parseur


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée de la commande ``job-radar``."""
    args = construire_parseur().parse_args(argv)

    try:
        config = charger_config(args.config)
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

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
