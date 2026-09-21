"""Doublures de test : session HTTP factice et client de recherche factice (aucun réseau)."""

from __future__ import annotations

from typing import Any


class FausseReponse:
    """Réponse HTTP minimale, suffisante pour le client."""

    def __init__(
        self, status_code: int, charge: dict[str, Any] | None = None, texte: str = ""
    ) -> None:
        self.status_code = status_code
        self._charge = charge or {}
        self.text = texte

    def json(self) -> dict[str, Any]:
        return self._charge


class FausseSession:
    """Session HTTP qui enregistre les appels et renvoie des réponses programmées."""

    def __init__(
        self,
        reponse_token: FausseReponse | None = None,
        reponses_recherche: list[FausseReponse] | None = None,
    ) -> None:
        self.reponse_token = reponse_token or FausseReponse(
            200, {"access_token": "jeton-de-test", "expires_in": 1500}
        )
        self.reponses_recherche = reponses_recherche or []
        self.appels_token: list[dict[str, Any]] = []
        self.appels_recherche: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FausseReponse:
        self.appels_token.append({"url": url, **kwargs})
        return self.reponse_token

    def get(self, url: str, **kwargs: Any) -> FausseReponse:
        self.appels_recherche.append({"url": url, **kwargs})
        if self.reponses_recherche:
            return self.reponses_recherche.pop(0)
        return FausseReponse(200, {"resultats": []})


class FauxClient:
    """Client de recherche factice, pour tester l'orchestration de la CLI."""

    def __init__(
        self,
        resultats_par_appel: list[list[dict[str, Any]]] | None = None,
        erreur: Exception | None = None,
    ) -> None:
        self.resultats_par_appel = resultats_par_appel
        #: Exception levée à chaque appel, pour simuler une API en échec.
        self.erreur = erreur
        self.appels: list[dict[str, Any]] = []

    def rechercher(
        self,
        mots_cles: str,
        departement: str | None = None,
        publiee_depuis: int = 7,
        page: int = 0,
    ) -> list[dict[str, Any]]:
        self.appels.append(
            {
                "mots_cles": mots_cles,
                "departement": departement,
                "publiee_depuis": publiee_depuis,
                "page": page,
            }
        )
        if self.erreur is not None:
            raise self.erreur
        if self.resultats_par_appel:
            return self.resultats_par_appel.pop(0)
        return [{"id": f"OFFRE-{len(self.appels)}", "intitule": "Poste"}]


class FauxLLM:
    """Client LLM factice : enregistre les prompts, renvoie des réponses programmées.

    Aucun appel à ``claude`` : les réponses sont fournies par le test.
    """

    def __init__(self, reponses: list[str] | None = None, defaut: str = "[]") -> None:
        self.reponses = list(reponses or [])
        self.defaut = defaut
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.reponses:
            return self.reponses.pop(0)
        return self.defaut

    @property
    def appels(self) -> int:
        return len(self.prompts)
