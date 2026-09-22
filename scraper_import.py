"""
scraper_import.py
------------------
Convertit l'export JSON brut de l'extension "FB Post Scraper" (bouton
« JSON (posts+comments) ») en deux DataFrames pandas au même format que
db.py (load_posts_dataframe / load_comments_dataframe), pour que le reste
de l'application (chat, tableau de bord, explorateur) n'ait pas à savoir
d'où viennent les données.

Format d'entrée attendu : une liste d'objets "post", chacun portant ses
commentaires imbriqués dans `commentsData`. Champs bruts observés :
  postId, feedbackId, actors, text, html, attachments, subattachments,
  comments, likes, shares, pageUrl, postUrl, publishTime, scrapedAt,
  commentsData: [ { commentId, postId, parentCommentId, isReply, actors,
                     text, html, attachments, likes, commentUrl, scrapedAt } ]

`actors` est une chaîne multi-lignes "id: ...\\nname: ...\\nurl: ..." : on
n'en extrait que le nom affiché.
"""

from __future__ import annotations

import re

import pandas as pd

_NAME_RE = re.compile(r"name:\s*(.+)")


def _actor_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return "Inconnu"
    m = _NAME_RE.search(raw)
    name = m.group(1).strip() if m else raw.strip()
    return name if name and name.lower() != "null" else "Inconnu"


def _to_int(value: object) -> int:
    """Le scraper laisse '' quand un compteur n'a pas pu être lu."""
    try:
        if value in (None, ""):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_scraper_export(raw_posts: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Args:
        raw_posts: le JSON désérialisé (liste de posts, chacun avec `commentsData`).

    Returns:
        (posts_df, comments_df) — mêmes colonnes que db.py, prêts pour
        detect_text_column / filter_publications / analytics.py.
    """
    post_rows: list[dict] = []
    comment_rows: list[dict] = []

    for i, post in enumerate(raw_posts):
        post_id = str(post.get("postId") or f"post_{i}")
        page_url = post.get("pageUrl") or ""

        post_rows.append({
            "post_id": post_id,
            "actors": _actor_name(post.get("actors")),
            "texte": post.get("text") or "",
            "nb_comments": _to_int(post.get("comments")) or len(post.get("commentsData") or []),
            "nb_likes": _to_int(post.get("likes")),
            "nb_shares": _to_int(post.get("shares")),
            "page_url": page_url,
            "post_url": post.get("postUrl") or "",
            "publish_time": post.get("publishTime") or post.get("scrapedAt") or None,
            "scraped_at": post.get("scrapedAt") or None,
        })

        for j, comment in enumerate(post.get("commentsData") or []):
            comment_rows.append({
                "comment_id": str(comment.get("commentId") or f"{post_id}_c{j}"),
                "post_id": post_id,
                "is_reply": bool(comment.get("isReply")),
                "parent_comment_id": comment.get("parentCommentId") or None,
                "actors": _actor_name(comment.get("actors")),
                "texte": comment.get("text") or "",
                "nb_likes": _to_int(comment.get("likes")),
                "comment_url": comment.get("commentUrl") or "",
                "scraped_at": comment.get("scrapedAt") or None,
                "page_url": page_url,  # dénormalisé : pratique pour filtrer par groupe sans jointure
            })

    posts_df = pd.DataFrame(post_rows, columns=[
        "post_id", "actors", "texte", "nb_comments", "nb_likes", "nb_shares",
        "page_url", "post_url", "publish_time", "scraped_at",
    ])
    comments_df = pd.DataFrame(comment_rows, columns=[
        "comment_id", "post_id", "is_reply", "parent_comment_id", "actors",
        "texte", "nb_likes", "comment_url", "scraped_at", "page_url",
    ])

    for df in (posts_df, comments_df):
        if "publish_time" in df.columns:
            df["publish_time"] = pd.to_datetime(df["publish_time"], errors="coerce", utc=True).dt.tz_localize(None)
        if "scraped_at" in df.columns:
            df["scraped_at"] = pd.to_datetime(df["scraped_at"], errors="coerce", utc=True).dt.tz_localize(None)

    return posts_df, comments_df
