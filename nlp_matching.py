"""
nlp_matching.py
----------------
Coeur "intelligent" du chatbot : normalisation multilingue (FR / AR / tunisien /
arabizi), détection d'intention, extraction de mots-clés à partir d'une phrase
libre, et filtrage flou (tolérant aux fautes) d'un DataFrame de publications.

Ce module est volontairement sans dépendance à un LLM externe : il fonctionne
"out of the box" avec des règles + fuzzy matching (rapidfuzz). Un point
d'extension (`call_llm_for_intent`) est prévu en bas de fichier pour brancher
plus tard une API LLM (Anthropic, OpenAI...) sans changer le reste du code.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd
import requests
from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# 1. Normalisation de texte (FR sans accents, AR sans diacritiques, arabizi)
# ---------------------------------------------------------------------------

# Table de translittération arabizi -> lettre arabe la plus probable.
# Utilisée pour rapprocher "nlawj" / "nlawej" de "نلوج" par ex., et pour
# normaliser les chiffres utilisés comme lettres dans le tunisien/arabizi.
_ARABIZI_DIGIT_MAP = {
    "2": "ء",
    "3": "ع",
    "5": "خ",
    "6": "ط",
    "7": "ح",
    "8": "غ",
    "9": "ق",
}

_ARABIC_DIACRITICS_RE = re.compile(
    r"[\u0610-\u061A\u064B-\u065F\u06D6-\u06DC\u06DF-\u06E8\u06EA-\u06ED\u0670]"
)


def strip_accents(text: str) -> str:
    """Retire les accents latins (é -> e, à -> a, etc.)."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def strip_arabic_diacritics(text: str) -> str:
    return _ARABIC_DIACRITICS_RE.sub("", text)


def normalize_text(text: str) -> str:
    """
    Normalisation générique appliquée à la fois aux publications et aux
    mots-clés avant comparaison :
      - minuscule
      - accents latins retirés
      - diacritiques arabes retirés
      - chiffres arabizi convertis en lettre arabe correspondante
      - ponctuation réduite à des espaces
      - espaces multiples compressés
    """
    if not isinstance(text, str):
        return ""

    text = text.lower()
    text = strip_accents(text)
    text = strip_arabic_diacritics(text)

    for digit, letter in _ARABIZI_DIGIT_MAP.items():
        text = text.replace(digit, letter)

    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# 2. Dictionnaire de concepts connus (utilisé pour la détection d'intention
#    "naturelle", en plus des mots-clés que l'utilisateur tape lui-même)
# ---------------------------------------------------------------------------

CONCEPT_VARIANTS: dict[str, list[str]] = {
    "recherche": [
        "je cherche", "je recherche", "je suis a la recherche", "cherche",
        "recherche", "on cherche", "nlawj", "nlawej", "nlowj", "نلوج", "نبحث",
        "نلوّج", "نحب نلوج",
    ],
    "recommandation": [
        "je recommande", "je conseille", "recommande", "recommandation",
        "conseil", "نوصي", "ننصح",
    ],
    "bac_francais": [
        "bac francais", "bac français", "baccalaureat francais",
        "baccalauréat français", "bac fr",
    ],
    "centre": [
        "centre", "centre de formation", "centre scolaire", "centre de langues",
        "مركز",
    ],
}


@dataclass
class MatchResult:
    """Résultat du filtrage d'un DataFrame de publications."""

    total_rows: int
    matched_rows: int
    matched_keywords: list[str]
    intent_labels: list[str]
    filtered_df: pd.DataFrame = field(repr=False)


# ---------------------------------------------------------------------------
# 3. Extraction de mots-clés à partir d'un message utilisateur libre
# ---------------------------------------------------------------------------

_SPLIT_RE = re.compile(r",|;| et |\bou\b", flags=re.IGNORECASE)

# Verbes/tournures d'intro qu'on retire avant de découper la liste de mots-clés
_INSTRUCTION_PREFIXES = [
    r"renvoie[- ]?moi (le|les) (fichier|posts?|publications?) (qui|ou)?.*?contiennent?( les)?( mots?[- ]?cl[ée]?s?)?\s*:?",
    r"trouve[- ]?moi?.*?(mots?[- ]?cl[ée]?s?)?\s*:?",
    r"filtre[r]?.*?(mots?[- ]?cl[ée]?s?)?\s*:?",
    r"garde[r]?.*?(mots?[- ]?cl[ée]?s?)?\s*:?",
]

