# FB Post Scraper — Installation

## 1. Installer en mode développeur
1. Ouvre `chrome://extensions` dans Chrome.
2. Active **Mode développeur** (en haut à droite).
3. Clique **Charger l'extension non empaquetée**.
4. Sélectionne le dossier `fb-scraper-extension`.

## 2. Utilisation
1. Ouvre un groupe ou une Page Facebook (`facebook.com/groups/...` ou `facebook.com/nom-de-page`).
2. Un **panneau flottant** apparaît automatiquement en bas à droite de la page
   (déplaçable par le titre, réductible avec le bouton "—").
   Si tu ne le vois pas ou l'as fermé, clique sur l'icône de l'extension dans la barre
   Chrome pour l'afficher/masquer.
3. Règle **Nombre max de posts** et **Délai de scroll** (1500ms est un bon défaut — trop
   rapide = Facebook ne charge pas le contenu à temps).
4. Clique **Démarrer le scraping**. La page va scroller automatiquement, essayer de
   déplier les textes tronqués ("Voir plus") et les commentaires supplémentaires, puis
   extraire posts + commentaires. Le compteur se met à jour en direct.
5. Trois boutons d'export :
   - **JSON (posts+comments)** — un seul fichier, chaque post contient ses commentaires
     imbriqués dans `commentsData`.
   - **CSV Posts** — colonnes : `Post Id, Feedback Id, Actors, Text, Html, Attachments,
     Subattachments, Comments, Likes, Shares, Page Url, Post Url, Publish Time, Scraped At`
   - **CSV Comments** — colonnes : `Comment Id, Post Id, Actors, Text, Html, Attachments,
     Likes, Comment Url, Scraped At`

## Nouveautés fiabilité (v5)
- **Arrêt intelligent en fin de flux** : le scraping ne se fie plus seulement au délai
  fixe. Si le nombre de posts ET la hauteur de la page n'ont pas bougé pendant
  6 tentatives de scroll consécutives, l'extension considère que Facebook n'a plus
  rien à charger et s'arrête toute seule (message "🏁 Fin du flux détectée").
- **Sauvegarde continue + reprise** : la progression (posts, commentaires, posts déjà
  dépliés) est sauvegardée automatiquement dans `chrome.storage.local`, par page/groupe
  (clé basée sur l'URL sans les paramètres). Une sauvegarde immédiate est aussi
  déclenchée si tu changes d'onglet ou fermes la page, en plus de la sauvegarde
  différée (max toutes les 2s) pendant le scraping.
  - Si le scraping s'interrompt (crash, reload, extension rechargée), un bandeau
    "💾 X post(s) déjà sauvegardé(s)..." apparaît au prochain chargement du panneau,
    avec un bouton **Reprendre** qui recharge les données déjà collectées et continue
    le scraping/scroll à partir de là, sans tout refaire.
  - Un lien **"effacer données sauvegardées"** permet de repartir de zéro sur une page.
  - Les erreurs ponctuelles pendant l'extraction (DOM Facebook qui change en cours de
    route) sont désormais journalisées dans la console et n'interrompent plus tout le
    scraping — seul le tick en cours est affecté, le suivant repart normalement.
- **Déduplication entre sessions** : comme les posts/commentaires déjà scrapés sont
  gardés en mémoire (via `chrome.storage.local`) et identifiés par leur `postId`/
  `commentId`, relancer un scraping sur le même groupe avec **Reprendre** n'ajoute que
  le nouveau contenu — idéal pour un passage périodique sur un groupe actif.

## Suivi automatique quotidien (v6) — ne rate aucun post ni aucun commentaire
Le panneau a maintenant une section **"⏱️ Suivi automatique quotidien"** :

1. Ouvre le groupe/la page à suivre, règle l'intervalle (par défaut 30 min, minimum
   10 min), puis clique **Activer le suivi auto sur cette page**.
