"""Historique des offres déjà vues, stocké en SQLite.

L'unique rôle de ce module est de répondre à la question « cette offre est-elle
nouvelle ? » et de mémoriser les offres affichées.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHEMIN_BASE_DEFAUT = Path("data/offres.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS offres (
    id TEXT PRIMARY KEY,
    intitule TEXT,
    entreprise TEXT,
    date_creation TEXT,
    vue_le TEXT NOT NULL
)
"""


class Historique:
    """Accès à la base des offres déjà vues.

    Utilisable comme gestionnaire de contexte :

    >>> with Historique(":memory:") as historique:
    ...     historique.filtrer_nouvelles([])
    []
    """

    def __init__(self, chemin: str | Path = CHEMIN_BASE_DEFAUT) -> None:
        self.chemin = chemin
        if str(chemin) != ":memory:":
            Path(chemin).parent.mkdir(parents=True, exist_ok=True)
        self.connexion = sqlite3.connect(chemin)
        self.connexion.execute(SCHEMA)
        self.connexion.commit()

    def __enter__(self) -> Historique:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.fermer()

    def fermer(self) -> None:
        self.connexion.close()

    # ----------------------------------------------------------- lecture

    def ids_connus(self, ids: Sequence[str]) -> set[str]:
        """Renvoie, parmi ``ids``, ceux déjà présents dans l'historique."""
        connus: set[str] = set()
        # SQLite limite le nombre de paramètres d'une requête : on interroge par lots.
        for debut in range(0, len(ids), 500):
            lot = ids[debut : debut + 500]
            marqueurs = ",".join("?" * len(lot))
            lignes = self.connexion.execute(f"SELECT id FROM offres WHERE id IN ({marqueurs})", lot)
            connus.update(ligne[0] for ligne in lignes)
        return connus

    def filtrer_nouvelles(self, offres: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Renvoie les offres jamais vues, dans leur ordre d'arrivée.

        Les doublons présents dans ``offres`` sont également écartés.
        """
        offres = list(offres)
        ids = [offre["id"] for offre in offres]
        connus = self.ids_connus(ids)

        nouvelles: list[dict[str, Any]] = []
        for offre in offres:
            if offre["id"] in connus:
                continue
            connus.add(offre["id"])
            nouvelles.append(offre)
        return nouvelles

    # ---------------------------------------------------------- écriture

    def enregistrer(self, offres: Iterable[dict[str, Any]]) -> int:
        """Mémorise les offres comme « vues ». Renvoie le nombre de lignes insérées."""
        maintenant = datetime.now(UTC).isoformat(timespec="seconds")
        lignes = [
            (
                offre["id"],
                offre.get("intitule"),
                offre.get("entreprise"),
                offre.get("date_creation"),
                maintenant,
            )
            for offre in offres
        ]
        if not lignes:
            return 0

        curseur = self.connexion.executemany(
            "INSERT OR IGNORE INTO offres (id, intitule, entreprise, date_creation, vue_le) "
            "VALUES (?, ?, ?, ?, ?)",
            lignes,
        )
        self.connexion.commit()
        return curseur.rowcount

    def compter(self) -> int:
        """Nombre total d'offres dans l'historique."""
        return self.connexion.execute("SELECT COUNT(*) FROM offres").fetchone()[0]