# Tournures qui indiquent, sans ambiguïté, que ce qui suit est un mot-clé
# LITTÉRAL à chercher tel quel dans les publications — qu'il figure ou non
# dans CONCEPT_VARIANTS. Contrairement à _INSTRUCTION_PREFIXES (qui matchent
# des phrases entières), celles-ci isolent précisément le(s) mot(s) qui suit.
# Exemple : "je veux les posts qui contient mot cle nlawj" -> ne garder que
# "nlawj", pas toute la phrase.
_KEYWORD_TRIGGER_PATTERNS = [
    r"\bmots?[- ]?cl[ée]?s?\b\s*(?:est|sont|[:=])?\s*",
    r"\bcontenant\s+(?:le\s+mot\s+|les\s+mots\s+|la\s+phrase\s+)?",
    r"\bcontiennent\s+(?:le\s+mot\s+|les\s+mots\s+|la\s+phrase\s+)?",
    r"\bcontient\s+(?:le\s+mot\s+|les\s+mots\s+|la\s+phrase\s+)?",
    r"\bavec\s+le\s+mot\s+",
    r"\bavec\s+les\s+mots\s+",
]


def extract_explicit_keywords(message: str) -> tuple[list[str], bool]:
    """
    Tente d'extraire une liste explicite de mots-clés d'une phrase comme :
    "Renvoie-moi le fichier qui contient juste les posts qui contiennent les
    mots clés bac français, je recommande, je cherche, nlawj, nlawej, centre."

    Ou, tout aussi bien, une phrase au singulier comme "je veux les posts qui
    contient mot cle nlawj" -> ["nlawj"].

    Renvoie (mots_cles, confiant). `confiant=True` signifie qu'une tournure
    déclenchante explicite ("mot(s) clé(s)", "contient le mot", "avec le
    mot"...) a été détectée : dans ce cas le(s) mot(s) extrait(s) doivent être
    utilisés TELS QUELS pour la recherche, littéralement, même s'ils ne sont
    dans aucun dictionnaire de concepts connus (CONCEPT_VARIANTS) — c'est le
    post lui-même, en base, qui fait foi, pas une liste programmée.
    """
    cleaned = message.strip()
    lowered = cleaned.lower()

    confident = False
    for pattern in _KEYWORD_TRIGGER_PATTERNS:
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if match:
            cleaned = cleaned[match.end():]
            confident = True
            break

    if not confident:
        for pattern in _INSTRUCTION_PREFIXES:
            match = re.search(pattern, lowered, flags=re.IGNORECASE)
            if match:
                cleaned = cleaned[match.end():]
                break

    parts = [p.strip(" .!?\n\t\"'") for p in _SPLIT_RE.split(cleaned)]
    keywords = [p for p in parts if p and len(p) > 1]
    return keywords, confident


def detect_intent_labels(message: str) -> list[str]:
    """
    Détecte les intentions/thèmes "connus" mentionnés dans le message, en
    comparant chaque concept + ses variantes au message normalisé (avec un
    fuzzy-match pour tolérer les fautes).
    """
    normalized_message = normalize_text(message)
    detected: list[str] = []

    for concept, variants in CONCEPT_VARIANTS.items():
        for variant in variants:
            normalized_variant = normalize_text(variant)
            if not normalized_variant:
                continue
            if normalized_variant in normalized_message:
                detected.append(concept)
                break
            # tolérance aux fautes d'orthographe / variantes proches
            score = fuzz.partial_ratio(normalized_variant, normalized_message)
            if score >= 85:
                detected.append(concept)
                break

    return detected


_LIST_HINT_RE = re.compile(r",|\bmots?[- ]?cl[ée]?s?\b|\bkeywords?\b", flags=re.IGNORECASE)


def _looks_like_explicit_list(message: str, explicit: list[str]) -> bool:
    """
    Distingue une vraie liste de mots-clés énumérée par l'utilisateur
    ("bac français, je recommande, centre...") d'une simple phrase en langage
    naturel ("trouve les personnes qui cherchent un centre...") où l'extraction
    naïve aurait renvoyé la phrase entière comme un seul "mot-clé".
    """
    if len(explicit) > 1:
        return True
    if _LIST_HINT_RE.search(message):
        return True
    # une phrase complète (plusieurs mots, verbe conjugué) n'est pas une liste
    if explicit and len(explicit[0].split()) > 4:
        return False
    return bool(explicit)


