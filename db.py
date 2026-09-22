"""
db.py
-----
Connexion à la base PostgreSQL du scraper (fb_scraper) et chargement des
posts sous forme de DataFrame pandas, pour réutiliser telle quelle la
logique de filtrage existante (build_search_terms / filter_publications)
sans dupliquer aucun code.

Aucune dépendance payante : SQLAlchemy + psycopg2 parlent directement à
PostgreSQL en local, pas d'API externe.
"""

from __future__ import annotations

import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

_engine = None

# Colonnes exposées pour chaque table (identiques à load_posts_dataframe /
# load_comments_dataframe, réutilisées par la recherche indexée SQL).
_TABLE_COLUMNS = {
    "social_post": (
        "post_id, actors, texte, nb_comments, nb_likes, nb_shares, "
        "page_url, post_url, publish_time, scraped_at"
    ),
    "social_commentaire": (
        "comment_id, post_id, is_reply, parent_comment_id, actors, texte, "
        "nb_likes, comment_url, scraped_at"
    ),
}

# Seuil de similarité trigram (0 à 1) en dessous duquel une ligne n'est pas
# considérée comme correspondant "flou" au terme recherché.
_TRIGRAM_SIMILARITY_THRESHOLD = 0.25


def get_engine():
    global _engine
    if _engine is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL n'est pas défini. Vérifie le fichier .env "
                "(voir .env.example) et que le mot de passe PostgreSQL y est renseigné."
            )
        _engine = create_engine(DATABASE_URL)
    return _engine


def load_posts_dataframe() -> pd.DataFrame:
    """
    Charge tous les posts de social_post en DataFrame, avec une colonne
    'texte' utilisable directement par detect_text_column/filter_publications.
    """
    query = f"SELECT {_TABLE_COLUMNS['social_post']} FROM social_post ORDER BY scraped_at DESC;"
    return pd.read_sql(query, get_engine())


def load_comments_dataframe(post_id: str | None = None) -> pd.DataFrame:
    """
    Charge les commentaires de social_commentaire. Si post_id est fourni,
    ne charge que les commentaires de ce post.
    """
    columns = _TABLE_COLUMNS["social_commentaire"]
    if post_id:
        query = f"""
            SELECT {columns}
            FROM social_commentaire
            WHERE post_id = %(post_id)s
            ORDER BY scraped_at DESC;
        """
        return pd.read_sql(query, get_engine(), params={"post_id": post_id})

    query = f"SELECT {columns} FROM social_commentaire ORDER BY scraped_at DESC;"
    return pd.read_sql(query, get_engine())


# ---------------------------------------------------------------------------
# Recherche indexée (mode "mot-clé exact" sur une base volumineuse) : le
# filtrage est poussé dans PostgreSQL (ILIKE + similarity trigram, voir
# schema-search-index.sql) au lieu de charger toute la table en mémoire et de
# filtrer ligne par ligne en Python. Bien plus rapide sur une grosse base.
# ---------------------------------------------------------------------------

def count_rows(table: str) -> int:
    """Nombre total de lignes d'une table (pour afficher total_rows sans
    charger toutes les données)."""
    engine = get_engine()
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def _search_table_sql(table: str, term: str, limit: int | None = None) -> pd.DataFrame:
    columns = _TABLE_COLUMNS[table]
    params: dict = {"pattern": f"%{term}%", "term": term, "threshold": _TRIGRAM_SIMILARITY_THRESHOLD}
    limit_sql = ""
    if limit:
        limit_sql = " LIMIT :limit"
        params["limit"] = limit

    engine = get_engine()

    # 1) Essai avec pg_trgm (ILIKE + tolérance aux fautes de frappe via
    #    similarity()) -> exploite l'index GIN si schema-search-index.sql a
    #    été exécuté ; sinon PostgreSQL fait un scan séquentiel classique
    #    (fonctionne quand même, juste plus lent sur une grosse base).
    try:
        sql = text(f"""
            SELECT {columns}
            FROM {table}
            WHERE texte ILIKE :pattern
               OR similarity(texte, :term) > :threshold
            ORDER BY similarity(texte, :term) DESC, scraped_at DESC
            {limit_sql}
        """)
        return pd.read_sql(sql, engine, params=params)
    except Exception:
        # 2) Repli : extension pg_trgm non installée sur cette base ->
        #    recherche ILIKE simple (fonctionne toujours, sans tolérance aux
        #    fautes de frappe côté SQL).
        sql = text(f"""
            SELECT {columns}
            FROM {table}
            WHERE texte ILIKE :pattern
            ORDER BY scraped_at DESC
            {limit_sql}
        """)
        return pd.read_sql(sql, engine, params={"pattern": params["pattern"], **({"limit": limit} if limit else {})})


def search_posts_sql(terms: list[str], limit: int | None = None) -> pd.DataFrame:
    """Recherche indexée de plusieurs termes (OR) dans social_post."""
    frames = [_search_table_sql("social_post", t, limit=limit) for t in terms if t.strip()]
    if not frames:
        return pd.DataFrame(columns=_TABLE_COLUMNS["social_post"].split(", "))
    combined = pd.concat(frames, ignore_index=True)
    return combined.drop_duplicates(subset="post_id").reset_index(drop=True)


def search_comments_sql(terms: list[str], limit: int | None = None) -> pd.DataFrame:
    """Recherche indexée de plusieurs termes (OR) dans social_commentaire."""
    frames = [_search_table_sql("social_commentaire", t, limit=limit) for t in terms if t.strip()]
    if not frames:
        return pd.DataFrame(columns=_TABLE_COLUMNS["social_commentaire"].split(", "))
    combined = pd.concat(frames, ignore_index=True)
    return combined.drop_duplicates(subset="comment_id").reset_index(drop=True)
