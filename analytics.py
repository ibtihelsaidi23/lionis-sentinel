"""
analytics.py
------------
Couche analytique (dashboard + explorateur BI) au-dessus des mêmes données que
le chatbot : soit la base PostgreSQL du scraper (social_post /
social_commentaire), soit un fichier importé via /api/chatbot/upload.

Trois endpoints :
  GET  /api/analytics/overview  -> tous les indicateurs du tableau de bord
  POST /api/analytics/explore   -> agrégation libre (dimension x mesure x filtres)
  POST /api/analytics/export    -> la même agrégation, en .xlsx

Aucune dépendance supplémentaire : pandas + ce qui est déjà dans
requirements.txt. Les DataFrames chargés depuis PostgreSQL sont mis en cache
60 secondes pour éviter de relire toute la table à chaque graphique.
"""

from __future__ import annotations

import io
import time
from typing import Any, Callable

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from db import load_comments_dataframe, load_posts_dataframe
from groups import all_groups, extract_group_id, match_group
from nlp_matching import (
    CONCEPT_VARIANTS,
    detect_text_column,
    extract_top_keywords,
    filter_publications,
    normalize_text,
)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# Injecté par main.py pour permettre l'analyse d'un fichier importé
# (source = "session:<session_id>") sans import circulaire.
_session_provider: Callable[[str], dict | None] = lambda _sid: None


def set_session_provider(provider: Callable[[str], dict | None]) -> None:
    global _session_provider
    _session_provider = provider


# ---------------------------------------------------------------------------
# Chargement + cache
# ---------------------------------------------------------------------------

_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_CACHE_TTL_SECONDS = 60.0

WEEKDAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _cached(key: str, loader: Callable[[], pd.DataFrame], refresh: bool = False) -> pd.DataFrame:
    hit = _CACHE.get(key)
    if hit and not refresh and (time.time() - hit[0]) < _CACHE_TTL_SECONDS:
        return hit[1]
    df = loader()
    _CACHE[key] = (time.time(), df)
    return df


def _load_source(source: str, refresh: bool = False) -> tuple[pd.DataFrame, str, str]:
    """
    Renvoie (dataframe, colonne_texte, libellé_source).
    `source` vaut "posts", "comments", ou "session:<session_id>".
    """
    if source.startswith("session:"):
        session_id = source.split(":", 1)[1]
        session = _session_provider(session_id)
        if session is None:
            raise HTTPException(404, "Session introuvable. Importez d'abord un fichier.")
        return session["df"], session["text_column"], session["filename"]

    if source == "posts":
        df = _cached("posts", load_posts_dataframe, refresh)
        return df, "texte", "Publications (base)"

    if source == "comments":
        df = _cached("comments", load_comments_dataframe, refresh)
        return df, "texte", "Commentaires (base)"

    raise HTTPException(400, "source doit être 'posts', 'comments' ou 'session:<id>'.")


def _safe_load(source: str, refresh: bool = False) -> tuple[pd.DataFrame, str, str]:
    try:
        return _load_source(source, refresh)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            500,
            f"Impossible de charger les données ({source}) : {exc}. "
            "Vérifiez DATABASE_URL dans backend/.env.",
        )


# ---------------------------------------------------------------------------
# Préparation : colonnes dérivées (date, heure, engagement)
# ---------------------------------------------------------------------------

def _to_datetime(series: pd.Series) -> pd.Series:
    dt = pd.to_datetime(series, errors="coerce", utc=True)
    try:
        return dt.dt.tz_convert(None)
    except (TypeError, AttributeError):
        return dt


def _prepare(df: pd.DataFrame, text_column: str) -> pd.DataFrame:
    out = df.copy()

    # Date de référence : publish_time si exploitable, sinon scraped_at.
    date_col = None
    for candidate in ("publish_time", "scraped_at", "date", "created_at"):
        if candidate in out.columns:
            parsed = _to_datetime(out[candidate])
            if parsed.notna().any():
                out["_date"] = parsed
                date_col = candidate
                break
    if date_col is None:
        out["_date"] = pd.NaT

    for col, target in (("nb_likes", "_likes"), ("nb_comments", "_comments"), ("nb_shares", "_shares")):
        out[target] = pd.to_numeric(out[col], errors="coerce").fillna(0) if col in out.columns else 0

    out["_engagement"] = out["_likes"] + out["_comments"] + out["_shares"]
    out["_actor"] = (
        out["actors"].fillna("Inconnu").astype(str).str.strip().replace("", "Inconnu")
        if "actors" in out.columns
        else "Inconnu"
    )
    out["_text"] = out[text_column].fillna("").astype(str)
    out["_length"] = out["_text"].str.len()

    if "page_url" in out.columns:
        out["_group_id"] = out["page_url"].map(extract_group_id)
        out["_group_name"] = out["page_url"].map(lambda u: match_group(u)["name"])
    else:
        out["_group_id"] = None
        out["_group_name"] = "Groupe non identifié"
    return out