def build_search_terms(message: str) -> list[str]:
    """
    Combine les mots-clés explicites tapés par l'utilisateur et les concepts
    "connus" détectés dans la phrase, pour obtenir la liste finale de termes
    à rechercher dans les publications.
    """
    explicit, confident = extract_explicit_keywords(message)
    intents = detect_intent_labels(message)
    explicit_mode = confident or _looks_like_explicit_list(message, explicit)

    terms: list[str] = []
    if explicit_mode:
        for kw in explicit:
            if kw and kw not in terms:
                terms.append(kw)

    # Si aucun mot-clé explicite exploitable, on retombe sur les variantes
    # associées aux concepts détectés (mode "intention" pur, langage naturel).
    if not terms:
        for concept in intents:
            terms.extend(CONCEPT_VARIANTS.get(concept, []))

    # dédoublonnage en conservant l'ordre
    seen = set()
    unique_terms = []
    for t in terms:
        norm = normalize_text(t)
        if norm and norm not in seen:
            seen.add(norm)
            unique_terms.append(t)

    return unique_terms


def extract_literal_keyword_terms(message: str) -> list[str]:
    """
    Mode "recherche par mot-clé exact" : ignore complètement CONCEPT_VARIANTS
    et l'analyse d'intention. Tout ce que l'utilisateur tape est traité comme
    un (ou plusieurs, séparés par virgule / "et" / "ou") mot-clé littéral à
    chercher tel quel dans les publications — que ce mot soit "connu" du
    système ou non. C'est le contenu réel de la base qui décide, pas une
    liste programmée à l'avance.

    Exemple : "nlawj" -> ["nlawj"] ; "nlawj, centre" -> ["nlawj", "centre"].
    """
    parts = [p.strip(" .!?\n\t\"'") for p in _SPLIT_RE.split(message.strip())]
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# 4. Détection automatique de la colonne "texte" d'un fichier
# ---------------------------------------------------------------------------

_LIKELY_TEXT_COLUMN_NAMES = [
    "message", "text", "texte", "post", "publication", "contenu", "content",
    "description", "caption", "body",
]


def detect_text_column(df: pd.DataFrame) -> str:
    """
    Heuristique de détection de la colonne de texte des publications :
    1. correspondance de nom de colonne connue,
    2. sinon, la colonne texte (dtype object) avec la plus grande longueur
       moyenne de chaîne de caractères.
    """
    lower_cols = {c.lower(): c for c in df.columns}
    for candidate in _LIKELY_TEXT_COLUMN_NAMES:
        if candidate in lower_cols:
            return lower_cols[candidate]

    text_cols = [c for c in df.columns if df[c].dtype == object]
    if not text_cols:
        raise ValueError("Aucune colonne texte détectée dans le fichier.")

    avg_lengths = {
        c: df[c].dropna().astype(str).str.len().mean() if not df[c].dropna().empty else 0
        for c in text_cols
    }
    return max(avg_lengths, key=avg_lengths.get)


# ---------------------------------------------------------------------------
# 4bis. Bibliothèque de mots : fréquence des mots présents dans les
#       publications, pour proposer des suggestions / de l'autocomplete côté
#       chatbot (indépendant de CONCEPT_VARIANTS — basé uniquement sur ce qui
#       est réellement dans les données).
# ---------------------------------------------------------------------------

from collections import Counter

_STOPWORDS_FR = {
    "le", "la", "les", "de", "des", "du", "un", "une", "et", "que", "qui",
    "pour", "avec", "dans", "sur", "ne", "pas", "est", "ce", "cette", "ces",
    "cet", "mon", "ma", "mes", "ton", "ta", "tes", "son", "sa", "ses",
    "notre", "nos", "votre", "vos", "leur", "leurs", "je", "tu", "il", "elle",
    "on", "nous", "vous", "ils", "elles", "au", "aux", "en", "se", "sont",
    "suis", "es", "sommes", "etes", "a", "ou", "donc", "car", "si", "plus",
    "moins", "tres", "comme", "fait", "faire", "etre", "avoir", "d", "l",
    "j", "n", "y", "toute", "tout", "tous", "toutes", "y", "meme", "bien",
}

_STOPWORDS_AR = {
    "في", "من", "على", "الى", "إلى", "و", "ان", "إن", "هذا", "هذه", "ذلك",
    "التي", "الذي", "مع", "عن", "كل", "لا", "ما", "هو", "هي", "نحن", "هم",
    "انا", "أنا", "لي", "له", "لها",
}

_STOPWORDS = _STOPWORDS_FR | _STOPWORDS_AR


