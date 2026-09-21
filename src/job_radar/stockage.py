"""Historique des offres, stocké en SQLite.

Ce module répond à deux questions et à elles seules : « cette offre est-elle
nouvelle ? » (table ``offres``) et « cette offre a-t-elle déjà été triée ? »
(table ``tri``). Il conserve aussi le contenu réduit de chaque offre, pour que
le tri puisse travailler sans relancer d'appel réseau.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHEMIN_BASE_DEFAUT = Path("data/offres.db")

SCHEMA_OFFRES = """
CREATE TABLE IF NOT EXISTS offres (
    id TEXT PRIMARY KEY,
    intitule TEXT,
    entreprise TEXT,
    date_creation TEXT,
    vue_le TEXT NOT NULL,
    donnees TEXT
)
"""

SCHEMA_TRI = """
CREATE TABLE IF NOT EXISTS tri (
    id TEXT PRIMARY KEY,
    score INTEGER NOT NULL,
    resume TEXT,
    drapeaux TEXT NOT NULL,
    verdict TEXT NOT NULL,
    modele TEXT,
    trie_le TEXT NOT NULL
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
        self.connexion.execute(SCHEMA_OFFRES)
        self.connexion.execute(SCHEMA_TRI)
        self._migrer()
        self.connexion.commit()

    def _migrer(self) -> None:
        """Ajoute les colonnes manquantes aux bases créées par une version antérieure."""
        colonnes = {ligne[1] for ligne in self.connexion.execute("PRAGMA table_info(offres)")}
        if "donnees" not in colonnes:
            self.connexion.execute("ALTER TABLE offres ADD COLUMN donnees TEXT")

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
                json.dumps(offre, ensure_ascii=False),
            )
            for offre in offres
        ]
        if not lignes:
            return 0

        curseur = self.connexion.executemany(
            "INSERT OR IGNORE INTO offres "
            "(id, intitule, entreprise, date_creation, vue_le, donnees) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            lignes,
        )
        self.connexion.commit()
        return curseur.rowcount

    def importer_contenu(self, offres: Iterable[dict[str, Any]]) -> int:
        """Complète le contenu des offres déjà connues mais stockées sans leurs données.

        Sert de passerelle pour les bases créées avant l'étape 2, dont les lignes ne
        contenaient que l'identifiant et l'intitulé : sans contenu, une offre ne peut
        pas être triée. Les offres inconnues sont ajoutées à l'historique.
        """
        complete = 0
        for offre in offres:
            donnees = json.dumps(offre, ensure_ascii=False)
            curseur = self.connexion.execute(
                "UPDATE offres SET donnees = ? WHERE id = ? AND donnees IS NULL",
                (donnees, offre["id"]),
            )
            complete += curseur.rowcount
        self.connexion.commit()
        complete += self.enregistrer(offre for offre in offres if not self._existe(offre["id"]))
        return complete

    def _existe(self, identifiant: str) -> bool:
        return (
            self.connexion.execute("SELECT 1 FROM offres WHERE id = ?", (identifiant,)).fetchone()
            is not None
        )

    # ------------------------------------------------------------------- tri

    def offres_a_trier(self, limite: int | None = None) -> list[dict[str, Any]]:
        """Renvoie les offres collectées, jamais triées, les plus récentes d'abord.

        Les offres dont le contenu n'a pas été conservé sont ignorées : elles ne
        contiennent pas de quoi juger l'annonce (voir :meth:`importer_contenu`).
        """
        requete = (
            "SELECT o.donnees FROM offres o "
            "LEFT JOIN tri t ON t.id = o.id "
            "WHERE t.id IS NULL AND o.donnees IS NOT NULL "
            "ORDER BY o.date_creation DESC, o.id"
        )
        parametres: tuple[Any, ...] = ()
        if limite is not None:
            requete += " LIMIT ?"
            parametres = (limite,)

        return [json.loads(ligne[0]) for ligne in self.connexion.execute(requete, parametres)]

    def enregistrer_tri(self, resultats: Iterable[dict[str, Any]], modele: str) -> int:
        """Mémorise le verdict du LLM pour chaque offre. Renvoie le nombre de lignes écrites."""
        maintenant = datetime.now(UTC).isoformat(timespec="seconds")
        lignes = [
            (
                resultat["id"],
                int(resultat["score"]),
                resultat.get("resume"),
                json.dumps(resultat.get("drapeaux") or [], ensure_ascii=False),
                resultat["verdict"],
                modele,
                maintenant,
            )
            for resultat in resultats
        ]
        if not lignes:
            return 0

        curseur = self.connexion.executemany(
            "INSERT OR REPLACE INTO tri "
            "(id, score, resume, drapeaux, verdict, modele, trie_le) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            lignes,
        )
        self.connexion.commit()
        return curseur.rowcount

    def offres_triees(self, verdicts: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Toutes les offres déjà triées, contenu et verdict réunis, meilleur score d'abord.

        Permet d'écrire la sélection complète, et non le seul dernier passage.
        """
        requete = (
            "SELECT o.donnees, t.score, t.resume, t.drapeaux, t.verdict "
            "FROM tri t JOIN offres o ON o.id = t.id "
            "WHERE o.donnees IS NOT NULL"
        )
        parametres: tuple[Any, ...] = ()
        if verdicts:
            marqueurs = ",".join("?" * len(verdicts))
            requete += f" AND t.verdict IN ({marqueurs})"
            parametres = tuple(verdicts)
        requete += " ORDER BY t.score DESC, o.id"

        return [
            json.loads(donnees)
            | {
                "score": score,
                "resume": resume,
                "drapeaux": json.loads(drapeaux),
                "verdict": verdict,
            }
            for donnees, score, resume, drapeaux, verdict in self.connexion.execute(
                requete, parametres
            )
        ]

    def effacer_tri(self) -> int:
        """Efface tous les résultats de tri, sans toucher aux offres.

        Sert à retrier l'ensemble après un changement de prompt ou de critères.
        """
        curseur = self.connexion.execute("DELETE FROM tri")
        self.connexion.commit()
        return curseur.rowcount

    def compter(self) -> int:
        """Nombre total d'offres dans l'historique."""
        return self.connexion.execute("SELECT COUNT(*) FROM offres").fetchone()[0]

    def compter_tries(self) -> int:
        """Nombre d'offres déjà triées."""
        return self.connexion.execute("SELECT COUNT(*) FROM tri").fetchone()[0]

    def compter_sans_contenu(self) -> int:
        """Nombre d'offres connues dont le contenu n'a pas été conservé."""
        return self.connexion.execute(
            "SELECT COUNT(*) FROM offres WHERE donnees IS NULL"
        ).fetchone()[0]