def _apply_filters(df: pd.DataFrame, text_column: str, filters: dict[str, Any] | None) -> pd.DataFrame:
    if not filters:
        return df
    out = df

    if filters.get("date_from"):
        out = out[out["_date"] >= pd.Timestamp(filters["date_from"])]
    if filters.get("date_to"):
        out = out[out["_date"] <= pd.Timestamp(filters["date_to"]) + pd.Timedelta(days=1)]
    if filters.get("actor"):
        out = out[out["_actor"].str.contains(str(filters["actor"]), case=False, na=False)]
    if filters.get("min_engagement"):
        out = out[out["_engagement"] >= float(filters["min_engagement"])]

    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        terms = [t.strip() for t in keyword.split(",") if t.strip()]
        if terms:
            out = filter_publications(out, text_column, terms, fuzzy_threshold=85).filtered_df
    return out


# ---------------------------------------------------------------------------
# Dimensions et mesures de l'explorateur BI
# ---------------------------------------------------------------------------

def _dimension_series(df: pd.DataFrame, dimension: str) -> pd.Series:
    if dimension == "day":
        return df["_date"].dt.strftime("%Y-%m-%d")
    if dimension == "week":
        return df["_date"].dt.to_period("W").astype(str).str.slice(0, 10)
    if dimension == "month":
        return df["_date"].dt.strftime("%Y-%m")
    if dimension == "hour":
        return df["_date"].dt.hour.map(lambda h: f"{int(h):02d}h" if pd.notna(h) else None)
    if dimension == "weekday":
        return df["_date"].dt.weekday.map(lambda d: WEEKDAYS_FR[int(d)] if pd.notna(d) else None)
    if dimension == "actor":
        return df["_actor"]
    if dimension == "group":
        return df["_group_name"]
    if dimension == "concept":
        return None  # traité à part (une ligne peut relever de plusieurs concepts)
    if dimension == "length_bucket":
        bins = [-1, 100, 300, 800, 2000, float("inf")]
        labels = ["< 100 car.", "100-300", "300-800", "800-2000", "> 2000"]
        return pd.cut(df["_length"], bins=bins, labels=labels).astype(str)
    if dimension == "engagement_bucket":
        bins = [-1, 0, 5, 20, 100, float("inf")]
        labels = ["aucune", "1-5", "6-20", "21-100", "100+"]
        return pd.cut(df["_engagement"], bins=bins, labels=labels).astype(str)
    raise HTTPException(400, f"Dimension inconnue : {dimension}")


MEASURES = {
    "count": ("Nombre de publications", lambda g: g.size()),
    "likes_sum": ("Total réactions", lambda g: g["_likes"].sum()),
    "comments_sum": ("Total commentaires", lambda g: g["_comments"].sum()),
    "shares_sum": ("Total partages", lambda g: g["_shares"].sum()),
    "engagement_sum": ("Engagement total", lambda g: g["_engagement"].sum()),
    "engagement_avg": ("Engagement moyen", lambda g: g["_engagement"].mean().round(2)),
    "likes_avg": ("Réactions moyennes", lambda g: g["_likes"].mean().round(2)),
    "actors_unique": ("Auteurs distincts", lambda g: g["_actor"].nunique()),
}

TIME_DIMENSIONS = {"day", "week", "month", "hour", "weekday"}


def _concept_table(df: pd.DataFrame, measure: str) -> list[dict]:
    """Répartition par concept métier (CONCEPT_VARIANTS de nlp_matching)."""
    normalized = df["_text"].map(normalize_text)
    rows = []
    for concept, variants in CONCEPT_VARIANTS.items():
        norm_variants = [normalize_text(v) for v in variants if v.strip()]
        mask = normalized.apply(lambda t, nv=norm_variants: any(v and v in t for v in nv))
        subset = df[mask]
        if subset.empty:
            rows.append({"label": concept.replace("_", " "), "value": 0, "rows": 0})
            continue
        grouped = subset.groupby(lambda _: 0)
        value = float(MEASURES[measure][1](grouped).iloc[0]) if measure != "count" else float(len(subset))
        rows.append({"label": concept.replace("_", " "), "value": value, "rows": int(len(subset))})
    return sorted(rows, key=lambda r: r["value"], reverse=True)