def extract_top_keywords(
    df: pd.DataFrame,
    text_column: str,
    top_n: int = 30,
    min_length: int = 3,
    prefix: str | None = None,
) -> list[dict]:
    """
    Construit une "bibliothèque de mots" à partir des publications réellement
    présentes dans `df` : compte la fréquence de chaque mot (après
    normalisation, filtrage des mots vides FR/AR et des mots trop courts).

    Sert à alimenter des suggestions / de l'autocomplete dans le chatbot —
    ex: pendant que l'utilisateur tape "nla", proposer "nlawj" s'il est
    présent dans les données, sans que ce mot ait jamais été "programmé".

    Args:
        top_n: nombre max de mots renvoyés.
        min_length: longueur minimale d'un mot pour être compté.
        prefix: si fourni, ne renvoie que les mots commençant par ce préfixe
                (utilisé pour l'autocomplete).

    Returns:
        Liste de {"word": str, "count": int}, triée par fréquence décroissante.
    """
    counter: Counter[str] = Counter()

    for raw in df[text_column].fillna("").astype(str):
        normalized = normalize_text(raw)
        for token in normalized.split():
            if len(token) < min_length:
                continue
            if token in _STOPWORDS:
                continue
            if token.isdigit():
                continue
            counter[token] += 1

    items = counter.most_common()

    if prefix:
        normalized_prefix = normalize_text(prefix)
        items = [(word, count) for word, count in items if word.startswith(normalized_prefix)]

    items = items[:top_n]
    return [{"word": word, "count": count} for word, count in items]


# ---------------------------------------------------------------------------
# 5. Filtrage flou du DataFrame
# ---------------------------------------------------------------------------

def _row_matches(normalized_text: str, normalized_terms: list[str], threshold: int) -> bool:
    if not normalized_text:
        return False
    for term in normalized_terms:
        if not term:
            continue
        if term in normalized_text:
            return True
        # match flou "mot par mot" pour tolérer fautes d'orthographe /
        # variantes proches (ex: "nlawj" vs "نلوج" une fois translittérés,
        # ou "recherche" mal orthographié).
        if fuzz.partial_ratio(term, normalized_text) >= threshold:
            return True
    return False


def filter_publications(
    df: pd.DataFrame,
    text_column: str,
    search_terms: Iterable[str],
    fuzzy_threshold: int = 88,
) -> MatchResult:
    normalized_terms = [normalize_text(t) for t in search_terms]
    normalized_terms = [t for t in normalized_terms if t]

    normalized_series = df[text_column].fillna("").astype(str).map(normalize_text)
    mask = normalized_series.apply(
        lambda t: _row_matches(t, normalized_terms, fuzzy_threshold)
    )

    filtered = df[mask].copy()

    return MatchResult(
        total_rows=len(df),
        matched_rows=len(filtered),
        matched_keywords=list(search_terms),
        intent_labels=detect_intent_labels(" ".join(search_terms)),
        filtered_df=filtered,
    )


# ---------------------------------------------------------------------------
# 6. Point d'extension : brancher une API LLM plus tard
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 6. Point d'extension : LLM local via Ollama (gratuit, aucune API payante)
# ---------------------------------------------------------------------------

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

_LLM_PROMPT_TEMPLATE = """Tu aides à filtrer des publications d'un groupe Facebook \
tunisien sur le bac français et les cours particuliers (les messages sont en \
français, arabe, tunisien ou arabizi).

Demande de l'utilisateur : "{message}"

Réponds UNIQUEMENT avec un objet JSON valide (rien d'autre, pas de markdown), au \
format exact :
{{"search_terms": ["mot1", "mot2", ...], "intent_labels": ["label1", ...]}}

- search_terms : mots-clés ou expressions courtes à chercher dans le texte des posts
- intent_labels : ex. "recherche_prof", "offre_cours", "demande_document", "urgent"
"""


def call_llm_for_intent(message: str, timeout: float = 15.0) -> dict:
    """
    Interroge un modèle LLM tournant en LOCAL via Ollama (http://localhost:11434) —
    aucune clé API, aucun coût. Nécessite qu'Ollama soit installé et lancé
    (https://ollama.com) avec un modèle téléchargé (ex: `ollama pull llama3.2`).

    Renvoie {"search_terms": [...], "intent_labels": [...]}. Lève une exception si
    Ollama n'est pas joignable ou si la réponse n'est pas un JSON exploitable — à
    l'appelant de décider s'il retombe sur le pipeline rules + fuzzy matching seul.
    """
    prompt = _LLM_PROMPT_TEMPLATE.format(message=message)

    response = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        },
        timeout=timeout,
    )
    response.raise_for_status()

    raw_text = response.json().get("response", "").strip()
    parsed = json.loads(raw_text)

    search_terms = [str(t).strip() for t in parsed.get("search_terms", []) if str(t).strip()]
    intent_labels = [str(t).strip() for t in parsed.get("intent_labels", []) if str(t).strip()]

    if not search_terms:
        raise ValueError("Le LLM n'a renvoyé aucun terme de recherche exploitable.")

    return {"search_terms": search_terms, "intent_labels": intent_labels}
