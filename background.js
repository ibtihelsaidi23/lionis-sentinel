// background.js — orchestre le suivi automatique quotidien :
// - toutes les X minutes (par source, configurable dans le panneau), ouvre la
//   page du groupe/de la page dans un onglet en arrière-plan et scrape les
//   NOUVEAUX posts (state.posts déjà connus sont ignorés/dédupliqués côté content.js) ;
// - pour chaque post identifié comme "d'aujourd'hui", navigue directement vers son
//   permalink pour ne récupérer que les nouveaux commentaires (bien plus fiable
//   que de re-scroller tout le fil pour retrouver un vieux post) ;
// - toutes les données sont fusionnées dans chrome.storage.local sous la même clé
//   que le mode manuel ("fbScraperData::<url nettoyée>"), donc le panneau (Reprendre,
//   export JSON/CSV) fonctionne aussi bien sur des données collectées manuellement
//   qu'automatiquement.
//
// LIMITE CONNUE (Manifest V3) : le service worker peut être arrêté par Chrome
// s'il reste inactif trop longtemps ou si une exécution est anormalement longue.
// Le design est volontairement incrémental et idempotent (dédup par postId/commentId,
// jamais de perte ni de doublon) : si une exécution est interrompue, elle reprendra
// simplement là où elle s'était arrêtée au prochain déclenchement du "heartbeat".

const SOURCES_KEY = "fbScraperSources";
const HEARTBEAT_ALARM = "fb-scraper-heartbeat";
const HEARTBEAT_PERIOD_MINUTES = 5; // granularité de vérification ; l'intervalle réel par source est respecté au-dessus de ce plancher

let isProcessing = false;

// ----- Utilitaires -----

function canonicalUrl(url) {
  return url.split("?")[0].replace(/\/$/, "");
}

function storageKeyForUrl(url) {
  return "fbScraperData::" + canonicalUrl(url);
}

function todayLocalDateStr() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function getSources() {
  return new Promise((resolve) => {
    chrome.storage.local.get([SOURCES_KEY], (r) => resolve(r[SOURCES_KEY] || []));
  });
}

function saveSources(list) {
  return new Promise((resolve) => {
    chrome.storage.local.set({ [SOURCES_KEY]: list }, () => resolve(true));
  });
}

function getStoredData(sourceKey) {
  return new Promise((resolve) => {
    chrome.storage.local.get([sourceKey], (r) => resolve(r[sourceKey] || null));
  });
}

// Attend qu'un onglet ait fini de charger (status "complete"), avec timeout de secours.
function waitForTabComplete(tabId, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    };
    const listener = (updatedTabId, info) => {
      if (updatedTabId === tabId && info.status === "complete") finish();
    };
    chrome.tabs.onUpdated.addListener(listener);
    // Vérifie aussi l'état actuel tout de suite (l'onglet peut déjà être "complete").
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError) { finish(); return; }
      if (tab && tab.status === "complete") finish();
    });
    setTimeout(finish, timeoutMs);
  });
}

// Envoie un message à un onglet en réessayant brièvement : juste après un chargement
// de page Facebook (SPA), le content script peut avoir besoin d'un instant de plus.
function sendMessageWithRetry(tabId, message, { retries = 5, delayMs = 800 } = {}) {
  return new Promise((resolve) => {
    let attempt = 0;
    const tryOnce = () => {
      attempt++;
      chrome.tabs.sendMessage(tabId, message, (response) => {
        if (chrome.runtime.lastError || !response) {
          if (attempt < retries) { setTimeout(tryOnce, delayMs); return; }
          resolve({ ok: false, error: chrome.runtime.lastError?.message || "no response" });
          return;
        }
        resolve(response);
      });
    };
    tryOnce();
  });
}

// ----- Traitement d'une source (un groupe/une page) -----