def _aggregate(df: pd.DataFrame, dimension: str, measure: str, limit: int, sort: str) -> list[dict]:
    if measure not in MEASURES:
        raise HTTPException(400, f"Mesure inconnue : {measure}")

    if dimension == "concept":
        return _concept_table(df, measure)[:limit]

    keys = _dimension_series(df, dimension)
    work = df.assign(_dim=keys).dropna(subset=["_dim"])
    work = work[work["_dim"].astype(str).str.lower() != "nan"]
    if work.empty:
        return []

    grouped = work.groupby("_dim")
    values = MEASURES[measure][1](grouped)
    counts = grouped.size()

    table = pd.DataFrame({"label": values.index.astype(str), "value": values.values, "rows": counts.values})

    if sort == "label" or (sort == "auto" and dimension in TIME_DIMENSIONS):
        if dimension == "weekday":
            order = {d: i for i, d in enumerate(WEEKDAYS_FR)}
            table = table.sort_values("label", key=lambda s: s.map(order))
        else:
            table = table.sort_values("label")
    else:
        table = table.sort_values("value", ascending=False)

    table = table.head(limit)
    return [
        {"label": str(r.label), "value": float(r.value), "rows": int(r.rows)}
        for r in table.itertuples()
    ]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class ExploreRequest(BaseModel):
    source: str = "posts"
    dimension: str = "day"
    measure: str = "count"
    limit: int = 20
    sort: str = "auto"  # "auto" | "value" | "label"
    filters: dict[str, Any] | None = None


@router.get("/overview")
async def overview(source: str = "posts", refresh: bool = False, top: int = 8):
    """Tout ce qu'affiche le tableau de bord, en un seul appel."""
    df_raw, text_column, label = _safe_load(source, refresh)

    if df_raw.empty:
        return {"source": source, "source_label": label, "empty": True}

    df = _prepare(df_raw, text_column)
    dates = df["_date"].dropna()

    kpis = {
        "rows": int(len(df)),
        "actors": int(df["_actor"].nunique()),
        "likes": int(df["_likes"].sum()),
        "comments": int(df["_comments"].sum()),
        "shares": int(df["_shares"].sum()),
        "engagement_avg": round(float(df["_engagement"].mean()), 1),
        "silent_share": round(float((df["_engagement"] == 0).mean() * 100), 1),
        "date_min": dates.min().strftime("%d/%m/%Y") if not dates.empty else None,
        "date_max": dates.max().strftime("%d/%m/%Y") if not dates.empty else None,
    }

    recent = df.sort_values("_date", ascending=False).head(top)
    url_col = "post_url" if "post_url" in df.columns else ("comment_url" if "comment_url" in df.columns else None)
    latest = [
        {
            "actor": r["_actor"],
            "text": (r["_text"][:220] + "…") if len(r["_text"]) > 220 else r["_text"],
            "engagement": int(r["_engagement"]),
            "date": r["_date"].strftime("%d/%m/%Y %H:%M") if pd.notna(r["_date"]) else "—",
            "url": r[url_col] if url_col and pd.notna(r.get(url_col)) else None,
        }
        for _, r in recent.iterrows()
    ]

    top_posts = df.sort_values("_engagement", ascending=False).head(top)
    best = [
        {
            "actor": r["_actor"],
            "text": (r["_text"][:160] + "…") if len(r["_text"]) > 160 else r["_text"],
            "likes": int(r["_likes"]),
            "comments": int(r["_comments"]),
            "shares": int(r["_shares"]),
            "engagement": int(r["_engagement"]),
            "url": r[url_col] if url_col and pd.notna(r.get(url_col)) else None,
        }
        for _, r in top_posts.iterrows()
    ]

    return {
        "source": source,
        "source_label": label,
        "empty": False,
        "kpis": kpis,
        "timeseries": _aggregate(df, "day", "count", 120, "label"),
        "engagement_series": _aggregate(df, "day", "engagement_sum", 120, "label"),
        "hours": _aggregate(df, "hour", "count", 24, "label"),
        "weekdays": _aggregate(df, "weekday", "count", 7, "label"),
        "top_actors": _aggregate(df, "actor", "count", top, "value"),
        "concepts": _concept_table(df, "count"),
        "keywords": extract_top_keywords(df, text_column, top_n=25),
        "latest": latest,
        "top_posts": best,
    }


