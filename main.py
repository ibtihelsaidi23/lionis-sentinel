"""
main.py
-------
API FastAPI pour le chatbot assistant.

Endpoints :
  POST /api/chatbot/upload            -> upload d'un CSV/XLSX, détection colonne texte
  POST /api/chatbot/query             -> analyse en langage naturel + filtrage
  GET  /api/chatbot/download/{file_id} -> téléchargement du fichier filtré

Stockage : en mémoire pour la démo (dict `SESSIONS`). En production,
remplacer par Redis / une base de données / un stockage de fichiers (S3...).
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from nlp_matching import (
    build_search_terms,
    call_llm_for_intent,
    detect_text_column,
    extract_literal_keyword_terms,
    extract_top_keywords,
    filter_publications,
)
import analytics
import groups as groups_module
from scraper_import import parse_scraper_export
from db import (
    count_rows,
    load_comments_dataframe,
    load_posts_dataframe,
    search_comments_sql,
    search_posts_sql,
)

app = FastAPI(title="Chatbot Assistant API")

# En développement : autoriser le front local. À restreindre en production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# session_id -> {"df": DataFrame, "text_column": str, "filename": str}
SESSIONS: dict[str, dict] = {}
# file_id -> bytes (fichiers filtrés générés, prêts à être téléchargés)
GENERATED_FILES: dict[str, tuple[str, bytes]] = {}

# Dashboard / explorateur BI : routes /api/analytics/*. Le provider permet
# d'analyser aussi un fichier importé (source = "session:<session_id>").
analytics.set_session_provider(lambda session_id: SESSIONS.get(session_id))
app.include_router(analytics.router)


# ---------------------------------------------------------------------------
# Schémas
# ---------------------------------------------------------------------------

class UploadResponse(BaseModel):
    session_id: str
    filename: str
    row_count: int
    text_column: str
    columns: list[str]


class UploadJsonResponse(BaseModel):
    session_id: str
    filename: str
    post_count: int
    comment_count: int
    text_column: str
    groups: list[dict]


class QueryRequest(BaseModel):
    session_id: str
    message: str
    use_llm: bool = False
    # "auto" (défaut) : NLP + concepts connus + LLM optionnel.
    # "keyword" : recherche littérale exacte (+ tolérance fautes de frappe),
    # ignore complètement CONCEPT_VARIANTS — n'importe quel mot tapé est
    # cherché tel quel dans le texte des publications, "programmé" ou non.
    mode: str = "auto"


class QueryDbRequest(BaseModel):
    source: str = "posts"  # "posts" ou "comments"
    message: str
    use_llm: bool = False
    mode: str = "auto"


class QueryResponse(BaseModel):
    reply: str
    total_rows: int
    matched_rows: int
    matched_keywords: list[str]
    download_file_id: str | None = None
    download_filename: str | None = None
    llm_used: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_dataframe(filename: str, raw_bytes: bytes) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    buffer = io.BytesIO(raw_bytes)

    if suffix == ".csv":
        return pd.read_csv(buffer)
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(buffer)

    raise HTTPException(status_code=400, detail="Format non supporté (utilisez .csv, .xlsx ou .xls).")


def _slugify(text: str) -> str:
    keep = [c if c.isalnum() else "_" for c in text.lower()]
    slug = "".join(keep)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_") or "recherche"


def _prepare_df_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    """
    Excel n'accepte pas les datetimes "timezone-aware" (ex: colonne
    `scraped_at` en `timestamptz` quand les données viennent de PostgreSQL).
    On neutralise le fuseau horaire (UTC -> naïf) avant l'export, sans
    changer la valeur affichée pour l'utilisateur.
    """
    df = df.copy()
    for col in df.columns:
        series = df[col]
        if pd.api.types.is_datetime64_any_dtype(series) and getattr(series.dt, "tz", None) is not None:
            df[col] = series.dt.tz_localize(None)
        elif series.dtype == object:
            # Cas où pandas garde les datetimes tz-aware en objets Python (ex:
            # certains drivers SQL) plutôt qu'en dtype datetime64 vectorisé.
            sample = series.dropna()
            if not sample.empty and hasattr(sample.iloc[0], "tzinfo") and sample.iloc[0].tzinfo is not None:
                df[col] = series.map(lambda v: v.replace(tzinfo=None) if hasattr(v, "tzinfo") and v.tzinfo else v)
    return df


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/chatbot/upload", response_model=UploadResponse)
async def upload_file(file: UploadFile = File(...)):
    raw_bytes = await file.read()

    try:
        df = _read_dataframe(file.filename, raw_bytes)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - message utilisateur générique
        raise HTTPException(status_code=400, detail=f"Impossible de lire le fichier : {exc}")

    if df.empty:
        raise HTTPException(status_code=400, detail="Le fichier est vide.")

    try:
        text_column = detect_text_column(df)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {
        "df": df,
        "text_column": text_column,
        "filename": file.filename,
    }

    return UploadResponse(
        session_id=session_id,
        filename=file.filename,
        row_count=len(df),
        text_column=text_column,
        columns=list(df.columns),
    )


@app.post("/api/chatbot/upload-json", response_model=UploadJsonResponse)
async def upload_json_file(file: UploadFile = File(...)):
    """
    Import de l'export brut de l'extension FB Post Scraper — bouton
    « JSON (posts+comments) ». Contrairement à /upload (CSV/XLSX), un seul
    fichier apporte à la fois les publications et leurs commentaires,
    reliés par post_id, et déjà répartis entre les groupes suivis
    (voir groups.py) grâce à leur pageUrl.
    """
    raw_bytes = await file.read()
    try:
        raw_posts = json.loads(raw_bytes)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"JSON invalide : {exc}")

    if not isinstance(raw_posts, list):
        raise HTTPException(
            status_code=400,
            detail="Format inattendu : le fichier doit contenir une liste de publications "
                   "(export « JSON (posts+comments) » de l'extension).",
        )
    if not raw_posts:
        raise HTTPException(status_code=400, detail="Le fichier ne contient aucune publication.")

    posts_df, comments_df = parse_scraper_export(raw_posts)
    if posts_df.empty:
        raise HTTPException(status_code=400, detail="Aucune publication exploitable dans ce fichier.")

    try:
        text_column = detect_text_column(posts_df)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {
        "df": posts_df,
        "comments_df": comments_df,
        "text_column": text_column,
        "filename": file.filename,
    }

    present_ids = {gid for gid in posts_df["page_url"].map(groups_module.extract_group_id) if gid}
    groups_summary = [
        {**g, "has_data": g["id"] in present_ids}
        for g in groups_module.all_groups()
    ]

    return UploadJsonResponse(
        session_id=session_id,
        filename=file.filename,
        post_count=len(posts_df),
        comment_count=len(comments_df),
        text_column=text_column,
        groups=groups_summary,
    )


# ---------------------------------------------------------------------------
# Logique de filtrage partagée entre /query (upload), /query-db (base live,
# chargement complet) et /query-db en mode indexé (filtrage poussé en SQL)
# ---------------------------------------------------------------------------

def _build_query_response(
    total_rows: int,
    filtered_df: pd.DataFrame,
    search_terms: list[str],
    filename_base: str,
    mode: str = "auto",
    llm_used: bool = False,
    indexed: bool = False,
) -> QueryResponse:
    matched_rows = len(filtered_df)

    download_file_id = None
    download_filename = None
    if matched_rows > 0:
        download_file_id = str(uuid.uuid4())
        keyword_slug = "_".join(_slugify(k) for k in search_terms[:4])
        download_filename = f"{filename_base}_filtre_{keyword_slug}.xlsx"

        output_buffer = io.BytesIO()
        _prepare_df_for_excel(filtered_df).to_excel(output_buffer, index=False)
        GENERATED_FILES[download_file_id] = (download_filename, output_buffer.getvalue())

    criteria_lines = "\n".join(f"• {kw}" for kw in search_terms)
    if mode == "keyword" and indexed:
        mode_note = "🔍 Mode mot-clé exact (recherche indexée SQL — rapide sur grosse base)\n\n"
    elif mode == "keyword":
        mode_note = "🔍 Mode mot-clé exact (recherche littérale dans le texte brut)\n\n"
    elif llm_used:
        mode_note = "🤖 Compréhension assistée par LLM (Ollama, local)\n\n"
    else:
        mode_note = ""

    reply = (
        f"✅ Analyse terminée\n\n"
        f"{mode_note}"
        f"J'ai analysé {total_rows} publications.\n\n"
        f"🔎 Critères :\n{criteria_lines}\n\n"
        f"📊 Résultats : {matched_rows} publications trouvées."
    )

    return QueryResponse(
        reply=reply,
        total_rows=total_rows,
        matched_rows=matched_rows,
        matched_keywords=search_terms,
        download_file_id=download_file_id,
        download_filename=download_filename,
        llm_used=llm_used,
    )


def _run_query(
    df: pd.DataFrame,
    text_column: str,
    message: str,
    filename_base: str,
    use_llm: bool = False,
    mode: str = "auto",
) -> QueryResponse:
    search_terms: list[str] = []
    llm_used = False
    fuzzy_threshold = 88

    if mode == "keyword":
        # Mode "mot-clé exact" : ce que l'utilisateur tape est LE mot-clé,
        # littéralement — pas de dictionnaire, pas de LLM. On accepte un peu
        # plus de tolérance aux fautes de frappe (80 au lieu de 88) puisque
        # c'est une recherche volontaire et précise d'un seul terme.
        search_terms = extract_literal_keyword_terms(message)
        fuzzy_threshold = 80
    else:
        if use_llm:
            try:
                llm_result = call_llm_for_intent(message)
                search_terms = llm_result["search_terms"]
                llm_used = True
            except Exception:
                # Ollama pas lancé / modèle pas téléchargé / réponse invalide —
                # on retombe silencieusement sur le pipeline rules + fuzzy matching.
                search_terms = []

        if not search_terms:
            search_terms = build_search_terms(message)

    if not search_terms:
        return QueryResponse(
            reply=(
                "Je n'ai pas identifié de mots-clés ou d'intention dans votre demande. "
                "Essayez par exemple : « trouve les personnes qui cherchent un centre "
                "pour le bac français » ou activez le mode « mot-clé exact » et tapez "
                "juste le mot à chercher (ex: « nlawj »)."
            ),
            total_rows=len(df),
            matched_rows=0,
            matched_keywords=[],
            llm_used=llm_used,
        )

    result = filter_publications(df, text_column, search_terms, fuzzy_threshold=fuzzy_threshold)

    return _build_query_response(
        total_rows=result.total_rows,
        filtered_df=result.filtered_df,
        search_terms=search_terms,
        filename_base=filename_base,
        mode=mode,
        llm_used=llm_used,
        indexed=False,
    )


@app.post("/api/chatbot/query", response_model=QueryResponse)
async def query(request: QueryRequest):
    session = SESSIONS.get(request.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session introuvable. Importez d'abord un fichier.")

    df: pd.DataFrame = session["df"]
    text_column: str = session["text_column"]
    filename_base = Path(session["filename"]).stem

    return _run_query(df, text_column, request.message, filename_base, use_llm=request.use_llm, mode=request.mode)


@app.post("/api/chatbot/query-db", response_model=QueryResponse)
async def query_db(request: QueryDbRequest):
    """
    Comme /query, mais lit directement les posts/commentaires depuis PostgreSQL
    (fb_scraper) au lieu de dépendre d'un fichier uploadé — toujours à jour avec
    les derniers scrapings sans re-uploader quoi que ce soit.

    En mode "keyword", le filtrage est poussé directement en SQL (ILIKE +
    similarité trigram, voir schema-search-index.sql) au lieu de charger toute
    la table en mémoire : bien plus rapide sur une base volumineuse.
    """
    if request.source not in ("posts", "comments"):
        raise HTTPException(status_code=400, detail="source doit être 'posts' ou 'comments'.")

    table = "social_post" if request.source == "posts" else "social_commentaire"

    if request.mode == "keyword":
        search_terms = extract_literal_keyword_terms(request.message)
        if not search_terms:
            return QueryResponse(
                reply="Aucun mot-clé fourni pour la recherche.",
                total_rows=0,
                matched_rows=0,
                matched_keywords=[],
            )
        try:
            total_rows = count_rows(table)
            filtered_df = (
                search_posts_sql(search_terms)
                if request.source == "posts"
                else search_comments_sql(search_terms)
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Erreur lors de la recherche indexée sur {table} : {exc}. "
                    "Vérifie DATABASE_URL dans backend/.env."
                ),
            )

        return _build_query_response(
            total_rows=total_rows,
            filtered_df=filtered_df,
            search_terms=search_terms,
            filename_base=f"fb_scraper_{request.source}",
            mode="keyword",
            indexed=True,
        )

    try:
        df = load_posts_dataframe() if request.source == "posts" else load_comments_dataframe()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Impossible de charger la base : {exc}. Vérifie DATABASE_URL dans backend/.env.",
        )

    if df.empty:
        return QueryResponse(
            reply="La base ne contient encore aucune donnée pour cette table.",
            total_rows=0,
            matched_rows=0,
            matched_keywords=[],
        )

    text_column = detect_text_column(df)  # doit détecter la colonne 'texte'
    return _run_query(
        df, text_column, request.message, f"fb_scraper_{request.source}",
        use_llm=request.use_llm, mode=request.mode,
    )


# ---------------------------------------------------------------------------
# Bibliothèque de mots : suggestions / autocomplete basées sur les mots
# réellement présents dans les données (pas sur CONCEPT_VARIANTS).
# ---------------------------------------------------------------------------

@app.get("/api/chatbot/keywords")
async def keywords(session_id: str, prefix: str | None = None, limit: int = 30):
    """Mots les plus fréquents dans la session uploadée (fichier)."""
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session introuvable. Importez d'abord un fichier.")

    df: pd.DataFrame = session["df"]
    text_column: str = session["text_column"]
    return {"keywords": extract_top_keywords(df, text_column, top_n=limit, prefix=prefix)}


@app.get("/api/chatbot/keywords-db")
async def keywords_db(source: str = "posts", prefix: str | None = None, limit: int = 30):
    """Mots les plus fréquents directement depuis la base (posts ou commentaires)."""
    if source not in ("posts", "comments"):
        raise HTTPException(status_code=400, detail="source doit être 'posts' ou 'comments'.")

    try:
        df = load_posts_dataframe() if source == "posts" else load_comments_dataframe()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Impossible de charger la base : {exc}. Vérifie DATABASE_URL dans backend/.env.",
        )

    if df.empty:
        return {"keywords": []}

    text_column = detect_text_column(df)
    return {"keywords": extract_top_keywords(df, text_column, top_n=limit, prefix=prefix)}


@app.get("/api/chatbot/download/{file_id}")
async def download(file_id: str):
    entry = GENERATED_FILES.get(file_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Fichier introuvable ou expiré.")

    filename, content = entry
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Frontend : si le dossier ../frontend existe, l'interface est servie
# directement par l'API sur http://localhost:8000/ (rien d'autre à lancer).
# Ce montage doit rester en fin de fichier : il capture toutes les routes
# non déclarées au-dessus.
# ---------------------------------------------------------------------------

from fastapi.staticfiles import StaticFiles  # noqa: E402

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
