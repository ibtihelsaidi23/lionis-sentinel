// content.js — injecte un panneau flottant sur la page Facebook, scrape les posts
// ET les commentaires de chaque post.
//
// TECHNIQUE DE DÉTECTION (v4) :
// - Un POST est identifié via l'attribut data-ad-rendering-role="story_message" —
//   présent UNIQUEMENT sur le texte d'un vrai post, jamais sur un commentaire.
//   L'auteur est trouvé via data-ad-rendering-role="profile_name" à proximité.
// - Un COMMENTAIRE est identifié via aria-label contenant "Comment by " / "Reply by "
//   (ou équivalents localisés) — un attribut d'accessibilité posé spécifiquement par
//   Facebook sur chaque commentaire, indépendant du markup visuel.
// - Comme un commentaire n'est pas toujours un DESCENDANT DOM du post (Facebook peut
//   les rendre comme des noeuds frères plus bas dans l'arbre), on rattache chaque
//   commentaire au post qui le PRÉCÈDE le plus proche dans l'ordre du document —
//   plus robuste que de dépendre de la structure d'imbrication.
//
// LIMITES CONNUES (voir README.md) :
// - "Feedback Id" (id GraphQL encodé en base64) n'est PAS présent dans le DOM visible.
// - "Publish Time" exact en ISO n'est disponible que si Facebook l'expose via title/aria-label.
// - Le DOM de Facebook change régulièrement : ces sélecteurs visent des attributs stables
//   mais peuvent nécessiter des ajustements dans le temps.