@router.post("/explore")
async def explore(request: ExploreRequest):
    """Agrégation libre : c'est le moteur de l'explorateur BI du frontend."""
    df_raw, text_column, label = _safe_load(request.source)
    if df_raw.empty:
        return {"rows": [], "measure_label": "", "matched": 0, "total": 0}

    df = _prepare(df_raw, text_column)
    filtered = _apply_filters(df, text_column, request.filters)

    rows = _aggregate(filtered, request.dimension, request.measure, request.limit, request.sort) if len(filtered) else []

    return {
        "source_label": label,
        "dimension": request.dimension,
        "measure": request.measure,
        "measure_label": MEASURES.get(request.measure, ("", None))[0],
        "rows": rows,
        "matched": int(len(filtered)),
        "total": int(len(df)),
    }


@router.post("/export")
async def export(request: ExploreRequest):
    """Le résultat de l'explorateur, en classeur Excel (agrégat + lignes sources)."""
    df_raw, text_column, _ = _safe_load(request.source)
    if df_raw.empty:
        raise HTTPException(400, "Aucune donnée à exporter.")

    df = _prepare(df_raw, text_column)
    filtered = _apply_filters(df, text_column, request.filters)
    rows = _aggregate(filtered, request.dimension, request.measure, request.limit, request.sort)

    summary = pd.DataFrame(rows).rename(
        columns={"label": request.dimension, "value": MEASURES[request.measure][0], "rows": "Publications"}
    )
    detail = filtered.drop(columns=[c for c in filtered.columns if c.startswith("_")], errors="ignore")
    for col in detail.columns:
        if pd.api.types.is_datetime64_any_dtype(detail[col]) and getattr(detail[col].dt, "tz", None) is not None:
            detail[col] = detail[col].dt.tz_localize(None)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Synthese", index=False)
        detail.head(20000).to_excel(writer, sheet_name="Donnees", index=False)

    filename = f"bi_{request.dimension}_{request.measure}.xlsx"
    return StreamingResponse(
        io.BytesIO(buffer.getvalue()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/groups")
async def groups_overview(source: str = "posts", refresh: bool = False):
    """
    Une fiche par groupe suivi (voir groups.py) : publications, commentaires,
    engagement — y compris les groupes déjà enregistrés mais pas encore
    scrapés (affichés à zéro), pour que l'admin voie d'un coup d'œil l'état
    de sa couverture.
    """
    posts_df, text_column, label = _safe_load(source, refresh)

    comments_df = None
    if source.startswith("session:"):
        session = _session_provider(source.split(":", 1)[1])
        comments_df = session.get("comments_df") if session else None
    elif source == "posts":
        try:
            comments_df = _cached("comments", load_comments_dataframe, refresh)
        except Exception:
            comments_df = None

    if posts_df.empty:
        rows = [{**g, "posts": 0, "comments": 0, "likes": 0, "engagement": 0, "avg_engagement": 0} for g in all_groups()]
        return {"source_label": label, "groups": rows, "unmatched_posts": 0}

    df = _prepare(posts_df, text_column)

    counts = df.groupby("_group_id").size()
    engagement = df.groupby("_group_id")["_engagement"].sum()
    likes = df.groupby("_group_id")["_likes"].sum()

    comment_counts = pd.Series(dtype="int64")
    if comments_df is not None and not comments_df.empty and "page_url" in comments_df.columns:
        comment_counts = comments_df["page_url"].map(extract_group_id).value_counts()

    rows = []
    for g in all_groups():
        gid = g["id"]
        post_count = int(counts.get(gid, 0))
        rows.append({
            **g,
            "posts": post_count,
            "comments": int(comment_counts.get(gid, 0)),
            "likes": int(likes.get(gid, 0)),
            "engagement": int(engagement.get(gid, 0)),
            "avg_engagement": round(engagement.get(gid, 0) / post_count, 1) if post_count else 0,
        })

    unmatched = int(counts.get(None, 0)) if counts.index.isin([None]).any() else 0
    return {"source_label": label, "groups": rows, "unmatched_posts": unmatched}


@router.get("/meta")
async def meta():
    """Dimensions et mesures disponibles — le frontend construit ses menus avec ça."""
    return {
        "dimensions": [
            {"id": "day", "label": "Jour"},
            {"id": "week", "label": "Semaine"},
            {"id": "month", "label": "Mois"},
            {"id": "hour", "label": "Heure de publication"},
            {"id": "weekday", "label": "Jour de la semaine"},
            {"id": "actor", "label": "Auteur"},
            {"id": "group", "label": "Groupe"},
            {"id": "concept", "label": "Intention détectée"},
            {"id": "engagement_bucket", "label": "Palier d'engagement"},
            {"id": "length_bucket", "label": "Longueur du texte"},
        ],
        "measures": [{"id": k, "label": v[0]} for k, v in MEASURES.items()],
    }
