-- schema-search-index.sql
-- --------------------------------------------------------------------------
-- Accélère la recherche de mots-clés (ILIKE '%mot%' et recherche floue) sur
-- social_post / social_commentaire quand la base devient volumineuse.
--
-- Sans index : PostgreSQL doit lire TOUTE la table à chaque recherche
-- (scan séquentiel) -> lent au-delà de quelques dizaines de milliers de
-- lignes.
-- Avec l'extension pg_trgm + un index GIN : la recherche devient bien plus
-- rapide, y compris pour des correspondances partielles / avec fautes de
-- frappe (via la fonction similarity()).
--
-- À exécuter une seule fois sur la base fb_scraper :
--   psql -h <host> -U <user> -d fb_scraper -f schema-search-index.sql

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_social_post_texte_trgm
    ON social_post USING GIN (texte gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_social_commentaire_texte_trgm
    ON social_commentaire USING GIN (texte gin_trgm_ops);