2. À partir de là, l'extension tourne toute seule en arrière-plan, **même si tu ne
   gardes pas cet onglet ouvert** — tant que Chrome est lancé (peu importe la fenêtre
   ou l'onglet actif) :
   - Toutes les X minutes, elle ouvre discrètement un onglet en arrière-plan sur le
     groupe, trie par **"Plus récents"** (au lieu de "Plus pertinents") pour que les
     nouveaux posts soient en haut, scrolle jusqu'à retomber sur du contenu déjà connu,
     puis referme l'onglet. → **aucun nouveau post raté**, peu importe l'heure de
     publication.
   - Pour chaque post identifié comme publié **aujourd'hui**, elle navigue ensuite
     directement vers son permalink pour ne récupérer que les **nouveaux
     commentaires** apparus depuis le dernier passage, avant de passer au suivant.
     → un commentaire posté à 22h sur un post du matin est capté au prochain passage,
     pas seulement lors du scrape initial.
   - Tout est fusionné dans le même stockage que le mode manuel (déduplication par
     `postId`/`commentId`) — tu peux rouvrir le panneau sur cette page à tout moment
     et exporter en JSON/CSV, les données s'accumulent automatiquement au fil de la
     journée.
3. **Effacer les données** ou **Reprendre** (section du dessus) fonctionnent aussi
   bien sur des données collectées manuellement qu'automatiquement.
4. Pour arrêter le suivi sur une page, reviens sur le panneau et clique
   **Désactiver le suivi auto sur cette page**.

### Limites du mode automatique (important)
- **Le navigateur doit rester ouvert** (peu importe l'onglet actif, même minimisé) —
  une extension ne peut rien faire tourner si Chrome est complètement fermé. Si ton
  PC/Mac s'éteint ou se met en veille profonde à un moment, ces passages-là seront
  simplement manqués (rien ne se duplique ni ne se perd pour autant : le prochain
  passage réussi rattrape tout le retard).
- Le contrôle "Plus récents" n'existe pas sur tous les types de pages Facebook — si
  Facebook ne le propose pas, l'extension continue avec le tri par défaut (peut
  rater des posts remontés artificiellement en haut du fil par l'algorithme).
- Chrome peut, rarement, arrêter le "service worker" de l'extension en cours de
  cycle (limite technique de Manifest V3 sur les tâches très longues). Le design est
  volontairement incrémental : si ça arrive en plein passage, ce passage s'arrête net
  mais rien n'est perdu, et le passage suivant reprend normalement.
- Un post peut recevoir des commentaires plusieurs jours après sa publication — ce
  mode ne revérifie que les posts du **jour même**. Pour un post plus ancien qui
  continue de recevoir des commentaires, il faudra relancer un scraping manuel dessus
  (ou me redemander une extension du système de revérification sur une fenêtre
  glissante de plusieurs jours si besoin).
- Plusieurs pages/groupes peuvent être suivis en même temps (active le suivi sur
  chacun séparément) — ils sont traités l'un après l'autre à chaque heartbeat, donc
  plus tu en ajoutes, plus un cycle complet prend de temps.

## Limites connues (important à savoir)
- **Feedback Id** reste toujours vide : c'est un identifiant GraphQL interne encodé en
  base64, qui n'apparaît jamais dans le HTML visible — seulement dans les réponses réseau
  internes de Facebook. Impossible à récupérer en scrapant le DOM depuis le navigateur.
- **Publish Time** est rempli seulement quand Facebook expose une info exacte via l'attribut
  `title` de l'horodatage (souvent partiel — jour/heure sans année). S'il n'est pas dispo,
  le champ reste vide plutôt que d'être deviné.
- **Subattachments** n'est pas rempli dans cette version (rare en pratique — pièces jointes
  imbriquées dans des partages de partages).
- Facebook change régulièrement la structure de son DOM. Les sélecteurs dans `content.js`
  (basés sur `role="article"`, `dir="auto"`, etc.) sont ceux qui bougent le moins, mais il
  faudra probablement les ajuster de temps en temps — comme avec ton scraper Selenium actuel.
- L'extraction des commentaires cible les `div[role="article"]` imbriqués sous une liste —
  si Facebook affiche les réponses en profondeur (réponses à un commentaire), elles peuvent
  être capturées comme des commentaires normaux plutôt que rattachées à leur parent.
- Ça scrape uniquement ce que ton propre compte connecté peut déjà voir — donc respecte les
  CGU de Facebook et la vie privée des gens (règles de protection des données applicables
  selon ta juridiction).

## Pour connecter ça à ton projet fb-scraper / n8n
Deux options qu'on a évoquées :
- Ajouter un `fetch()` dans `content.js` qui POST les données vers un Webhook n8n
  automatiquement au lieu de (ou en plus de) l'export manuel.
- Garder l'export manuel JSON/CSV et écrire un petit script Python qui importe ces fichiers
  dans MySQL (`social_post` / `social_commentaire`), réutilisable avec ton schéma actuel.
