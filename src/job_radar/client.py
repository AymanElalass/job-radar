"""Client de l'API France Travail « Offres d'emploi v2 ».

Gère l'authentification OAuth2 (client credentials), la pagination via le paramètre
``range``, le tri par date et le respect de la limite de 10 requêtes par seconde.
"""

from __future__ import annotations

import time
from typing import Any

import requests

URL_TOKEN = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
REALM = "/partenaire"
SCOPES = "api_offresdemploiv2 o2dsoffre"
URL_RECHERCHE = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"

#: Nombre maximal d'offres renvoyées par requête (contrainte de l'API).
TAILLE_PAGE = 150

#: Délai minimal entre deux appels : l'API plafonne à 10 requêtes par seconde.
DELAI_MIN_ENTRE_APPELS = 0.11

#: Valeurs acceptées par le paramètre ``publieeDepuis`` (en jours).
PUBLIEE_DEPUIS_VALIDES = (1, 3, 7, 14, 31)

#: Valeur du paramètre ``sort`` : tri par date de création décroissante, puis pertinence
#: décroissante, puis distance croissante (0 = pertinence d'abord, 2 = distance d'abord).
SORT_DATE_DECROISSANTE = 1

#: Longueur minimale d'un mot-clé imposée par l'API : « chaque mot clé ou expression
#: comprend au moins 2 caractères ».
LONGUEUR_MIN_MOT_CLE = 2

#: Caractères non alphanumériques documentés comme acceptés dans ``motsCles``.
#: L'espace en fait partie : une expression de plusieurs mots est donc un mot-clé valide.
#: L'apostrophe et le tiret, courants en français, y sont ajoutés par tolérance.
CARACTERES_AUTORISES_MOT_CLE = " @#$%^&+./-\"'\u2019"


class ErreurFranceTravail(RuntimeError):
    """Erreur renvoyée par l'API ou par le service d'authentification."""


class ErreurAuthentification(ErreurFranceTravail):
    """Le service d'authentification a refusé les identifiants.

    Erreur fatale : sans jeton, aucune recherche ne peut aboutir. Elle n'est donc
    jamais rattrapée pour être réessayée recherche par recherche.
    """


class ErreurRecherche(ErreurFranceTravail):
    """Une recherche précise a échoué ; les autres peuvent encore aboutir."""


def normaliser_mot_cle(mot: str) -> str:
    """Normalise un mot-clé et vérifie qu'il est exploitable par l'API.

    Une expression de plusieurs mots (« alternance data ») est parfaitement valide :
    l'espace fait partie des caractères autorisés et l'API traite l'expression comme
    un seul mot-clé. La virgule, elle, sert de séparateur entre mots-clés côté API :
    la laisser passer lancerait une recherche différente de celle demandée, elle est
    donc refusée au profit de deux entrées distinctes dans la configuration.
    """
    normalise = " ".join(mot.split())

    if len(normalise) < LONGUEUR_MIN_MOT_CLE:
        raise ValueError(
            f"mot-clé {mot!r} trop court : l'API en exige au moins "
            f"{LONGUEUR_MIN_MOT_CLE} caractères"
        )
    if "," in normalise:
        raise ValueError(
            f"mot-clé {mot!r} : la virgule sépare les mots-clés côté API ; "
            "utilisez plutôt une entrée par mot-clé dans la configuration"
        )

    interdits = sorted(
        {
            caractere
            for caractere in normalise
            if not caractere.isalnum() and caractere not in CARACTERES_AUTORISES_MOT_CLE
        }
    )
    if interdits:
        raise ValueError(f"mot-clé {mot!r} : caractère(s) non autorisé(s) par l'API {interdits}")

    return normalise


def reduire_offre(offre: dict[str, Any]) -> dict[str, Any]:
    """Réduit une offre brute de l'API aux seuls champs utiles à la veille.

    La fonction est tolérante : les champs absents ou nuls deviennent ``None``,
    car l'API ne garantit la présence que de très peu de clés.
    """
    lieu = offre.get("lieuTravail") or {}
    entreprise = offre.get("entreprise") or {}
    salaire = offre.get("salaire") or {}

    return {
        "id": offre.get("id"),
        "intitule": offre.get("intitule"),
        "lieu": lieu.get("libelle"),
        "code_postal": lieu.get("codePostal"),
        "entreprise": entreprise.get("nom"),
        "contrat": offre.get("typeContratLibelle") or offre.get("typeContrat"),
        "salaire": salaire.get("libelle"),
        "experience": offre.get("experienceLibelle"),
        "rome": offre.get("romeCode"),
        "rome_libelle": offre.get("romeLibelle"),
        "alternance": bool(offre.get("alternance", False)),
        "date_creation": offre.get("dateCreation"),
        "url": _extraire_url(offre),
        "description": offre.get("description"),
    }


