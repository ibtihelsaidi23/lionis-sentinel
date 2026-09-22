"""
groups.py
---------
Registre des groupes Facebook suivis : nom d'affichage, "logo" (une courte
étiquette visuelle, faute de vrai fichier image côté serveur) et URL du
groupe. Sert à transformer une pageUrl brute en identité lisible pour
l'admin, dans le tableau de bord et l'explorateur.

Pour ajouter un groupe : une seule ligne dans GROUPS. Le id est le seul
identifiant Facebook (le nombre dans /groups/<id>/) : c'est lui qui sert à
faire correspondre une publication à son groupe, peu importe le format
exact de l'URL scrapée (avec ou sans slash final, avec /posts/... ensuite,
etc.).
"""

from __future__ import annotations

import re

GROUPS: list[dict] = [
    {
        "id": "899217377222721",
        "name": "Bac Français : Les Candidats libres des centres étrangers",
        "emoji": "🎓",
        "color": "#17635F",
        "url": "https://www.facebook.com/groups/899217377222721",
    },
    {
        "id": "451387466689773",
        "name": "BAC Français",
        "emoji": "🇫🇷",
        "color": "#1D4ED8",
        "url": "https://www.facebook.com/groups/451387466689773",
    },
    {
        "id": "144609388240152",
        "name": "Bac français en Tunisie : candidats libres",
        "emoji": "⚜️",
        "color": "#9E3B2E",
        "url": "https://www.facebook.com/groups/144609388240152",
    },
]

_BY_ID = {g["id"]: g for g in GROUPS}
_UNKNOWN = {"id": None, "name": "Groupe non identifié", "emoji": "❔", "color": "#8AA0A7", "url": None}

_GROUP_ID_RE = re.compile(r"/groups/(\d+)")


def extract_group_id(url: str | None) -> str | None:
    """Isole l'identifiant numérique du groupe dans une URL Facebook quelconque."""
    if not url or not isinstance(url, str):
        return None
    m = _GROUP_ID_RE.search(url)
    return m.group(1) if m else None


def match_group(url: str | None) -> dict:
    """Renvoie la fiche du groupe (nom, emoji, couleur) correspondant à une URL,
    ou une fiche générique si le groupe n'est pas dans le registre."""
    group_id = extract_group_id(url)
    return _BY_ID.get(group_id, _UNKNOWN)


def all_groups() -> list[dict]:
    """Tous les groupes suivis, y compris ceux sans données pour le moment."""
    return GROUPS