(function () {
  let state = {
    running: false,
    maxPosts: 25,
    delay: 1500,
    posts: new Map(),
    comments: new Map(),
    expandedPostKeys: new Set(),
    commentIdByMarkerEl: new Map(), // pour relier une réponse à son commentaire parent
    scrollTimer: null,
    // Détection de fin de flux (plus de nouveau contenu après scroll)
    stableTicks: 0,
    lastScrollHeight: 0,
    lastPostCount: 0,
    maxStableTicks: 6, // nb de ticks sans nouveau contenu avant d'arrêter automatiquement
    // Persistance / reprise
    storageKey: null,
    saveTimer: null,
  };

  let panel, shadowRoot, els = {};

  // ----- Persistance (chrome.storage.local) -----

  // URL nettoyée (sans paramètres, sans slash final) : sert de base à la fois pour
  // la clé de stockage des données scrapées et pour identifier une "source" dans
  // la liste des pages suivies automatiquement. Doit être répliquée à l'identique
  // dans background.js (qui, lui, n'a pas accès à `location`).
  function canonicalUrl(url) {
    return url.split("?")[0].replace(/\/$/, "");
  }

  function storageKeyForCurrentPage() {
    return "fbScraperData::" + canonicalUrl(location.href);
  }

  const SOURCES_KEY = "fbScraperSources";

  function loadSources() {
    return new Promise((resolve) => {
      try { chrome.storage.local.get([SOURCES_KEY], (r) => resolve(r[SOURCES_KEY] || [])); }
      catch (e) { resolve([]); }
    });
  }

  function saveSources(list) {
    return new Promise((resolve) => {
      try { chrome.storage.local.set({ [SOURCES_KEY]: list }, () => resolve(true)); }
      catch (e) { resolve(false); }
    });
  }

  function serializeState() {
    return {
      posts: Array.from(state.posts.entries()),
      comments: Array.from(state.comments.entries()),
      expandedPostKeys: Array.from(state.expandedPostKeys),
      savedAt: new Date().toISOString(),
    };
  }

  function persistState() {
    if (!state.storageKey) return;
    try {
      chrome.storage.local.set({ [state.storageKey]: serializeState() });
    } catch (e) {
      // On ignore silencieusement (ex: contexte d'extension invalidé après reload)
    }
  }

  // Sauvegarde différée pour ne pas écrire à chaque post : au plus une fois
  // toutes les 2s pendant un scraping actif.
  function schedulePersist() {
    if (state.saveTimer) return;
    state.saveTimer = setTimeout(() => {
      state.saveTimer = null;
      persistState();
    }, 2000);
  }

  function loadPersistedState(key) {
    return new Promise((resolve) => {
      try {
        chrome.storage.local.get([key], (result) => resolve(result[key] || null));
      } catch (e) {
        resolve(null);
      }
    });
  }

  function clearPersistedState() {
    if (!state.storageKey) return;
    try { chrome.storage.local.remove([state.storageKey]); } catch (e) {}
  }

  function applySavedDataToState(saved) {
    state.posts = new Map(saved.posts || []);
    state.comments = new Map(saved.comments || []);
    state.expandedPostKeys = new Set(saved.expandedPostKeys || []);
  }

  function buildPanel() {
    if (panel) return;

    const host = document.createElement("div");
    host.id = "fb-scraper-ext-host";
    host.style.cssText = "position:fixed; bottom:20px; right:20px; z-index:2147483647;";
    document.body.appendChild(host);

    shadowRoot = host.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = `
      * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
      .panel { width: 290px; background:#18191a; color:#e4e6eb; border-radius:10px;
        box-shadow:0 4px 24px rgba(0,0,0,0.4); overflow:hidden; border:1px solid #3a3b3c; }
      .header { background:#1877f2; padding:10px 12px; display:flex; align-items:center;
        justify-content:space-between; cursor:move; user-select:none; }
      .header span { font-size:13px; font-weight:700; }
      .header button { background:none; border:none; color:white; font-size:14px; cursor:pointer; opacity:.85; }
      .body { padding:12px; }
      .body.collapsed { display:none; }
      label { display:block; font-size:11px; color:#b0b3b8; margin:8px 0 3px; }
      input[type="number"] { width:100%; padding:5px 7px; border-radius:6px; border:1px solid #3a3b3c;
        background:#242526; color:#e4e6eb; font-size:12px; }
      button.action { width:100%; padding:8px; margin-top:10px; border:none; border-radius:6px;
        font-size:12px; font-weight:600; cursor:pointer; }
      #startBtn { background:#1877f2; color:white; }
      #startBtn:disabled { background:#3a3b3c; color:#7a7c80; cursor:not-allowed; }
      #stopBtn { background:#3a3b3c; color:#e4e6eb; }
      .grid { display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-top:8px; }
      .grid button { background:#2d2e2f; color:#e4e6eb; padding:7px 4px; border:none; border-radius:6px;
        font-size:11px; cursor:pointer; }
      .status { margin-top:10px; font-size:11px; color:#b0b3b8; line-height:1.5; }
      .count { font-weight:700; color:#45bd62; }
    `;
    shadowRoot.appendChild(style);

    const wrapper = document.createElement("div");
    wrapper.className = "panel";
    wrapper.innerHTML = `
      <div class="header" id="dragHandle">
        <span>📥 FB Post Scraper</span>
        <button id="collapseBtn" title="Réduire">—</button>
      </div>
      <div class="body" id="body">
        <label>Nombre max de posts</label>
        <input type="number" id="maxPosts" value="25" min="1" max="500">
        <label>Délai de scroll (ms)</label>
        <input type="number" id="delay" value="1500" min="500" step="100">
        <button class="action" id="startBtn">Démarrer le scraping</button>
        <button class="action" id="resumeBtn" style="display:none;">Reprendre</button>
        <button class="action" id="stopBtn">Arrêter</button>
        <div class="grid">
          <button id="exportJson">JSON (posts+comments)</button>
          <button id="exportPostsCsv">CSV Posts</button>
          <button id="exportCommentsCsv">CSV Comments</button>
        </div>
        <div class="status" id="savedInfo" style="display:none;"></div>
        <div class="status">
          Posts : <span class="count" id="countPosts">0</span> —
          Comments : <span class="count" id="countComments">0</span>
          <span id="clearSavedWrap" style="display:none;"> — <a href="#" id="clearSavedBtn" style="color:#e4626a;">effacer données sauvegardées</a></span>
        </div>
        <hr style="border-color:#3a3b3c; margin:12px 0 8px;">
        <label>⏱️ Suivi automatique quotidien</label>
        <div class="status" id="autoStatus">Cette page n'est pas suivie automatiquement.</div>
        <label>Revérifier toutes les (minutes)</label>
        <input type="number" id="autoInterval" value="30" min="10" step="5">
        <button class="action" id="autoToggleBtn">Activer le suivi auto sur cette page</button>
      </div>
    `;
    shadowRoot.appendChild(wrapper);
    panel = host;

    els = {
      body: shadowRoot.getElementById("body"),
      collapseBtn: shadowRoot.getElementById("collapseBtn"),
      dragHandle: shadowRoot.getElementById("dragHandle"),
      maxPosts: shadowRoot.getElementById("maxPosts"),
      delay: shadowRoot.getElementById("delay"),
      startBtn: shadowRoot.getElementById("startBtn"),
      resumeBtn: shadowRoot.getElementById("resumeBtn"),
      stopBtn: shadowRoot.getElementById("stopBtn"),
      exportJson: shadowRoot.getElementById("exportJson"),
      exportPostsCsv: shadowRoot.getElementById("exportPostsCsv"),
      exportCommentsCsv: shadowRoot.getElementById("exportCommentsCsv"),
      countPosts: shadowRoot.getElementById("countPosts"),
      countComments: shadowRoot.getElementById("countComments"),
      savedInfo: shadowRoot.getElementById("savedInfo"),
      clearSavedWrap: shadowRoot.getElementById("clearSavedWrap"),
      clearSavedBtn: shadowRoot.getElementById("clearSavedBtn"),
      autoStatus: shadowRoot.getElementById("autoStatus"),
      autoInterval: shadowRoot.getElementById("autoInterval"),
      autoToggleBtn: shadowRoot.getElementById("autoToggleBtn"),
    };

    els.collapseBtn.addEventListener("click", () => {
      els.body.classList.toggle("collapsed");
      els.collapseBtn.textContent = els.body.classList.contains("collapsed") ? "+" : "—";
    });
    els.startBtn.addEventListener("click", () => {
      startScraping(parseInt(els.maxPosts.value, 10) || 25, parseInt(els.delay.value, 10) || 1500, { resume: false });
    });
    els.resumeBtn.addEventListener("click", () => {
      startScraping(parseInt(els.maxPosts.value, 10) || 25, parseInt(els.delay.value, 10) || 1500, { resume: true });
    });
    els.stopBtn.addEventListener("click", stopScraping);
    els.exportJson.addEventListener("click", () => download(exportCombinedJson(), "fb_posts_comments.json", "application/json"));
    els.exportPostsCsv.addEventListener("click", () => download(exportPostsCsv(), "fb_posts.csv", "text/csv"));
    els.exportCommentsCsv.addEventListener("click", () => download(exportCommentsCsv(), "fb_comments.csv", "text/csv"));
    els.clearSavedBtn.addEventListener("click", (e) => {
      e.preventDefault();
      clearPersistedState();
      state.posts.clear();
      state.comments.clear();
      state.expandedPostKeys.clear();
      updateStatus();
      refreshSavedInfo(null);
    });
    els.autoToggleBtn.addEventListener("click", async () => {
      const url = canonicalUrl(location.href);
      const sources = await loadSources();
      const idx = sources.findIndex((s) => s.url === url);
      if (idx >= 0) {
        sources.splice(idx, 1);
      } else {
        sources.push({
          url,
          intervalMinutes: Math.max(10, parseInt(els.autoInterval.value, 10) || 30),
          addedAt: new Date().toISOString(),
          lastRunAt: null,
        });
      }
      await saveSources(sources);
      try { chrome.runtime.sendMessage({ type: "REFRESH_AUTO_SOURCES" }); } catch (e) {}
      refreshAutoStatus();
    });

    makeDraggable(host, els.dragHandle);

    state.storageKey = storageKeyForCurrentPage();
    loadPersistedState(state.storageKey).then((saved) => refreshSavedInfo(saved));
    refreshAutoStatus();
  }

  async function refreshAutoStatus() {
    if (!els.autoStatus) return;
    const url = canonicalUrl(location.href);
    const sources = await loadSources();
    const existing = sources.find((s) => s.url === url);
    if (existing) {
      const last = existing.lastRunAt ? new Date(existing.lastRunAt).toLocaleString() : "jamais encore";
      els.autoStatus.textContent = `✅ Suivi actif — revérification toutes les ${existing.intervalMinutes} min. Dernière exécution : ${last}.`;
      els.autoToggleBtn.textContent = "Désactiver le suivi auto sur cette page";
      els.autoInterval.value = existing.intervalMinutes;
    } else {
      els.autoStatus.textContent = "Cette page n'est pas suivie automatiquement.";
      els.autoToggleBtn.textContent = "Activer le suivi auto sur cette page";
    }
  }

  function refreshSavedInfo(saved) {
    if (!els.savedInfo) return;
    if (saved && (saved.posts || []).length) {
      const n = saved.posts.length;
      const when = saved.savedAt ? new Date(saved.savedAt).toLocaleString() : "";
      els.savedInfo.style.display = "block";
      els.savedInfo.textContent = `💾 ${n} post(s) déjà sauvegardé(s) pour cette page (${when}).`;
      els.resumeBtn.style.display = "block";
      els.clearSavedWrap.style.display = "inline";
    } else {
      els.savedInfo.style.display = "none";
      els.resumeBtn.style.display = "none";
      els.clearSavedWrap.style.display = "none";
    }
  }

  function makeDraggable(hostEl, handle) {
    let offsetX = 0, offsetY = 0, dragging = false;
    handle.addEventListener("mousedown", (e) => {
      dragging = true;
      const rect = hostEl.getBoundingClientRect();
      offsetX = e.clientX - rect.left;
      offsetY = e.clientY - rect.top;
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      hostEl.style.left = e.clientX - offsetX + "px";
      hostEl.style.top = e.clientY - offsetY + "px";
      hostEl.style.right = "auto";
      hostEl.style.bottom = "auto";
    });
    document.addEventListener("mouseup", () => (dragging = false));
  }

  function togglePanel() {
    if (!panel) { buildPanel(); return; }
    panel.style.display = panel.style.display === "none" ? "block" : "none";
  }

  function updateStatus() {
    if (!els.countPosts) return;
    els.countPosts.textContent = state.posts.size;
    els.countComments.textContent = state.comments.size;
  }

  function download(content, filename, mime) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === "TOGGLE_PANEL") {
      togglePanel();
      sendResponse({ ok: true });
      return true;
    }
    if (msg.type === "AUTO_SCRAPE_NEW_POSTS") {
      autoScrapeNewPosts(msg.sourceKey, msg.opts || {})
        .then((result) => sendResponse({ ok: true, ...result }))
        .catch((e) => sendResponse({ ok: false, error: String(e) }));
      return true; // réponse asynchrone
    }
    if (msg.type === "AUTO_RECHECK_POST_COMMENTS") {
      autoRecheckPostComments(msg.sourceKey, msg.postId, msg.opts || {})
        .then((result) => sendResponse({ ok: true, ...result }))
        .catch((e) => sendResponse({ ok: false, error: String(e) }));
      return true; // réponse asynchrone
    }
    return true;
  });

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  function todayLocalDateStr() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function setStatusMessage(msg) {
    if (!els.savedInfo) return;
    els.savedInfo.style.display = "block";
    els.savedInfo.textContent = msg;
  }

  async function startScraping(maxPosts, delay, opts = {}) {
    if (state.running) return;
    const resume = !!opts.resume;

    state.storageKey = state.storageKey || storageKeyForCurrentPage();

    if (resume) {
      const saved = await loadPersistedState(state.storageKey);
      if (saved) applySavedDataToState(saved);
    } else {
      state.posts.clear();
      state.comments.clear();
      state.expandedPostKeys.clear();
    }
    state.commentIdByMarkerEl.clear(); // les éléments DOM d'une session précédente ne sont plus valides

    state.running = true;
    state.maxPosts = maxPosts;
    state.delay = delay;
    state.stableTicks = 0;
    state.lastScrollHeight = 0;
    state.lastPostCount = state.posts.size;
    els.startBtn.disabled = true;
    els.resumeBtn.disabled = true;
    updateStatus();

    const tick = async () => {
      if (!state.running) return;
      try {
        await extractVisible(state);
      } catch (e) {
        // Une erreur ponctuelle (DOM Facebook qui change en plein scroll, etc.) ne doit
        // pas arrêter tout le scraping : on la journalise et on continue au prochain tick.
        console.warn("[FB Scraper] Erreur pendant l'extraction, on continue :", e);
      }
      updateStatus();
      schedulePersist(); // sauvegarde différée (max 1x/2s) : permet de reprendre après un crash/reload

      // Sécurité : relâche le focus au cas où un clic aurait ouvert un champ de saisie,
      // pour ne jamais laisser le scroll automatique se faire bloquer par ça.
      if (document.activeElement && document.activeElement !== document.body) {
        try { document.activeElement.blur(); } catch (e) {}
      }

      if (state.posts.size >= state.maxPosts) {
        setStatusMessage(`✅ Objectif atteint (${state.posts.size} posts).`);
        stopScraping();
        return;
      }

      // Détection de fin de flux : si ni le nombre de posts ni la hauteur de la page
      // n'ont bougé depuis N ticks, Facebook n'a plus rien de nouveau à charger
      // (au lieu de se fier à un simple délai fixe qui peut être trop court ou trop long).
      const currentScrollHeight = document.body.scrollHeight;
      const noNewPosts = state.posts.size === state.lastPostCount;
      const noNewHeight = currentScrollHeight === state.lastScrollHeight;
      if (noNewPosts && noNewHeight) {
        state.stableTicks++;
      } else {
        state.stableTicks = 0;
      }
      state.lastPostCount = state.posts.size;
      state.lastScrollHeight = currentScrollHeight;

      if (state.stableTicks >= state.maxStableTicks) {
        setStatusMessage(`🏁 Fin du flux détectée (plus de nouveau contenu après ${state.maxStableTicks} tentatives) — ${state.posts.size} posts.`);
        stopScraping();
        return;
      }

      window.scrollBy(0, window.innerHeight * 1.3);
      state.scrollTimer = setTimeout(tick, state.delay);
    };
    tick();
  }

  function stopScraping() {
    state.running = false;
    if (state.scrollTimer) clearTimeout(state.scrollTimer);
    if (state.saveTimer) { clearTimeout(state.saveTimer); state.saveTimer = null; }
    persistState(); // sauvegarde finale garantie, même si un autosave était en attente
    if (els.startBtn) els.startBtn.disabled = false;
    if (els.resumeBtn) els.resumeBtn.disabled = false;
    updateStatus();
  }

  // ----- Helpers génériques -----

  function clickMatching(container, regex) {
    const nodes = container.querySelectorAll('div[role="button"], span[role="button"], a[role="link"]');
    let clicked = false;
    nodes.forEach((n) => {
      const text = (n.innerText || "").trim();
      if (text && regex.test(text)) {
        try { n.click(); clicked = true; } catch (e) {}
      }
    });
    return clicked;
  }

  function cleanTextClone(node) {
    if (!node) return null;
    const clone = node.cloneNode(true);
    clone.querySelectorAll('div[role="button"], span[role="button"]').forEach((b) => b.remove());
    return clone;
  }

  function parseFirstNumber(str) {
    if (!str) return null;
    const m = String(str).replace(/\u00a0/g, " ").match(/[\d.,]+\s*[KkMm]?/);
    if (!m) return null;
    let v = m[0].trim();
    let mult = 1;
    if (/[Kk]$/.test(v)) { mult = 1000; v = v.slice(0, -1); }
    if (/[Mm]$/.test(v)) { mult = 1000000; v = v.slice(0, -1); }
    const num = parseFloat(v.replace(",", "."));
    return isNaN(num) ? null : Math.round(num * mult);
  }

  function extractIdFromUrl(url) {
    if (!url) return null;
    const m = url.match(/\/user\/(\d+)/) || url.match(/profile\.php\?id=(\d+)/) || url.match(/[?&]id=(\d+)/);
    return m ? m[1] : null;
  }

  function extractPostIdFromUrl(url) {
    if (!url) return null;
    const m = url.match(/\/posts\/(\d+)/) || url.match(/\/permalink\/(\d+)/) || url.match(/story_fbid=(\d+)/);
    return m ? m[1] : null;
  }

  function extractQueryParam(url, name) {
    if (!url) return null;
    try { return new URL(url).searchParams.get(name); } catch (e) { return null; }
  }

  function formatActors(name, id, url) {
    return `id: ${id || "null"}\nname: ${name || "null"}\nurl: ${url || "null"}`;
  }

  function countFromRenderingRole(container, role) {
    const marker = container.querySelector(`[data-ad-rendering-role="${role}"]`);
    if (!marker) return null;
    const clickable = marker.closest('[role="button"]') || marker.parentElement;
    return parseFirstNumber(clickable ? clickable.innerText : null);
  }

  function clickCommentToggle(container) {
    const marker = container.querySelector('[data-ad-rendering-role="comment_button"]');
    const clickable = marker ? marker.closest('[role="button"]') : null;
    if (clickable) { try { clickable.click(); return true; } catch (e) {} }
    return false;
  }

  // ----- Détection des posts (via data-ad-rendering-role) -----

  // Isole, pour chaque marqueur "story_message" (= texte d'un vrai post), le plus
  // petit ancêtre qui contient exactement UN story_message et UN profile_name —
  // ça capture le post dans son entier (header + texte + barre d'actions) sans
  // remonter jusqu'à un conteneur englobant plusieurs posts à la fois.
  function findPostContainers() {
    const messageNodes = document.querySelectorAll('[data-ad-rendering-role="story_message"]');
    const containers = [];

    messageNodes.forEach((msgEl) => {
      let node = msgEl;
      let best = msgEl;
      while (node && node !== document.body) {
        const msgCount = node.querySelectorAll('[data-ad-rendering-role="story_message"]').length;
        const nameCount = node.querySelectorAll('[data-ad-rendering-role="profile_name"]').length;
        if (msgCount === 1 && nameCount >= 1) {
          best = node;
        } else if (msgCount > 1) {
          break; // on a dépassé la frontière du post, on s'arrête au dernier "best" valide
        }
        node = node.parentElement;
      }
      if (!containers.some((c) => c.el === best)) {
        containers.push({ el: best, textEl: msgEl });
      }
    });

    // Ordre du document (haut vers bas) — important pour rattacher les commentaires
    containers.sort((a, b) => (a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
    return containers;
  }

  // Les commentaires ont un aria-label posé par Facebook lui-même : "Comment by X",
  // "Reply by X" (ou équivalents localisés) — indépendant du markup visuel.
  const COMMENT_ARIA_RE = /\b(comment|reply|answer)\s+by\b|commentaire de|réponse de|kommentar von|comentario de/i;

  function findCommentMarkers(root = document) {
    const all = root.querySelectorAll("[aria-label]");
    const markers = [];
    all.forEach((el) => {
      const label = el.getAttribute("aria-label") || "";
      if (COMMENT_ARIA_RE.test(label)) markers.push(el);
    });
    // Ne déduplique QUE les vrais doublons (même aria-label, un noeud contenant l'autre —
    // ex: un wrapper redondant autour du même commentaire). On garde en revanche les
    // commentaires imbriqués avec un aria-label DIFFÉRENT : ce sont de vraies réponses,
    // structurellement contenues dans le commentaire parent, pas des doublons.
    return markers.filter((el) => {
      const label = el.getAttribute("aria-label") || "";
      return !markers.some((other) => other !== el && other.contains(el) && (other.getAttribute("aria-label") || "") === label);
    });
  }

  // Ferme une fenêtre superposée (lightbox "Publication de X") ouverte par Facebook.
  function closeDialog(dialog) {
    const closeBtn = dialog.querySelector(
      '[aria-label="Close"], [aria-label="Fermer"], [aria-label="close"], [aria-label="fermer"]'
    );
    if (closeBtn) { try { closeBtn.click(); return; } catch (e) {} }
    try {
      const esc = { key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true };
      document.dispatchEvent(new KeyboardEvent("keydown", esc));
    } catch (e) {}
  }

  // Associe chaque commentaire au dernier post qui le PRÉCÈDE dans l'ordre du document —
  // plus robuste que de dépendre d'une relation d'imbrication DOM stricte.
  function assignCommentsToPosts(postContainers, commentMarkers) {
    const assignments = [];
    commentMarkers.forEach((marker) => {
      let owner = null;
      for (const post of postContainers) {
        const rel = post.el.compareDocumentPosition(marker);
        const markerIsAfter = !!(rel & Node.DOCUMENT_POSITION_FOLLOWING) || post.el.contains(marker);
        if (markerIsAfter) owner = post;
        else break;
      }
      if (owner) assignments.push({ marker, post: owner });
    });
    return assignments;
  }

  // ----- Extraction -----

  const EXPAND_RE = /(view more comments|view previous comments|voir plus de commentaires|voir (?:les )?commentaires précédents|afficher (?:plus|les) (?:commentaires|réponses)|view \d+\s*repl(?:y|ies)|voir \d+\s*réponses?|\d+\s*repl(?:y|ies)|\d+\s*réponses?)/i;

  const SORT_TRIGGER_RE = /^(most relevant|newest|oldest|top comments|plus pertinents|les plus récents|meilleurs commentaires)$/i;
  const ALL_COMMENTS_RE = /^(all comments|tous les commentaires|todos los comentarios|todos os comentários|alle kommentare|tutti i commenti)$/i;

  // Ouvre le menu de tri des commentaires (ex: "Plus pertinents ▾") et sélectionne
  // "Tous les commentaires" — sinon Facebook masque par défaut une partie des
  // commentaires (triés par pertinence), et on n'en récupérerait qu'un sous-ensemble.
  async function switchToAllComments(el) {
    const nodes = el.querySelectorAll('div[role="button"], span[role="button"]');
    let trigger = null;
    for (const n of nodes) {
      const t = (n.innerText || "").trim();
      if (t && SORT_TRIGGER_RE.test(t)) { trigger = n; break; }
    }
    if (!trigger) return false;

    const existingMenus = new Set(document.querySelectorAll('[role="menu"], [role="listbox"]'));
    try { trigger.click(); } catch (e) { return false; }

    let menu = null;
    for (let i = 0; i < 20; i++) {
      await sleep(100);
      const menus = Array.from(document.querySelectorAll('[role="menu"], [role="listbox"]')).filter((m) => !existingMenus.has(m));
      if (menus.length) { menu = menus[menus.length - 1]; break; }
    }
    if (!menu) return false;

    const items = menu.querySelectorAll('div[role="menuitem"], span[role="menuitem"], div, span');
    let target = null;
    for (const it of items) {
      const t = (it.innerText || "").trim();
      if (t && t.length < 60 && ALL_COMMENTS_RE.test(t)) { target = it; break; }
    }
    if (!target) {
      try { trigger.click(); } catch (e) {} // referme le menu proprement
      return false;
    }
    try { target.click(); } catch (e) { return false; }
    await sleep(700);
    return true;
  }

  // Clique de façon répétée sur tout ce qui permet de charger PLUS de commentaires
  // (pagination "voir plus de commentaires") ET les réponses à chaque commentaire
  // ("voir X réponses"), jusqu'à ce qu'il n'y ait plus rien de nouveau à déplier.
  // Contrairement à comment_button (qui OUVRE la saisie), ces liens se contentent
  // d'afficher du contenu déjà existant — sans danger pour le focus/scroll.
  async function expandAllComments(el, maxRounds = 25) {
    let stableRounds = 0;
    let lastCount = -1;

    for (let round = 0; round < maxRounds; round++) {
      const clicked = clickMatching(el, EXPAND_RE);
      if (clicked) await sleep(550);

      const currentCount = el.querySelectorAll("[aria-label]").length; // proxy grossier du volume chargé
      if (!clicked && currentCount === lastCount) break;

      if (currentCount === lastCount) {
        stableRounds++;
        if (stableRounds >= 2) break;
      } else {
        stableRounds = 0;
      }
      lastCount = currentCount;
    }
  }

  async function extractVisible(store) {
    const postContainers = findPostContainers();
    const today = todayLocalDateStr();

    for (const { el, textEl } of postContainers) {
      try {
        if (clickMatching(el, /^(see more|voir plus|afficher plus)$/i)) await sleep(250);

        const post = extractPost(el, textEl);
        const key = post.postId || "text:" + (post.text || "").slice(0, 80);
        post.postId = key; // garantit que Posts.postId == Comments.postId même sans id numérique extractible
        if (!store.posts.has(key)) {
          post.firstSeenDate = today; // sert à identifier "les posts d'aujourd'hui" pour la revérification des commentaires
          store.posts.set(key, post);
        }

        // NOTE : on ne clique PLUS sur comment_button — ce bouton sert à ÉCRIRE un
        // commentaire (ouvre le champ de saisie), pas à afficher les commentaires
        // existants. Le clic répété volait le focus et bloquait le scroll automatique.
        // On ne déplie qu'une seule fois par post (évite de tout refaire à chaque tick
        // tant que le post reste visible à l'écran pendant le scroll).
        if (!store.expandedPostKeys.has(key)) {
          store.expandedPostKeys.add(key);

          const existingDialogs = new Set(document.querySelectorAll('[role="dialog"]'));
          await switchToAllComments(el);
          await expandAllComments(el);

          // Sur certains posts, interagir avec les commentaires ouvre une fenêtre
          // superposée (lightbox "Publication de X") — un élément DOM séparé du post
          // dans le flux. On y bascule le tri + dépliage, on extrait ses commentaires
          // tout de suite (avant qu'elle ne se referme et emporte le DOM avec elle),
          // puis on la referme pour ne pas bloquer le scroll du flux principal.
          const newDialogs = Array.from(document.querySelectorAll('[role="dialog"]')).filter((d) => !existingDialogs.has(d));
          if (newDialogs.length) {
            const dialog = newDialogs[newDialogs.length - 1];
            await switchToAllComments(dialog);
            await expandAllComments(dialog);
            findCommentMarkers(dialog).forEach((marker) => extractComment(marker, key, store));
            closeDialog(dialog);
            await sleep(400);
          }
        }
      } catch (e) {
        // Ignore silencieusement les posts qui ne matchent pas le pattern attendu
      }
    }

    // Les commentaires sont recollectés sur l'ensemble de la page à chaque tick
    // (nouveaux posts + nouveaux commentaires dépliés depuis le dernier passage).
    const freshPostContainers = findPostContainers();
    const commentMarkers = findCommentMarkers();
    const assignments = assignCommentsToPosts(freshPostContainers, commentMarkers);

    assignments.forEach(({ marker, post }) => {
      const postKey = extractPostIdFromUrl(findPermalink(post.el)) || "text:" + (post.textEl.innerText || "").trim().slice(0, 80);
      extractComment(marker, postKey, store);
    });
  }

  function findPermalink(el) {
    const permalinkEl = el.querySelector('a[href*="/posts/"], a[href*="/permalink/"], a[href*="story_fbid"]');
    return permalinkEl ? permalinkEl.href.split("?")[0] : null;
  }

  function extractPost(el, textElRaw) {
    const textBlock = cleanTextClone(textElRaw);
    let text = textBlock ? textBlock.innerText.trim() : "";
    text = text.replace(/\s*(see more|voir plus)\.?$/i, "").trim();
    const html = textBlock ? textBlock.innerHTML.trim() : "";

    const nameMarker = el.querySelector('[data-ad-rendering-role="profile_name"]');
    const nameHeading = nameMarker ? nameMarker.querySelector("h2, h3, h4") : null;
    const author = nameHeading ? nameHeading.innerText.trim() : (nameMarker ? nameMarker.innerText.trim() : null);
    const authorLink = nameMarker ? (nameMarker.querySelector("a[href]") || el.querySelector('a[href*="/user/"]')) : null;
    const authorUrl = authorLink ? authorLink.href : null;
    const authorId = extractIdFromUrl(authorUrl);

    const permalink = findPermalink(el);
    const postId = extractPostIdFromUrl(permalink);

    const timeEl = el.querySelector("abbr, a[aria-label] span, a[role='link'] time");
    const publishTime = timeEl ? (timeEl.getAttribute("title") || timeEl.getAttribute("datetime") || "") : "";

    const images = Array.from(el.querySelectorAll('img[src*="scontent"]'))
      .map((img) => img.src)
      .filter((src, idx, arr) => arr.indexOf(src) === idx);

    const likesEl = el.querySelector('[aria-label*="reaction"], [aria-label*="réaction"], [aria-label*="J\'aime"]');
    const likes = parseFirstNumber(likesEl ? likesEl.getAttribute("aria-label") : null);
    const commentsCount = countFromRenderingRole(el, "comment_button");
    const sharesCount = countFromRenderingRole(el, "share_button");

    return {
      postId: postId || "",
      feedbackId: "", // non disponible via le DOM visible (id GraphQL interne)
      actors: formatActors(author, authorId, authorUrl),
      text,
      html,
      attachments: images.join("\n"),
      subattachments: "",
      comments: commentsCount ?? "",
      likes: likes ?? "",
      shares: sharesCount ?? "",
      pageUrl: location.href.split("?")[0],
      postUrl: permalink || "",
      publishTime,
      scrapedAt: new Date().toISOString(),
    };
  }

  // Un noeud de texte qui appartient à une RÉPONSE imbriquée (et non au commentaire
  // "marker" lui-même) a un ancêtre — avant d'atteindre marker — qui est lui-même un
  // autre marqueur de commentaire. On l'exclut pour ne pas piocher le texte d'une
  // réponse à la place du texte du commentaire parent.
  function belongsToNestedComment(node, marker) {
    let n = node.parentElement;
    while (n && n !== marker) {
      const label = n.getAttribute && n.getAttribute("aria-label");
      if (label && COMMENT_ARIA_RE.test(label)) return true;
      n = n.parentElement;
    }
    return false;
  }

  function extractComment(marker, postId, store) {
    try {
      const label = marker.getAttribute("aria-label") || "";
      const nameMatch = label.match(/\b(?:comment|reply|answer)\s+by\s+(.+?)(?:$|,|\.|\s+(?:on|at)\b)/i);
      let author = nameMatch ? nameMatch[1].trim() : null;

      const authorLink = Array.from(marker.querySelectorAll('a[role="link"][href], a[href*="/user/"]'))
        .find((a) => !belongsToNestedComment(a, marker));
      const authorUrl = authorLink ? authorLink.href : null;
      const authorId = extractIdFromUrl(authorUrl);
      if (!author && authorLink) author = authorLink.innerText.trim();

      const textNodes = Array.from(marker.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
        .filter((n) => !n.closest('[role="button"]') && !belongsToNestedComment(n, marker))
        .map((n) => n.innerText.trim())
        .filter(Boolean);
      const text = textNodes.sort((a, b) => b.length - a.length)[0] || "";
      if (!text) return;

      const permalinkEl = Array.from(marker.querySelectorAll('a[href*="comment_id="]'))
        .find((a) => !belongsToNestedComment(a, marker));
      const commentUrl = permalinkEl ? permalinkEl.href.split("?")[0] : "";
      const commentId = extractQueryParam(commentUrl, "comment_id") || ("c:" + text.slice(0, 60));

      const likesEl = Array.from(marker.querySelectorAll('[aria-label*="reaction"], [aria-label*="réaction"]'))
        .find((n) => !belongsToNestedComment(n, marker));
      const likes = parseFirstNumber(likesEl ? likesEl.getAttribute("aria-label") : null);

      const images = Array.from(marker.querySelectorAll('img[src*="scontent"]'))
        .filter((img) => !belongsToNestedComment(img, marker))
        .map((img) => img.src);

      // Remonte jusqu'au premier ANCÊTRE qui est lui-même un marqueur de commentaire —
      // s'il y en a un, ce commentaire est en réalité une réponse à celui-ci.
      let parentCommentId = "";
      let n = marker.parentElement;
      while (n && n !== document.body) {
        const l = n.getAttribute && n.getAttribute("aria-label");
        if (l && COMMENT_ARIA_RE.test(l)) { parentCommentId = store.commentIdByMarkerEl.get(n) || ""; break; }
        n = n.parentElement;
      }

      const key = postId + "::" + commentId;
      store.commentIdByMarkerEl.set(marker, commentId);
      if (store.comments.has(key)) return false;

      store.comments.set(key, {
        commentId,
        postId,
        parentCommentId,
        isReply: !!parentCommentId,
        actors: formatActors(author, authorId, authorUrl),
        text,
        html: text,
        attachments: images.join("\n"),
        likes: likes ?? "",
        commentUrl,
        scrapedAt: new Date().toISOString(),
      });
      return true;
    } catch (e) {
      // Ignore les fragments qui ne correspondent pas au pattern attendu
      return false;
    }
  }

  // ===== Mode automatique (piloté par background.js via chrome.alarms) =====
  //
  // Contrairement au mode manuel (state global, scroll piloté par l'utilisateur),
  // le mode auto travaille sur un "store" éphémère reconstruit à chaque appel à
  // partir de chrome.storage.local, sous une clé stable ("sourceKey") qui NE dépend
  // PAS de location.href — indispensable car on va aussi naviguer vers des permalinks
  // de posts individuels pour revérifier leurs commentaires, sur une URL différente
  // de celle du fil d'actualité d'origine.

  function makeEmptyStore() {
    return { posts: new Map(), comments: new Map(), expandedPostKeys: new Set(), commentIdByMarkerEl: new Map() };
  }

  async function loadStoreForSource(sourceKey) {
    const saved = await loadPersistedState(sourceKey);
    const store = makeEmptyStore();
    if (saved) {
      store.posts = new Map(saved.posts || []);
      store.comments = new Map(saved.comments || []);
      store.expandedPostKeys = new Set(saved.expandedPostKeys || []);
    }
    return store;
  }

  function persistStore(sourceKey, store) {
    return new Promise((resolve) => {
      const payload = {
        posts: Array.from(store.posts.entries()),
        comments: Array.from(store.comments.entries()),
        expandedPostKeys: Array.from(store.expandedPostKeys),
        savedAt: new Date().toISOString(),
      };
      try {
        chrome.storage.local.set({ [sourceKey]: payload }, () => resolve(true));
      } catch (e) {
        resolve(false);
      }
    });
  }

  // Tentative best-effort de forcer le tri du FIL (pas des commentaires) sur
  // "Plus récents"/"Most recent" plutôt que "Plus pertinents" — indispensable en
  // mode auto pour que les nouveaux posts apparaissent en haut à chaque passage,
  // et qu'on puisse s'arrêter tôt dès qu'on retombe sur du déjà-connu. Si Facebook
  // ne propose pas ce contrôle sur cette page (ex: page sans onglet "Discussion"),
  // on continue quand même avec le tri par défaut.
  const FEED_SORT_TRIGGER_RE = /^(most relevant|top posts|plus pertinents?|publications les plus pertinentes)$/i;
  const FEED_SORT_RECENT_RE = /^(most recent|new posts first|plus r[ée]cents?|r[ée]centes? d'abord)$/i;

  async function tryForceRecentFeedSort() {
    const nodes = document.querySelectorAll('div[role="button"], span[role="button"]');
    let trigger = null;
    for (const n of nodes) {
      const t = (n.innerText || "").trim();
      if (t && FEED_SORT_TRIGGER_RE.test(t)) { trigger = n; break; }
    }
    if (!trigger) return false;

    const existingMenus = new Set(document.querySelectorAll('[role="menu"], [role="listbox"]'));
    try { trigger.click(); } catch (e) { return false; }

    let menu = null;
    for (let i = 0; i < 20; i++) {
      await sleep(100);
      const menus = Array.from(document.querySelectorAll('[role="menu"], [role="listbox"]')).filter((m) => !existingMenus.has(m));
      if (menus.length) { menu = menus[menus.length - 1]; break; }
    }
    if (!menu) return false;

    const items = menu.querySelectorAll('div[role="menuitem"], span[role="menuitem"], div, span');
    let target = null;
    for (const it of items) {
      const t = (it.innerText || "").trim();
      if (t && t.length < 60 && FEED_SORT_RECENT_RE.test(t)) { target = it; break; }
    }
    if (!target) { try { trigger.click(); } catch (e) {} return false; }
    try { target.click(); } catch (e) { return false; }
    await sleep(800);
    return true;
  }

  // Scrappe uniquement les posts NOUVEAUX depuis le dernier passage : on trie par
  // "Plus récents" pour que le nouveau contenu soit en haut, puis on scrolle en
  // s'arrêtant dès que le nombre total de posts connus ne progresse plus pendant
  // quelques ticks d'affilée (pas besoin de re-descendre dans tout l'historique
  // déjà connu à chaque passage — contrairement au mode manuel qui, lui, vise à
  // tout récupérer en une fois).
  async function autoScrapeNewPosts(sourceKey, opts = {}) {
    const { maxNewPosts = 80, maxTicks = 40, stableTicksToStop = 3, delay = 1600 } = opts;
    const store = await loadStoreForSource(sourceKey);
    const startCount = store.posts.size;

    await tryForceRecentFeedSort();
    await sleep(700);

    let stableTicks = 0;
    let lastCount = store.posts.size;

    for (let tick = 0; tick < maxTicks; tick++) {
      try {
        await extractVisible(store);
      } catch (e) {
        console.warn("[FB Scraper][auto] erreur pendant l'extraction, on continue :", e);
      }

      if (document.activeElement && document.activeElement !== document.body) {
        try { document.activeElement.blur(); } catch (e) {}
      }

      const newSinceStart = store.posts.size - startCount;
      if (newSinceStart >= maxNewPosts) break;

      if (store.posts.size === lastCount) {
        stableTicks++;
        if (stableTicks >= stableTicksToStop) break;
      } else {
        stableTicks = 0;
      }
      lastCount = store.posts.size;

      window.scrollBy(0, window.innerHeight * 1.3);
      await sleep(delay);
    }

    await persistStore(sourceKey, store);
    return { newPostsCount: store.posts.size - startCount, totalPosts: store.posts.size, totalComments: store.comments.size };
  }

  // Revisite un post déjà connu (identifié par son permalink) pour n'en récupérer
  // que les NOUVEAUX commentaires. Appelé après navigation directe vers postUrl —
  // bien plus fiable que de re-scroller tout le fil pour retrouver un vieux post.
  async function autoRecheckPostComments(sourceKey, postId, opts = {}) {
    const { timeoutMs = 9000 } = opts;
    const store = await loadStoreForSource(sourceKey);
    const beforeCount = store.comments.size;

    // Attend que la page du permalink ait fini de rendre le post (SPA : le DOM
    // n'est pas forcément prêt juste après le changement d'URL).
    let containers = [];
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      containers = findPostContainers();
      if (containers.length) break;
      await sleep(300);
    }
    if (!containers.length) {
      return { newCommentsCount: 0, error: "post introuvable sur la page (DOM pas chargé ou post supprimé)" };
    }

    // Sur une page de permalink il n'y a normalement qu'un seul post principal ;
    // s'il y en a plusieurs (posts liés suggérés en bas), on privilégie celui dont
    // l'URL correspond au postId recherché, sinon on prend le premier.
    let target = containers.find((c) => extractPostIdFromUrl(findPermalink(c.el)) === postId) || containers[0];

    try {
      const existingDialogs = new Set(document.querySelectorAll('[role="dialog"]'));
      await switchToAllComments(target.el);
      await expandAllComments(target.el);
      findCommentMarkers(target.el).forEach((marker) => extractComment(marker, postId, store));

      // Idem que dans extractVisible : certaines interactions ouvrent une lightbox
      // séparée du DOM principal.
      const newDialogs = Array.from(document.querySelectorAll('[role="dialog"]')).filter((d) => !existingDialogs.has(d));
      if (newDialogs.length) {
        const dialog = newDialogs[newDialogs.length - 1];
        await switchToAllComments(dialog);
        await expandAllComments(dialog);
        findCommentMarkers(dialog).forEach((marker) => extractComment(marker, postId, store));
        closeDialog(dialog);
      }
    } catch (e) {
      console.warn("[FB Scraper][auto] erreur pendant la revérification des commentaires :", e);
    }

    await persistStore(sourceKey, store);
    return { newCommentsCount: store.comments.size - beforeCount, totalComments: store.comments.size };
  }



  const POST_HEADERS = ["postId", "feedbackId", "actors", "text", "html", "attachments", "subattachments", "comments", "likes", "shares", "pageUrl", "postUrl", "publishTime", "scrapedAt"];
  const POST_HEADER_LABELS = ["Post Id", "Feedback Id", "Actors", "Text", "Html", "Attachments", "Subattachments", "Comments", "Likes", "Shares", "Page Url", "Post Url", "Publish Time", "Scraped At"];

  const COMMENT_HEADERS = ["commentId", "postId", "isReply", "parentCommentId", "actors", "text", "html", "attachments", "likes", "commentUrl", "scrapedAt"];
  const COMMENT_HEADER_LABELS = ["Comment Id", "Post Id", "Is Reply", "Parent Comment Id", "Actors", "Text", "Html", "Attachments", "Likes", "Comment Url", "Scraped At"];

  function toCsvRow(obj, headers) {
    return headers.map((h) => `"${String(obj[h] ?? "").replace(/"/g, '""')}"`).join(",");
  }

  function exportPostsCsv() {
    const rows = Array.from(state.posts.values()).map((p) => toCsvRow(p, POST_HEADERS));
    return [POST_HEADER_LABELS.join(","), ...rows].join("\n");
  }

  function exportCommentsCsv() {
    const rows = Array.from(state.comments.values()).map((c) => toCsvRow(c, COMMENT_HEADERS));
    return [COMMENT_HEADER_LABELS.join(","), ...rows].join("\n");
  }

  function exportCombinedJson() {
    const commentsByPost = {};
    state.comments.forEach((c) => {
      (commentsByPost[c.postId] = commentsByPost[c.postId] || []).push(c);
    });
    const posts = Array.from(state.posts.values()).map((p) => ({
      ...p,
      commentsData: commentsByPost[p.postId] || [],
    }));
    return JSON.stringify(posts, null, 2);
  }

  // Filet de sécurité : si l'onglet passe en arrière-plan (l'utilisateur change d'onglet,
  // ce qui peut aussi ralentir/throttle le setTimeout du scraping) ou si la page se
  // décharge, on force une sauvegarde immédiate plutôt que d'attendre le debounce de 2s.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && state.running) persistState();
  });
  window.addEventListener("beforeunload", () => {
    if (state.running) persistState();
  });

  buildPanel();
})();
