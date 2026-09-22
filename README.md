# Lionis Sentinel — interface du chatbot + tableau de bord BI

Interface web pour le backend existant : un assistant de recherche, un tableau de bord
d'indicateurs, et un explorateur BI (axe d'analyse × mesure × filtres, avec export Excel).

```
backend/
  main.py            ← modifié : branche le routeur analytics + sert le frontend
  analytics.py       ← nouveau : /api/analytics/overview, /explore, /export, /meta
  db.py, nlp_matching.py, schema-search-index.sql, requirements.txt, .env
frontend/
  index.html         ← l'interface complète (aucune installation, aucun build)
```

## Démarrage

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

Puis ouvrez **http://localhost:8000** — le frontend est servi par FastAPI lui-même
(montage `StaticFiles` en fin de `main.py`), donc pas de second serveur ni de souci de CORS.

Pour héberger le frontend ailleurs (Nginx, Vercel, `python -m http.server`), c'est
possible aussi : le champ « adresse de l'API » en bas de la barre latérale garde en
mémoire l'URL du backend.

Sans PostgreSQL configuré, tout reste utilisable : importez un CSV ou un XLSX depuis la
barre latérale et les trois espaces travaillent sur ce fichier.

## Les trois espaces

**Assistant** — le chat existant. Sélecteur de source (publications, commentaires,
fichier importé), compréhension automatique ou mot-clé exact, renfort LLM local
optionnel, lien de téléchargement du fichier filtré. Le panneau latéral liste les mots
réellement fréquents dans les données ; un clic lance la recherche.

**Tableau de bord** — un seul appel à `/api/analytics/overview` alimente : volume et
engagement dans le temps, heures et jours de publication, auteurs les plus actifs,
intentions détectées (basées sur `CONCEPT_VARIANTS` de `nlp_matching.py`), vocabulaire du
corpus, publications les plus engageantes et les plus récentes.

**Explorateur** — l'outil BI. Vous croisez un axe (jour, semaine, mois, heure, jour de la
semaine, auteur, intention, palier d'engagement, longueur du texte) avec une mesure
(nombre, réactions, commentaires, partages, engagement total ou moyen, auteurs distincts),
en filtrant par période, mot-clé, auteur et engagement minimum. Résultat en barres, courbe
ou anneau, plus un tableau chiffré. « Exporter en Excel » produit un classeur à deux
feuilles : la synthèse agrégée et les lignes sources correspondantes.

## API ajoutée

| Route | Rôle |
|---|---|
| `GET /api/analytics/meta` | Axes et mesures disponibles (les menus du frontend) |
| `GET /api/analytics/overview?source=posts` | Tous les indicateurs du tableau de bord |
| `POST /api/analytics/explore` | Agrégation libre |
| `POST /api/analytics/export` | La même agrégation en `.xlsx` |

`source` vaut `posts`, `comments` ou `session:<session_id>` (fichier importé).
Le corps d'`/explore` :

```json
{
  "source": "posts",
  "dimension": "week",
  "measure": "engagement_avg",
  "limit": 20,
  "filters": { "keyword": "centre", "date_from": "2026-01-01", "min_engagement": 5 }
}
```

## Notes techniques

- Les DataFrames lus depuis PostgreSQL sont mis en cache 60 secondes ; le bouton
  « Actualiser » du tableau de bord force la relecture (`?refresh=true`).
- La date de référence est `publish_time`, avec repli sur `scraped_at` si elle est vide
  ou illisible.
- Le filtre mot-clé de l'explorateur réutilise `filter_publications` : même normalisation
  FR / arabe / arabizi et même tolérance aux fautes que le chatbot.
- Sur une base volumineuse, exécutez `schema-search-index.sql` une fois, et envisagez de
  remplacer `load_posts_dataframe()` par des agrégations SQL (`GROUP BY`) dans
  `analytics.py` : la structure des endpoints n'aurait pas à changer.
- `Chart.js` et les polices sont chargés depuis un CDN ; prévoyez de les copier en local
  pour un usage hors ligne.