async function processSource(source) {
  const sourceKey = storageKeyForUrl(source.url);
  const log = (msg) => console.log(`[FB Scraper][auto][${source.url}] ${msg}`);

  let tab;
  try {
    tab = await chrome.tabs.create({ url: source.url, active: false });
  } catch (e) {
    log("impossible d'ouvrir l'onglet : " + e);
    return;
  }

  try {
    await waitForTabComplete(tab.id, 25000);
    await sleep(2500); // laisse le temps au SPA Facebook de finir de rendre le fil

    // Étape 1 : récupérer les nouveaux posts depuis le dernier passage.
    const scrapeResult = await sendMessageWithRetry(tab.id, {
      type: "AUTO_SCRAPE_NEW_POSTS",
      sourceKey,
      opts: { maxNewPosts: 100, maxTicks: 50, stableTicksToStop: 3, delay: 1600 },
    });
    log(`nouveaux posts : ${scrapeResult.newPostsCount ?? "?"} (total ${scrapeResult.totalPosts ?? "?"})`);

    // Étape 2 : revérifier les commentaires des posts DU JOUR déjà connus (nouveaux
    // ou plus anciens dans la journée), pour capter les commentaires arrivés depuis.
    const data = await getStoredData(sourceKey);
    const today = todayLocalDateStr();
    const todaysPosts = ((data && data.posts) || [])
      .map(([, p]) => p)
      .filter((p) => p.firstSeenDate === today && p.postUrl);

    const MAX_RECHECKS_PER_RUN = 60; // garde-fou pour ne pas enchaîner des centaines de navigations en une seule fois
    const toRecheck = todaysPosts.slice(0, MAX_RECHECKS_PER_RUN);
    log(`revérification des commentaires sur ${toRecheck.length} post(s) du jour...`);

    for (const post of toRecheck) {
      try {
        await chrome.tabs.update(tab.id, { url: post.postUrl });
        await waitForTabComplete(tab.id, 20000);
        await sleep(1500);
        const recheckResult = await sendMessageWithRetry(tab.id, {
          type: "AUTO_RECHECK_POST_COMMENTS",
          sourceKey,
          postId: post.postId,
          opts: {},
        });
        if (recheckResult && recheckResult.newCommentsCount) {
          log(`+${recheckResult.newCommentsCount} nouveau(x) commentaire(s) sur ${post.postUrl}`);
        }
        await sleep(1200); // petite pause entre deux posts, pour rester raisonnable
      } catch (e) {
        log(`erreur en revérifiant ${post.postUrl} : ${e}`);
        // On continue avec le post suivant plutôt que d'abandonner toute la source.
      }
    }
  } catch (e) {
    log("erreur pendant le traitement : " + e);
  } finally {
    try { await chrome.tabs.remove(tab.id); } catch (e) {}
  }
}

// ----- Heartbeat : décide, pour chaque source, si son intervalle est écoulé -----

async function runHeartbeat() {
  if (isProcessing) return; // évite le chevauchement si un cycle précédent traîne encore
  isProcessing = true;
  try {
    const sources = await getSources();
    if (!sources.length) return;

    const now = Date.now();
    for (const source of sources) {
      const intervalMs = Math.max(10, source.intervalMinutes || 30) * 60000;
      const last = source.lastRunAt ? new Date(source.lastRunAt).getTime() : 0;
      if (now - last < intervalMs) continue; // pas encore l'heure pour cette source

      source.lastRunAt = new Date().toISOString(); // marqué avant traitement pour éviter un double-run si le heartbeat suivant tombe pendant un run très long
      await saveSources(sources);
      await processSource(source);
    }
  } finally {
    isProcessing = false;
  }
}

function ensureHeartbeatAlarm() {
  chrome.alarms.get(HEARTBEAT_ALARM, (existing) => {
    if (!existing) {
      chrome.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: HEARTBEAT_PERIOD_MINUTES });
    }
  });
}

chrome.runtime.onInstalled.addListener(ensureHeartbeatAlarm);
chrome.runtime.onStartup.addListener(ensureHeartbeatAlarm);

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === HEARTBEAT_ALARM) runHeartbeat();
});

// ----- Messages venant du panneau (content.js) -----

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "REFRESH_AUTO_SOURCES") {
    ensureHeartbeatAlarm();
    runHeartbeat(); // donne un retour rapide dès l'activation, sans attendre le prochain tick
    sendResponse({ ok: true });
    return true;
  }
  return true;
});

// ----- Toggle du panneau via l'icône de l'extension -----

chrome.action.onClicked.addListener((tab) => {
  if (!tab.id || !tab.url || !tab.url.includes("facebook.com")) return;
  chrome.tabs.sendMessage(tab.id, { type: "TOGGLE_PANEL" });
});