def _extraire_url(offre: dict[str, Any]) -> str | None:
    """Renvoie l'URL de l'offre : celle du partenaire d'origine, sinon celle de France Travail."""
    origine = offre.get("origineOffre") or {}

    for partenaire in origine.get("partenaires") or []:
        url = (partenaire or {}).get("url")
        if url:
            return url

    return origine.get("urlOrigine")


def construire_range(page: int) -> str:
    """Construit la valeur du paramètre ``range`` pour la page demandée (0 = première page)."""
    debut = page * TAILLE_PAGE
    return f"{debut}-{debut + TAILLE_PAGE - 1}"


class ClientFranceTravail:
    """Petit client HTTP autour des endpoints token et ``offres/search``."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        session: requests.Session | None = None,
        timeout: int = 20,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self.timeout = timeout
        self._token: str | None = None
        self._token_expire_le: float = 0.0
        self._dernier_appel: float = 0.0

    # ------------------------------------------------------------------ auth

    def _token_valide(self) -> str:
        """Renvoie un jeton d'accès, en le renouvelant si besoin (marge de 30 s)."""
        if self._token and time.monotonic() < self._token_expire_le:
            return self._token

        reponse = self.session.post(
            URL_TOKEN,
            params={"realm": REALM},
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": SCOPES,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        if reponse.status_code != 200:
            raise ErreurAuthentification(
                f"authentification refusée par France Travail ({reponse.status_code}) : "
                f"{reponse.text[:200]}"
            )

        charge = reponse.json()
        self._token = charge["access_token"]
        self._token_expire_le = time.monotonic() + int(charge.get("expires_in", 1500)) - 30
        return self._token

    # --------------------------------------------------------------- requête

    def _attendre_quota(self) -> None:
        """Espace les appels pour rester sous la limite de 10 requêtes/seconde."""
        attente = DELAI_MIN_ENTRE_APPELS - (time.monotonic() - self._dernier_appel)
        if attente > 0:
            time.sleep(attente)
        self._dernier_appel = time.monotonic()

    def rechercher(
        self,
        mots_cles: str,
        departement: str | None = None,
        publiee_depuis: int = 7,
        page: int = 0,
    ) -> list[dict[str, Any]]:
        """Renvoie les offres brutes d'une page de résultats, les plus récentes d'abord.

        ``departement`` à ``None`` omet le paramètre : la recherche porte alors sur
        toute la France. Les statuts 200 et 206 contiennent des résultats, 204 signifie
        « aucune offre ».
        """
        if publiee_depuis not in PUBLIEE_DEPUIS_VALIDES:
            raise ValueError(
                f"publiee_depuis doit valoir l'une de {PUBLIEE_DEPUIS_VALIDES}, "
                f"reçu {publiee_depuis!r}"
            )

        parametres: dict[str, Any] = {
            "motsCles": normaliser_mot_cle(mots_cles),
            "publieeDepuis": publiee_depuis,
            "range": construire_range(page),
            # Sans tri explicite, l'API classe par pertinence : la pagination ne
            # ramènerait pas forcément les offres les plus récentes.
            "sort": SORT_DATE_DECROISSANTE,
        }
        if departement is not None:
            parametres["departement"] = departement

        self._attendre_quota()
        reponse = self.session.get(
            URL_RECHERCHE,
            params=parametres,
            headers={
                "Authorization": f"Bearer {self._token_valide()}",
                "Accept": "application/json",
            },
            timeout=self.timeout,
        )

        if reponse.status_code == 204:
            return []
        if reponse.status_code not in (200, 206):
            zone = f"dép. {departement}" if departement is not None else "France entière"
            raise ErreurRecherche(
                f"recherche en échec ({reponse.status_code}) pour {mots_cles!r} / "
                f"{zone} : {reponse.text[:200]}"
            )

        return reponse.json().get("resultats") or []
