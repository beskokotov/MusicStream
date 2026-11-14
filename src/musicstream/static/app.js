"use strict";

const form = document.querySelector("#search-form");
const input = document.querySelector("#search-input");
const region = document.querySelector("#results-region");
const status = document.querySelector("#search-status");
const state = document.querySelector("#empty-state");
const stateTitle = document.querySelector("#state-title");
const stateDescription = document.querySelector("#state-description");
const retry = document.querySelector("#retry-button");
const table = document.querySelector("#track-table");
const list = document.querySelector("#track-list");
const count = document.querySelector("#result-count");
const template = document.querySelector("#track-template");
const config = window.MUSICSTREAM_CONFIG || {};
let demoMode = config.demoMode !== false;
let apiBaseUrl = "";
try {
  if (config.apiBaseUrl) {
    const url = new URL(config.apiBaseUrl);
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash
        || ["api.deezer.com", "musicbrainz.org"].includes(url.hostname)) {
      throw new Error("Invalid backend URL");
    }
    apiBaseUrl = url.href.replace(/\/+$/, "");
  }
} catch {
  demoMode = true;
}
let timer;
let controller;
let revision = 0;

class SearchError extends Error {}

function duration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—:—";
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function setState(title, description, { loading = false, error = false } = {}) {
  state.hidden = false;
  table.hidden = true;
  state.classList.toggle("loading", loading);
  stateTitle.textContent = title;
  stateDescription.textContent = description;
  retry.hidden = !error;
  region.setAttribute("aria-busy", String(loading));
  status.textContent = `${title} ${description}`;
}

function reset() {
  list.replaceChildren();
  if (demoMode) {
    count.textContent = "Предпросмотр интерфейса";
    setState("Реальный поиск пока недоступен", "Сейчас открыт демонстрационный режим.\nПоиск появится после подключения внешнего backend.");
    return;
  }
  count.textContent = "Начните с любимого исполнителя";
  setState("Что звучит у вас в голове?", "Введите название трека или имя исполнителя.\nВаша следующая находка — всего в одном поиске.");
}

function addCover(container, url) {
  container.querySelectorAll("img").forEach(image => image.remove());
  if (typeof url !== "string" || !url) return;
  try {
    const parsed = new URL(url);
    if (!["https:", "http:"].includes(parsed.protocol)) return;
    const image = document.createElement("img");
    image.alt = ""; // The adjacent title already identifies the track.
    image.loading = "lazy";
    image.decoding = "async";
    image.referrerPolicy = "no-referrer";
    image.addEventListener("error", () => image.remove(), { once: true });
    image.src = parsed.href;
    container.append(image);
  } catch {
    // Missing, invalid and failed covers keep the neutral music-note placeholder.
  }
}

function selectTrack(track, row) {
  list.querySelectorAll(".track-row").forEach(item => {
    const selected = item === row;
    item.classList.toggle("selected", selected);
    item.querySelector(".track-title").setAttribute("aria-pressed", String(selected));
  });
  document.querySelector("#player-title").textContent = track.title || "Без названия";
  document.querySelector("#player-artist").textContent = track.artist || "Исполнитель неизвестен";
  document.querySelector("#player-duration").textContent = duration(track.duration_seconds);
  addCover(document.querySelector("#player-cover"), track.cover_url);
  status.textContent = `Выбрана композиция ${track.title || "без названия"}. Ищем источник воспроизведения.`;
  musicPlayer.select(track);
}

function render(tracks) {
  const fragment = document.createDocumentFragment();
  tracks.forEach((track, index) => {
    const row = template.content.firstElementChild.cloneNode(true);
    const title = row.querySelector(".track-title");
    row.querySelector(".track-number").textContent = String(index + 1).padStart(2, "0");
    title.textContent = track.title || "Без названия";
    title.title = track.title || "Без названия";
    title.setAttribute("aria-label", `Показать в панели: ${track.title || "Без названия"}, ${track.artist || "исполнитель неизвестен"}`);
    title.setAttribute("aria-pressed", "false");
    title.addEventListener("click", () => selectTrack(track, row));
    row.querySelector(".track-artist").textContent = track.artist || "Исполнитель неизвестен";
    const album = row.querySelector(".track-album");
    album.textContent = track.album || "Альбом неизвестен";
    album.title = track.album || "Альбом неизвестен";
    row.querySelector(".track-duration").textContent = duration(track.duration_seconds);
    addCover(row.querySelector(".track-cover"), track.cover_url);
    fragment.append(row);
  });
  list.replaceChildren(fragment);
  state.hidden = true;
  table.hidden = false;
  region.setAttribute("aria-busy", "false");
  count.textContent = `Найдено: ${tracks.length}`;
  status.textContent = `Поиск завершён. Найдено композиций: ${tracks.length}.`;
}

function cancelSearch() {
  clearTimeout(timer);
  controller?.abort();
  controller = undefined;
  revision += 1;
}

async function search() {
  cancelSearch();
  if (demoMode) {
    reset();
    return;
  }
  const query = input.value.trim();
  if (!query) {
    reset();
    return;
  }
  const current = revision;
  controller = new AbortController();
  count.textContent = "Ищем в каталоге Deezer";
  setState("Ищем вашу музыку…", "Ещё немного — собираем результаты.", { loading: true });
  try {
    const params = new URLSearchParams({ q: query, limit: "25" });
    const response = await fetch(`${apiBaseUrl}/api/search?${params}`, { signal: controller.signal });
    if (current !== revision) return;
    if (!response.ok) {
      const messages = {
        422: "Проверьте поисковый запрос и попробуйте снова.",
        503: "Музыкальный каталог временно недоступен. Попробуйте немного позже.",
        504: "Поиск занял слишком много времени. Попробуйте ещё раз.",
      };
      throw new SearchError(messages[response.status] || "Не удалось загрузить результаты. Попробуйте ещё раз.");
    }
    const tracks = await response.json();
    if (current !== revision) return;
    if (!Array.isArray(tracks) || tracks.some(track => !track || typeof track !== "object" || Array.isArray(track))) {
      throw new SearchError("Не удалось прочитать результаты поиска. Попробуйте ещё раз.");
    }
    if (tracks.length === 0) {
      count.textContent = "Найдено: 0";
      list.replaceChildren();
      setState("Пока ничего не нашлось", "Попробуйте другое название или проверьте имя исполнителя.");
    } else {
      render(tracks);
    }
  } catch (error) {
    if (current !== revision || error.name === "AbortError") return;
    count.textContent = "Не удалось выполнить поиск";
    const message = error instanceof SearchError ? error.message
      : "Не удалось связаться с сервером. Проверьте подключение и повторите поиск.";
    setState("Музыка немного задерживается", message, { error: true });
  } finally {
    if (current === revision) {
      controller = undefined;
      region.setAttribute("aria-busy", "false");
    }
  }
}

input.addEventListener("input", () => {
  cancelSearch();
  if (demoMode) {
    reset();
    return;
  }
  if (!input.value.trim()) {
    reset();
    return;
  }
  count.textContent = "Новый запрос";
  setState("Готовим поиск…", "Введите название или нажмите Enter.");
  timer = setTimeout(search, 350);
});

form.addEventListener("submit", event => {
  event.preventDefault();
  search();
});
retry.addEventListener("click", search);
document.querySelectorAll("[data-query]").forEach(button => {
  button.addEventListener("click", () => {
    input.value = button.dataset.query;
    search();
  });
});

document.querySelector("#demo-banner").hidden = !demoMode;
if (demoMode) {
  document.querySelector("#demo-banner").hidden = false;
  document.querySelector(".source-pill").textContent = "Демо · без подключения к API";
  document.querySelector(".page-footer > span:last-child").textContent = "MusicStream · демонстрационный режим";
  input.placeholder = "Поиск пока недоступен";
  input.disabled = true;
  document.querySelector("#search-submit").disabled = true;
  document.querySelectorAll("[data-query]").forEach(button => { button.disabled = true; });
}
reset();





// Official YouTube IFrame API only. No media URLs or audio extraction.
const musicPlayer = (() => {
  const section = document.querySelector("#playback-section");
  const message = document.querySelector("#source-status");
  const consent = document.querySelector("#youtube-consent");
  const source = document.querySelector("#audio-source");
  const candidates = document.querySelector("#source-candidates");
  const reports = document.querySelector("#source-reports");
  const frameArea = document.querySelector("#youtube-frame-area");
  const play = document.querySelector("#player-play");
  const seek = document.querySelector("#player-seek");
  const volume = document.querySelector("#player-volume");
  const playbackState = document.querySelector("#playback-state");
  const resolveButton = document.querySelector("#resolve-source");
  const externalSearch = document.querySelector("#youtube-search-link");
  const externalVideo = document.querySelector("#youtube-video-link");
  const hitmosSearch = document.querySelector("#hitmos-search-link");
  const hitmosForm = document.querySelector("#hitmos-manual-form");
  const hitmosLink = document.querySelector("#hitmos-song-link");
  const audio = document.querySelector("#hitmos-audio");
  const diagnosticPanel = document.querySelector("#source-diagnostics");
  let diagnosticsEnabled = false;
  const stageLabels = { source_search: "поиск источника", source_metadata: "метаданные источника", matching: "сопоставление записей", stream_resolution: "получение адреса", cdn_response: "загрузка CDN", browser_playback: "воспроизведение браузером" };
  let activeKind = null;
  let preparedSource = null;
  let selected, player, request, polling, apiPromise, frameTimer;
  let generation = 0;
  let ready = false;
  let torrentAutoAvailable = false;
  let torrentJobId = null;
  let torrentRequest = null;
  let torrentPlayStarted = null;
  let torrentFirstProgress = false;
  let torrentPlaybackId = null;
  let torrentStreamUrl = null;
  let torrentKeepalive = null;
  let torrentRecoveryAttempts = 0;
  let torrentShouldResume = false;
  let automaticHitmos = null;
  // Previous browser measurements: Hitmos playback became active in 1.85–2.52 s
  // after a resolved stream was assigned. Keep a modest margin for CDN variance.
  const HITMOS_LOOKUP_TIMEOUT_MS = 6500;
  const HITMOS_AUDIO_START_TIMEOUT_MS = 4000;

  function logTorrentMilestone(stage) {
    if (torrentPlayStarted === null) return;
    console.info("MusicStream Auto timing", {
      stage, elapsed_ms: Math.round(performance.now() - torrentPlayStarted),
    });
  }

  function controls(enabled) {
    ready = enabled;
    play.disabled = !enabled;
    volume.disabled = !enabled;
    seek.disabled = true;
    play.textContent = "▶";
    play.setAttribute("aria-label", activeKind === "torrent" ? "Воспроизвести аудио" : activeKind === "hitmos" ? "Воспроизвести аудио Hitmos" : "Воспроизвести видео YouTube");
  }

  function stop() {
    generation += 1;
    if (automaticHitmos?.timer) clearTimeout(automaticHitmos.timer);
    automaticHitmos = null;
    torrentPlayStarted = null;
    torrentFirstProgress = false;
    releaseTorrentPlayback();
    torrentStreamUrl = null;
    torrentRecoveryAttempts = 0;
    torrentShouldResume = false;
    clearInterval(torrentKeepalive);
    torrentKeepalive = null;
    torrentRequest?.abort();
    torrentRequest = null;
    if (torrentJobId) {
      const cancelled = torrentJobId;
      torrentJobId = null;
      fetch(`/api/experimental/playback/prepare/${encodeURIComponent(cancelled)}`, { method: "DELETE" }).catch(() => {});
    }
    preparedSource = null;
    activeKind = null;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    audio.hidden = true;
    request?.abort();
    request = undefined;
    clearInterval(polling);
    clearTimeout(frameTimer);
    player?.destroy();
    player = undefined;
    frameArea.replaceChildren();
    frameArea.hidden = true;
    externalVideo.hidden = true;
    controls(false);
    resolveButton.disabled = false;
    section.setAttribute("aria-busy", "false");
    seek.value = "0";
    document.querySelector("#player-time").textContent = "—:—";
  }

  function fail(text, stage = null, code = null) {
    controls(false);
    clearInterval(torrentKeepalive);
    torrentKeepalive = null;
    if (activeKind === "torrent" && torrentJobId) {
      const cancelled = torrentJobId;
      torrentJobId = null;
      fetch(`/api/experimental/playback/prepare/${encodeURIComponent(cancelled)}`, { method: "DELETE" }).catch(() => {});
    }
    if (activeKind === "torrent") releaseTorrentPlayback();
    if (activeKind === "hitmos" || activeKind === "torrent") audio.pause();
    clearInterval(polling);
    clearTimeout(frameTimer);
    playbackState.textContent = "Недоступно";
    message.textContent = stage ? `Этап: ${stageLabels[stage] || stage}. ${text}` : text;
    if (diagnosticsEnabled && stage) {
      // Only explicit non-sensitive facts; never log event/error objects or src.
      console.info("MusicStream audio", { stage, code, media_error_code: audio.error?.code || null,
        ready_state: audio.readyState, network_state: audio.networkState });
    }
    section.setAttribute("aria-busy", "false");
  }

  function loadAPI() {
    if (window.YT?.Player) return Promise.resolve();
    if (apiPromise) return apiPromise;
    apiPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      const timeout = setTimeout(() => {
        script.remove();
        apiPromise = undefined;
        reject(new Error("YouTube API timeout"));
      }, 12000);
      window.onYouTubeIframeAPIReady = () => { clearTimeout(timeout); resolve(); };
      script.addEventListener("error", () => {
        clearTimeout(timeout);
        script.remove();
        apiPromise = undefined;
        reject(new Error("YouTube API unavailable"));
      });
      script.src = "https://www.youtube.com/iframe_api";
      document.head.append(script);
    });
    return apiPromise;
  }

  function updateProgress() {
    if (!ready || (!["hitmos", "torrent"].includes(activeKind) && !player?.getCurrentTime)) return;
    const current = ["hitmos", "torrent"].includes(activeKind) ? audio.currentTime : player.getCurrentTime();
    const total = ["hitmos", "torrent"].includes(activeKind) ? audio.duration : player.getDuration();
    if (Number.isFinite(total) && total > 0) {
      seek.max = String(total);
      seek.value = String(current);
      seek.disabled = ["hitmos", "torrent"].includes(activeKind) && (audio.seekable.length === 0
        || audio.seekable.end(audio.seekable.length - 1) <= audio.seekable.start(0));
      document.querySelector("#player-time").textContent = duration(current);
      document.querySelector("#player-duration").textContent = duration(total);
    }
  }

  async function cue(id, video = null, playWhenReady = false) {
    if (!selected || !consent.checked || !/^[a-zA-Z0-9_-]{11}$/.test(id)) return;
    stop();
    activeKind = "youtube";
    externalVideo.href = `https://www.youtube.com/watch?v=${id}`;
    externalVideo.hidden = false;
    document.querySelector("#player-title").textContent = video?.title || `YouTube · ${id}`;
    document.querySelector("#player-artist").textContent = video?.artist || "Ручной выбор · совпадение не проверено";
    document.querySelector("#player-duration").textContent = "—:—";
    document.querySelector("#player-cover").querySelectorAll("img").forEach(image => image.remove());
    const current = generation;
    playbackState.textContent = "Загрузка YouTube…";
    section.setAttribute("aria-busy", "true");
    message.textContent = "Загружаем видимый плеер YouTube. Воспроизведение начнётся только по вашей команде.";
    section.scrollIntoView({ block: "start", behavior: "instant" });
    try {
      await loadAPI();
      if (current !== generation || !consent.checked) return;
      frameArea.hidden = false;
      const host = document.createElement("div");
      frameArea.replaceChildren(host);
      let expired = false;
      frameTimer = setTimeout(() => {
        if (current === generation && !ready) {
          expired = true;
          fail("YouTube не подтвердил готовность плеера. Проверьте сеть или откройте выбранное видео на YouTube.");
        }
      }, 15000);
      player = new YT.Player(host, {
        width: "100%", height: "100%", videoId: id,
        playerVars: { origin: location.origin, playsinline: 1, controls: 1, autoplay: 0 },
        events: {
          onReady(event) {
            if (current !== generation || expired) return;
            clearTimeout(frameTimer);
            controls(true);
            section.setAttribute("aria-busy", "false");
            event.target.setVolume(Number(volume.value));
            event.target.cueVideoById(id);
            playbackState.textContent = "Готово · YouTube";
            message.textContent = "Нажмите Play в плеере YouTube или в нижней панели. Проверьте выбранное исполнение.";
            polling = setInterval(updateProgress, 500);
            if (playWhenReady) event.target.playVideo();
          },
          onStateChange(event) {
            if (current !== generation || !ready) return;
            const playing = event.data === 1;
            play.textContent = playing ? "Ⅱ" : "▶";
            play.setAttribute("aria-label", playing ? "Пауза видео YouTube" : "Воспроизвести видео YouTube");
            playbackState.textContent = ({ 1: "Играет · YouTube", 2: "Пауза · YouTube", 3: "Буферизация…", 0: "Завершено · YouTube", 5: "Готово · YouTube" })[event.data] || "Подключение…";
            updateProgress();
          },
          onAutoplayBlocked() {
            if (current === generation) message.textContent = "Браузер ожидает действие пользователя. Нажмите Play в видимом плеере YouTube.";
          },
          onError(event) {
            if (current !== generation) return;
            const messages = {
              100: "Видео удалено, закрыто или недоступно.",
              101: "Владелец видео запретил встраивание. Откройте его на YouTube.",
              150: "Владелец видео запретил встраивание. Откройте его на YouTube.",
              153: "YouTube не подтвердил источник запроса (ошибка 153). Проверьте доступ и Referrer браузера или откройте YouTube.",
            };
            fail(messages[event.data] || "YouTube не может воспроизвести это видео. Возможны ограничения сети, региона или возраста.");
          },
        },
      });
    } catch {
      if (current === generation) fail("Не удалось загрузить плеер YouTube. Проверьте сеть или откройте видео на YouTube.");
    }
  }

  function parseVideoId(text) {
    text = text.trim();
    if (/^[a-zA-Z0-9_-]{11}$/.test(text)) return text;
    try {
      const url = new URL(text);
      if (url.protocol !== "https:" || url.username || url.password) return null;
      let id;
      if (url.hostname === "youtu.be") id = url.pathname.slice(1);
      else if (["www.youtube.com", "youtube.com", "m.youtube.com"].includes(url.hostname)) {
        if (url.pathname === "/watch") id = url.searchParams.get("v");
        else if (/^\/(?:shorts|embed)\//.test(url.pathname)) id = url.pathname.split("/")[2];
      }
      return /^[a-zA-Z0-9_-]{11}$/.test(id || "") ? id : null;
    } catch { return null; }
  }

  function validHitmosStream(url, id) {
    if (typeof url !== "string" || !/^[0-9]{1,12}$/.test(id)) return false;
    try {
      const parsed = new URL(url);
      const prefix = ({ "ru.hitmox.org": "/get", "cdn13.deliciousoranges.com": "/s1/get",
        "cdn14.deliciousoranges.com": "/s1/get", "cdn11.deliciousoranges.com": "/s1/get", "s4.deliciouspeaches.com": "/get" })[parsed.hostname];
      return Boolean(prefix) && parsed.protocol === "https:"
        && (!parsed.port || parsed.port === "443") && !parsed.username && !parsed.password && !parsed.search && !parsed.hash
        && new RegExp(`^${prefix}/(?:cuts/[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{32}/${id}/[A-Za-z0-9_-]+|music/[0-9]{8}/[A-Za-z0-9_-]+_${id})\\.mp3$`).test(parsed.pathname);
    } catch { return false; }
  }

  function audioWaiting() {
    if (!["hitmos", "torrent"].includes(activeKind) || (audio.paused && audio.readyState >= 1)) return;
    if (activeKind === "torrent") {
      playbackState.textContent = "Буферизация…";
      section.setAttribute("aria-busy", "true");
      clearTimeout(frameTimer);
      frameTimer = setTimeout(() => {
        if (activeKind === "torrent" && audio.readyState < 3)
          recoverTorrent("waiting_timeout");
      }, 60000);
      return;
    }
    playbackState.textContent = "Загрузка · Hitmos";
    message.textContent = "Этап: загрузка CDN. Браузер ожидает аудиоданные; результат сопоставления не изменился.";
    section.setAttribute("aria-busy", "true");
    clearTimeout(frameTimer);
    frameTimer = setTimeout(() => {
      if (activeKind === "hitmos") fail("Данные не поступили вовремя. Это не ошибка сопоставления. Можно открыть Hitmos.", "cdn_response", "audio_load_timeout");
    }, 15000);
  }

  async function prepareTorrent(fallbackReason = null, originalStartedAt = null) {
    if (!selected || !torrentAutoAvailable) return;
    stop();
    torrentPlayStarted = originalStartedAt ?? performance.now();
    torrentFirstProgress = false;
    logTorrentMilestone("play_clicked");
    if (fallbackReason) console.info("MusicStream Auto source", {
      chosen: "prowlarr", fallback_reason: fallbackReason,
      elapsed_ms: Math.round(performance.now() - torrentPlayStarted),
    });
    activeKind = "torrent";
    const current = generation;
    const track = selected;
    torrentJobId = crypto.randomUUID();
    torrentRequest = new AbortController();
    playbackState.textContent = "Поиск источника…";
    message.textContent = "Ищем доступный источник композиции…";
    section.setAttribute("aria-busy", "true");
    try {
      const response = await fetch("/api/experimental/playback/prepare", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: torrentJobId, id: track.id, title: track.title,
          artist: track.artist, album: track.album, duration_seconds: track.duration_seconds }),
        signal: torrentRequest.signal,
      });
      if (current !== generation) return;
      if (!response.ok) throw new Error("prepare failed");
      const started = await response.json();
      if (started.job_id !== torrentJobId) throw new Error("invalid preparation");
      while (current === generation) {
        const poll = await fetch(`/api/experimental/playback/prepare/${encodeURIComponent(torrentJobId)}`,
          { signal: torrentRequest.signal });
        if (!poll.ok) throw new Error("preparation status failed");
        const result = await poll.json();
        if (current !== generation) return;
        if (result.status === "ready") {
          logTorrentMilestone("stream_ready");
          if (!/^\/api\/experimental\/torrent\/[A-Za-z0-9_-]{1,64}$/.test(result.stream_url || ""))
            throw new Error("invalid stream path");
          audio.hidden = true;
          audio.volume = Number(volume.value) / 100;
          torrentPlaybackId = result.stream_url.split("/").pop();
          torrentStreamUrl = result.stream_url;
          torrentShouldResume = true;
          audio.src = result.stream_url;
          audio.load();
          playbackState.textContent = "Буферизация…";
          message.textContent = "Загружаем аудио…";
          play.disabled = false;
          try { await audio.play(); }
          catch (error) {
            if (current === generation && error.name !== "AbortError") {
              playbackState.textContent = "Готово к воспроизведению";
              message.textContent = "Нажмите Play для запуска аудио.";
            }
          }
          return;
        }
        if (result.status === "unavailable" || result.status === "error") {
          fail(result.status === "error" ? "Ошибка подготовки композиции." : "Композиция сейчас недоступна.");
          playbackState.textContent = "Источник недоступен";
          return;
        }
        playbackState.textContent = result.status === "buffering" ? "Буферизация…" : "Подготовка аудио…";
        await new Promise(resolve => setTimeout(resolve, 200));
      }
    } catch (error) {
      if (current === generation && error.name !== "AbortError") {
        fail("Не удалось подготовить композицию.");
        playbackState.textContent = "Ошибка подготовки";
      }
    }
  }

  function playHitmos(item) {
    if (!validHitmosStream(item.stream_url, item.id)) { fail("Адрес аудио не прошёл проверку браузерного адаптера.", "stream_resolution", "client_url_rejected"); return; }
    stop();
    activeKind = "hitmos";
    const current = generation;
    document.querySelector("#player-title").textContent = item.title || "Запись Hitmos";
    document.querySelector("#player-artist").textContent = item.artist || "Исполнитель неизвестен";
    document.querySelector("#player-duration").textContent = "—:—";
    document.querySelector("#player-cover").querySelectorAll("img").forEach(image => image.remove());
    hitmosLink.href = `https://ru.hitmox.org/song/${item.id}`;
    hitmosLink.hidden = false;
    audio.hidden = false;
    audio.volume = Number(volume.value) / 100;
    audio.src = item.stream_url;
    message.textContent = "Аудио загружается браузером напрямую с Hitmos. Сервер MusicStream не передаёт и не сохраняет музыкальный файл.";
    audioWaiting();
    audio.scrollIntoView({ block: "center", behavior: "instant" });
    audio.play().catch(error => {
      if (current === generation && activeKind === "hitmos") audioFailure(error.name);
    });
  }

  async function prepareAutomaticPlayback() {
    if (!selected) return;
    stop();
    const startedAt = performance.now();
    let current = generation;
    const track = selected;
    automaticHitmos = { startedAt, current, track, timer: null, phase: "search" };
    const fetchHitmos = async (url, timeoutMs, timeoutReason) => {
      const controller = new AbortController();
      request = controller;
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
      try {
        return await fetch(url, { signal: controller.signal });
      } catch (error) {
        if (error.name === "AbortError" && current === generation) throw new Error(timeoutReason);
        throw error;
      } finally {
        clearTimeout(timeout);
        if (request === controller) request = undefined;
      }
    };
    const timing = { hitmos_search_ms: null, hitmos_resolve_ms: null,
      hitmos_first_audio_response_ms: null, fallback_reason: null };
    const fallback = reason => {
      if (current !== generation || !automaticHitmos) return;
      timing.fallback_reason = reason;
      console.info("MusicStream Auto timing", {
        ...timing, chosen: torrentAutoAvailable ? "prowlarr" : "unavailable",
        play_to_fallback_ms: Math.round(performance.now() - startedAt),
      });
      if (torrentAutoAvailable) prepareTorrent(reason, startedAt);
      else {
        automaticHitmos = null;
        fail("Hitmos не подтвердил аудиопоток; резервный Auto Play Prowlarr не включён на сервере.",
          "stream_resolution", reason);
      }
    };
    // The auto resolver only promotes one exact, high-confidence Hitmos match and
    // never enables YouTube. Its matching contract rejects alternate versions.
    const params = new URLSearchParams({ title: track.title, artist: track.artist,
      provider: "auto", allow_youtube: "false" });
    if (typeof track.duration_seconds === "number") params.set("duration_seconds", String(track.duration_seconds));
    if (typeof track.explicit_lyrics === "boolean") params.set("explicit_lyrics", String(track.explicit_lyrics));
    playbackState.textContent = "Проверяем Hitmos…";
    message.textContent = "Сначала проверяем точное совпадение и доступность аудио Hitmos.";
    section.setAttribute("aria-busy", "true");
    let searchStarted = performance.now();
    try {
      const search = await fetchHitmos(`/api/audio/resolve?${params}`, HITMOS_LOOKUP_TIMEOUT_MS, "hitmos_search_timeout");
      timing.hitmos_search_ms = Math.round(performance.now() - searchStarted);
      if (current !== generation) return;
      if (!search.ok) return fallback(`hitmos_search_http_${search.status}`);
      const discovered = await search.json();
      const candidate = discovered.selection === "automatic" && discovered.selected_source === "hitmos"
        ? (discovered.candidates || []).find(item => item.id === discovered.selected_id
          && item.match?.status === "likely" && /^[0-9]{1,12}$/.test(item.id)) : null;
      if (!candidate) {
        const hitmosStatus = (discovered.sources || []).find(item => item.source === "hitmos");
        return fallback(hitmosStatus?.reason || discovered.reason || "hitmos_no_exact_candidate");
      }

      automaticHitmos.phase = "stream_resolution";
      const resolveParams = new URLSearchParams({ title: track.title, artist: track.artist,
        provider: "hitmos", hitmos_id: candidate.id });
      if (typeof track.duration_seconds === "number") resolveParams.set("duration_seconds", String(track.duration_seconds));
      if (typeof track.explicit_lyrics === "boolean") resolveParams.set("explicit_lyrics", String(track.explicit_lyrics));
      const resolveStarted = performance.now();
      const resolved = await fetchHitmos(`/api/audio/resolve?${resolveParams}`, HITMOS_LOOKUP_TIMEOUT_MS, "hitmos_resolve_timeout");
      timing.hitmos_resolve_ms = Math.round(performance.now() - resolveStarted);
      if (current !== generation) return;
      if (!resolved.ok) return fallback(`hitmos_stream_http_${resolved.status}`);
      const prepared = await resolved.json();
      const stream = (prepared.candidates || []).find(item => item.id === candidate.id
        && item.match?.status === "likely" && item.method === "html_audio"
        && validHitmosStream(item.stream_url, item.id));
      if (prepared.status !== "available" || !stream) return fallback(prepared.reason || "hitmos_stream_unavailable");

      automaticHitmos.phase = "audio_response";
      automaticHitmos.timing = timing;
      automaticHitmos.timer = setTimeout(() => fallback("hitmos_audio_start_timeout"), HITMOS_AUDIO_START_TIMEOUT_MS);
      console.info("MusicStream Auto timing", {
        ...timing, chosen: "hitmos_pending_audio", hitmos_url_resolved_ms: Math.round(performance.now() - startedAt),
      });
      playHitmos(stream);
      // playHitmos calls stop() to reset the audio element; restore this generation's
      // playback guard so a changing song still cancels the pending Hitmos attempt.
      current = generation;
      automaticHitmos = { startedAt, current, track, timer: setTimeout(() => {
        fallback("hitmos_audio_start_timeout");
      }, HITMOS_AUDIO_START_TIMEOUT_MS), timing };
    } catch (error) {
      if (current !== generation || error.name === "AbortError") return;
      fallback(["hitmos_search_timeout", "hitmos_resolve_timeout"].includes(error.message)
        ? error.message : "hitmos_request_failed");
    }
  }

  function audioFailure(errorName) {
    if (activeKind === "torrent") {
      if (errorName === "NotAllowedError") {
        torrentShouldResume = false;
        playbackState.textContent = "Готово к воспроизведению";
        message.textContent = "Нажмите Play, чтобы продолжить с текущей позиции.";
        controls(true);
        return;
      }
      const mediaCode = audio.error?.code;
      if (mediaCode === 3) {
        torrentShouldResume = false;
        fail("Браузер не смог декодировать аудиоданные.", "browser_playback", "audio_decode_error");
        return;
      }
      recoverTorrent(mediaCode === 2 ? "http_range_or_network_error"
        : mediaCode === 4 ? "media_source_unavailable_or_range_failed" : "media_error_without_code");
      return;
    }
    if (activeKind !== "hitmos") return;
    if (automaticHitmos) {
      const reason = errorName === "NotAllowedError" ? "browser_play_not_allowed" : "hitmos_audio_error";
      const context = automaticHitmos;
      if (context.timer) clearTimeout(context.timer);
      automaticHitmos = null;
      console.info("MusicStream Auto timing", {
        ...(context.timing || {}), chosen: torrentAutoAvailable ? "prowlarr" : "unavailable",
        fallback_reason: reason,
        play_to_fallback_ms: Math.round(performance.now() - context.startedAt),
      });
      if (torrentAutoAvailable) prepareTorrent(reason, context.startedAt);
      else fail("Hitmos не подтвердил аудиопоток; резервный Auto Play Prowlarr не включён на сервере.",
        "browser_playback", reason);
      return;
    }
    if (errorName === "NotAllowedError") {
      fail("Браузер требует действие пользователя. Нажмите Play в видимом аудиоплеере; совпадение записи не отклонено.", "browser_playback", "play_not_allowed");
      return;
    }
    const code = audio.error?.code;
    if (code === 2) fail("Не удалось загрузить данные с CDN (MediaError 2). HTTP-статус браузером не предоставлен. Откройте исходную запись.", "cdn_response", "audio_network_error");
    else if (code === 3) fail("Браузер не декодировал аудио (MediaError 3). Это не ошибка совпадения записей.", "browser_playback", "audio_decode_error");
    else if (code === 4) fail("Источник или формат не поддерживается (MediaError 4). Возможен неаудиоответ CDN; точный HTTP-ответ не подтверждён. Откройте Hitmos.", "browser_playback", "audio_source_not_supported");
    else fail("Воспроизведение прервано или не запустилось. Причина сопоставления не изменилась; можно открыть Hitmos.", "browser_playback", "audio_play_failed");
  }

  function startTorrentKeepalive() {
    clearInterval(torrentKeepalive);
    if (!torrentPlaybackId) return;
    torrentKeepalive = setInterval(() => {
      if (activeKind !== "torrent" || audio.ended || audio.paused) return;
      fetch(`/api/experimental/torrent/${encodeURIComponent(torrentPlaybackId)}/keepalive`, { method: "POST" })
        .catch(() => console.info("MusicStream torrent keepalive", { result: "unavailable" }));
    }, 10000);
  }

  function releaseTorrentPlayback() {
    const playbackId = torrentPlaybackId;
    torrentPlaybackId = null;
    if (playbackId) {
      fetch(`/api/experimental/torrent/${encodeURIComponent(playbackId)}`, { method: "DELETE" })
        .catch(() => console.info("MusicStream torrent cleanup", { result: "unavailable" }));
    }
  }

  function recoverTorrent(reason) {
    if (activeKind !== "torrent" || !torrentStreamUrl || !torrentShouldResume) return;
    if (torrentRecoveryAttempts >= 5) {
      torrentShouldResume = false;
      fail("Передача не восстановилась после нескольких повторных Range-запросов. Источник или пиры сейчас недоступны.",
        "torrent_recovery", reason);
      return;
    }
    const position = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
    const current = generation;
    const attempt = ++torrentRecoveryAttempts;
    const delayMs = Math.min(8000, 750 * (2 ** (attempt - 1)));
    clearTimeout(frameTimer);
    clearInterval(torrentKeepalive);
    torrentKeepalive = null;
    playbackState.textContent = "Восстанавливаем поток…";
    message.textContent = "Источник временно не передал проверенный фрагмент. Повторяем Range-запрос с текущей позиции.";
    console.info("MusicStream torrent recovery", {
      reason, attempt, delay_ms: delayMs, position_seconds: Math.round(position * 10) / 10,
      media_error_code: audio.error?.code || null,
    });
    frameTimer = setTimeout(() => {
      if (current !== generation || activeKind !== "torrent" || !torrentShouldResume) return;
      const resume = () => {
        if (current !== generation || activeKind !== "torrent" || !torrentShouldResume) return;
        if (position > 0 && Number.isFinite(audio.duration))
          audio.currentTime = Math.min(position, Math.max(0, audio.duration - 0.05));
        audio.play().catch(error => {
          if (current === generation && error.name !== "AbortError") audioFailure(error.name);
        });
      };
      audio.addEventListener("loadedmetadata", resume, { once: true });
      audio.src = torrentStreamUrl;
      audio.load();
      if (audio.readyState >= 1) resume();
    }, delayMs);
  }

  audio.addEventListener("loadedmetadata", () => {
    if (!["hitmos", "torrent"].includes(activeKind)) return;
    controls(true);
    updateProgress();
  });
  audio.addEventListener("playing", () => {
    if (!["hitmos", "torrent"].includes(activeKind) || audio.paused || audio.ended) return;
    if (activeKind === "torrent") {
      logTorrentMilestone("browser_playing");
      torrentRecoveryAttempts = 0;
      torrentShouldResume = true;
      startTorrentKeepalive();
      clearTimeout(frameTimer);
      controls(true);
      play.textContent = "Ⅱ";
      play.setAttribute("aria-label", "Пауза аудио");
      playbackState.textContent = "Воспроизведение";
      message.textContent = "Воспроизведение";
      section.setAttribute("aria-busy", "false");
      updateProgress();
      return;
    }
    clearTimeout(frameTimer);
    if (automaticHitmos && automaticHitmos.current === generation) {
      console.info("MusicStream Auto timing", {
        ...(automaticHitmos.timing || {}), chosen: "hitmos",
        play_to_playing_ms: Math.round(performance.now() - automaticHitmos.startedAt),
      });
      clearTimeout(automaticHitmos.timer);
      automaticHitmos = null;
    }
    controls(true);
    play.textContent = "Ⅱ";
    play.setAttribute("aria-label", "Пауза аудио Hitmos");
    playbackState.textContent = "Играет · Hitmos";
    message.textContent = "Воспроизведение подтверждено браузером. Аудио поступает с CDN напрямую.";
    section.setAttribute("aria-busy", "false");
    updateProgress();
  });
  audio.addEventListener("loadeddata", () => {
    if (!automaticHitmos || automaticHitmos.current !== generation || automaticHitmos.timing?.hitmos_first_audio_response_ms != null) return;
    automaticHitmos.timing.hitmos_first_audio_response_ms = Math.round(performance.now() - automaticHitmos.startedAt);
  });
  audio.addEventListener("pause", () => {
    if (!["hitmos", "torrent"].includes(activeKind) || !ready) return;
    if (activeKind === "torrent") {
      clearInterval(torrentKeepalive);
      torrentKeepalive = null;
      play.textContent = "▶";
      play.setAttribute("aria-label", "Воспроизвести аудио");
      playbackState.textContent = "Пауза";
      section.setAttribute("aria-busy", "false");
      return;
    }
    clearTimeout(frameTimer);
    play.textContent = "▶";
    play.setAttribute("aria-label", "Воспроизвести аудио Hitmos");
    playbackState.textContent = audio.ended ? "Завершено · Hitmos" : "Пауза · Hitmos";
    section.setAttribute("aria-busy", "false");
  });
  audio.addEventListener("timeupdate", updateProgress);
  audio.addEventListener("timeupdate", () => {
    if (activeKind === "torrent" && !torrentFirstProgress && audio.currentTime > 0.2) {
      torrentFirstProgress = true;
      logTorrentMilestone("browser_first_progress");
    }
  });
  audio.addEventListener("ended", () => {
    if (activeKind === "torrent") {
      torrentShouldResume = false;
      clearInterval(torrentKeepalive);
      torrentKeepalive = null;
      clearTimeout(frameTimer);
      releaseTorrentPlayback();
      playbackState.textContent = "Завершено";
      play.textContent = "▶";
      play.setAttribute("aria-label", "Воспроизвести аудио");
      return;
    }
    if (activeKind === "hitmos") {
      playbackState.textContent = "Завершено · Hitmos";
      play.textContent = "▶";
      play.setAttribute("aria-label", "Воспроизвести аудио Hitmos");
    }
  });
  audio.addEventListener("progress", updateProgress);
  audio.addEventListener("waiting", audioWaiting);
  audio.addEventListener("stalled", () => {
    if (activeKind !== "torrent") return;
    console.info("MusicStream torrent media", { event: "stalled", current_time: Math.round(audio.currentTime * 10) / 10 });
    if (!frameTimer && !audio.paused) {
      frameTimer = setTimeout(() => recoverTorrent("stalled_timeout"), 60000);
    }
  });
  audio.addEventListener("error", () => {
    if (["hitmos", "torrent"].includes(activeKind) && audio.error) audioFailure();
  });

  function showCandidates(items, sourceName) {
    candidates.replaceChildren();
    for (const item of items.slice(0, 5)) {
      const candidateSource = item.source || sourceName;
      if (!["hitmos", "youtube"].includes(candidateSource)) continue;
      const isHitmos = candidateSource === "hitmos";
      if (!(isHitmos ? /^[0-9]{1,12}$/ : /^[a-zA-Z0-9_-]{11}$/).test(item.id)) continue;
      const card = document.createElement("div");
      card.className = "source-candidate";
      const title = document.createElement("strong");
      title.textContent = item.title;
      const detail = document.createElement("p");
      const matchText = { likely: "Вероятное совпадение", uncertain: "Совпадение не подтверждено", mismatch: "Другая запись или версия" };
      detail.textContent = `${isHitmos ? "Hitmos" : "YouTube"} · ${item.artist || "исполнитель неизвестен"} · ${duration(item.duration_seconds)} · ${matchText[item.match?.status] || "совпадение не подтверждено"}${typeof item.match_score === "number" ? ` · оценка метаданных ${item.match_score.toFixed(2)} (не вероятность)` : ""}`;
      const open = document.createElement("a");
      open.textContent = isHitmos ? "Слушать на сайте Hitmos ↗" : "Открыть это видео на YouTube ↗";
      open.href = isHitmos ? `https://ru.hitmox.org/song/${item.id}` : `https://www.youtube.com/watch?v=${item.id}`;
      open.target = "_blank";
      open.rel = "noopener noreferrer";
      card.append(title, detail, open);
      const comparison = item.match?.comparison;
      if (comparison) {
        const line = text => { const p = document.createElement("p"); p.textContent = text; card.append(p); };
        const seconds = value => typeof value === "number" ? `${value.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} с (${duration(value)})` : "неизвестно";
        line(`Каталог: ${comparison.reference_title} — ${comparison.reference_artist}; ${seconds(comparison.reference_duration_seconds)}.`);
        line(`Источник: ${comparison.candidate_title} — ${comparison.candidate_artist || "исполнитель неизвестен"}; ${seconds(comparison.candidate_duration_seconds)}.`);
        const delta = comparison.duration_difference_seconds;
        line(`Разница источник − каталог: ${typeof delta === "number" ? `${delta >= 0 ? "+" : ""}${delta.toLocaleString("ru-RU", {maximumFractionDigits: 2})} с` : "неизвестна"}; допуск: ${comparison.duration_tolerance_seconds == null ? "неизвестен" : `${comparison.duration_tolerance_seconds.toLocaleString("ru-RU", {maximumFractionDigits:2})} с`}.`);
      }
      const reasons = { title_differs: "отличается название", artist_unconfirmed: "исполнитель не подтверждён", version_differs: "отличается версия", explicit_unconfirmed: "Explicit-версия не подтверждена источником", duration_unknown: "длительность неизвестна", duration_differs: "превышено отклонение длительности" };
      const rejection = document.createElement("p");
      rejection.textContent = item.match?.reasons?.length ? `Причины: ${item.match.reasons.map(reason => reasons[reason] || "неизвестная проверка").join("; ")}.` : item.match?.status === "likely" ? "Отклонений по сопоставлению нет." : "Подробная причина сопоставления не предоставлена.";
      card.append(rejection);
      if (item.selection_reason) {
        const choice = document.createElement("p");
        choice.textContent = ({ unique_high_confidence: "Выбрана автоматически: единственное точное совпадение метаданных.",
          previously_verified: "Ранее проверенное соответствие метаданных. Источник будет проверен заново по Play.",
          manual_confirmation_required: "Требуется ваш выбор: уверенность недостаточна или есть другие варианты.",
          recording_mismatch: "Отклонена: запись или версия отличается." })[item.selection_reason] || "Требуется проверка записи.";
        card.append(choice);
        if (isHitmos && item.match?.status !== "mismatch") {
          const button = document.createElement("button");
          button.type = "button";
          button.textContent = item.selection_reason === "unique_high_confidence" ? "Play · проверить поток и слушать" : "Выбрать и проверить поток";
          button.addEventListener("click", () => resolveSource(item.id, true, "hitmos"));
          card.append(button);
        }
      }
      if (!isHitmos && item.method === "youtube_iframe" && item.match?.status !== "mismatch") {
        const button = document.createElement("button");
        button.textContent = "Выбрать это видео YouTube";
        button.type = "button";
        button.addEventListener("click", () => resolveSource(item.id, true, "youtube"));
        card.append(button);
      }
      if (isHitmos && item.method === "html_audio" && item.match?.status !== "mismatch" && validHitmosStream(item.stream_url, item.id)) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Попробовать воспроизвести в MusicStream";
        button.addEventListener("click", () => playHitmos(item));
        card.append(button);
      } else if (isHitmos && item.method === "html_audio" && item.match?.status !== "mismatch") {
        const warning = document.createElement("p");
        warning.textContent = "Этап: получение адреса. Формат потока не поддержан браузерным адаптером; это не ошибка сопоставления.";
        card.append(warning);
      }
      candidates.append(card);
    }
  }

  async function resolveSource(candidateId = null, playAfterResolve = false, candidateSource = null) {
    if (typeof candidateId !== "string") candidateId = null;
    const provider = candidateId ? candidateSource || "hitmos" : source.value;
    if (!selected) return;
    if (provider === "youtube" && !consent.checked) {
      message.textContent = "Для подключения YouTube подтвердите согласие выше. Поиск можно открыть отдельно.";
      return;
    }
    stop();
    const current = generation;
    diagnosticsEnabled = false;
    diagnosticPanel.hidden = true;
    candidates.replaceChildren();
    reports.replaceChildren();
    if (!selected.title || !selected.artist || selected.title.length > 200 || selected.artist.length > 200) {
      fail("Недостаточно метаданных для автоматического сравнения. Выберите видео вручную.");
      return;
    }
    request = new AbortController();
    resolveButton.disabled = true;
    playbackState.textContent = "Поиск источника…";
    section.setAttribute("aria-busy", "true");
    message.textContent = "Ищем источник и проверяем совпадение записи…";
    const params = new URLSearchParams({ title: selected.title, artist: selected.artist, provider });
    if (candidateId) params.set(provider === "hitmos" ? "hitmos_id" : "candidate_id", candidateId);
    if (provider === "auto") params.set("allow_youtube", String(consent.checked));
    if (typeof selected.duration_seconds === "number") params.set("duration_seconds", String(selected.duration_seconds));
    if (typeof selected.explicit_lyrics === "boolean") params.set("explicit_lyrics", String(selected.explicit_lyrics));
    try {
      const response = await fetch(`${apiBaseUrl}/api/audio/resolve?${params}`, { signal: request.signal });
      if (!response.ok) {
        const error = new Error("source lookup failed"); error.status = response.status; throw error;
      }
      const result = await response.json();
      if (current !== generation) return;
      message.textContent = `${result.stage ? `Этап: ${stageLabels[result.stage] || "получение источника"}. ` : ""}${result.message || "Источник не сообщил о доступных записях."}`;
      for (const report of Array.isArray(result.sources) ? result.sources : []) {
        if (!["hitmos", "youtube"].includes(report.source)) continue;
        const row = document.createElement("p");
        row.textContent = `${report.source === "hitmos" ? "Hitmos" : "YouTube"}: ${report.message || "Состояние источника неизвестно."}`;
        // Build links from known services; never trust a diagnostic URL.
        const open = document.createElement("a");
        open.href = report.source === "hitmos" ? hitmosSearch.href : externalSearch.href;
        open.textContent = " Открыть сервис ↗";
        open.target = "_blank";
        open.rel = "noopener noreferrer";
        row.append(open);
        reports.append(row);
      }
      diagnosticsEnabled = Boolean(result.diagnostics);
      diagnosticPanel.hidden = !diagnosticsEnabled;
      if (diagnosticsEnabled) {
        const safe = {};
        const fields = ["elapsed_ms", "metadata_status", "player_relative", "player_valid", "redirect_status", "redirect_valid"];
        for (const key of fields) if (["number", "boolean"].includes(typeof result.diagnostics[key])) safe[key] = result.diagnostics[key];
        for (const key of ["player_format", "redirect_format"]) if (["cuts", "music", "missing", "unsupported"].includes(result.diagnostics[key])) safe[key] = result.diagnostics[key];
        if (/^(ru\.hitmox\.org|cdn(?:11|13|14)\.deliciousoranges\.com|s4\.deliciouspeaches\.com|unapproved)$/.test(result.diagnostics.redirect_host || "")) safe.redirect_host = result.diagnostics.redirect_host;
        document.querySelector("#source-diagnostics-data").textContent = JSON.stringify(safe, null, 2);
      }
      playbackState.textContent = result.source === "hitmos" && result.status === "external_player" ? "Плеер на Hitmos" : result.status === "available" ? "Выберите запись" : "Источник недоступен";
      showCandidates(Array.isArray(result.candidates) ? result.candidates : [], result.source);
      if (["hitmos", "youtube", "auto"].includes(result.source)) {
        const items = Array.isArray(result.candidates) ? result.candidates : [];
        const selectedSource = result.selected_source || result.source;
        const chosen = candidateId ? items.find(item => item.id === candidateId && ["html_audio", "youtube_iframe"].includes(item.method) && item.match?.status !== "mismatch")
          : result.selection === "automatic" ? items.find(item => item.id === result.selected_id && (item.source || result.source) === selectedSource && item.match?.status === "likely") : null;
        const chosenSource = chosen?.source || (candidateId ? provider : selectedSource);
        if (chosen && (chosenSource === "hitmos" ? /^[0-9]{1,12}$/ : /^[a-zA-Z0-9_-]{11}$/).test(chosen.id)) {
          if (playAfterResolve && chosenSource === "hitmos" && validHitmosStream(chosen.stream_url, chosen.id)) { playHitmos(chosen); return; }
          if (playAfterResolve && chosenSource === "youtube" && consent.checked) { cue(chosen.id, chosen, true); return; }
          preparedSource = { ...chosen, source: chosenSource };
          play.disabled = false;
          play.setAttribute("aria-label", `Проверить источник и воспроизвести ${chosenSource === "hitmos" ? "Hitmos" : "YouTube"}`);
          playbackState.textContent = "Готово к проверке · Play";
        } else if (result.selection === "choose") playbackState.textContent = "Выберите запись";
      }
    } catch (error) {
      if (current === generation && error.name !== "AbortError") fail(error.status ? `API вернул HTTP ${error.status}${error.status === 429 ? ": лимит запросов" : error.status === 504 ? ": таймаут" : ""}. Это не ошибка сопоставления; можно открыть источник.` : "Не удалось связаться с API. Сопоставление ещё не подтверждено; можно открыть источник.", candidateId ? "stream_resolution" : "source_search", "resolve_request_failed");
    } finally {
      if (current === generation) { resolveButton.disabled = false; section.setAttribute("aria-busy", "false"); }
    }
  }

  document.querySelector("#youtube-manual-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!consent.checked) { message.textContent = "Сначала разрешите подключение YouTube."; return; }
    const id = parseVideoId(document.querySelector("#youtube-url").value);
    if (!id) { message.textContent = "Нужна HTTPS-ссылка youtube.com/watch, youtu.be, shorts или 11-символьный ID видео."; return; }
    cue(id);
  });
  resolveButton.addEventListener("click", resolveSource);
  hitmosForm.addEventListener("submit", event => {
    event.preventDefault();
    const text = document.querySelector("#hitmos-url").value.trim();
    let id = /^[0-9]{1,12}$/.test(text) ? text : null;
    try {
      if (!id) {
        const url = new URL(text);
        if (url.protocol === "https:" && url.hostname === "ru.hitmox.org" && !url.port && !url.username && !url.password && !url.search && !url.hash) {
          id = /^\/song\/([0-9]{1,12})\/?$/.exec(url.pathname)?.[1];
        }
      }
    } catch {}
    if (!id) { message.textContent = "Нужна ссылка https://ru.hitmox.org/song/<число> или числовой ID."; return; }
    hitmosLink.href = `https://ru.hitmox.org/song/${id}`;
    hitmosLink.hidden = false;
    resolveSource(id);
  });
  consent.addEventListener("change", () => {
    sourceChanged();
  });
  function sourceChanged() {
    stop();
    diagnosticsEnabled = false;
    diagnosticPanel.hidden = true;
    resolveButton.disabled = false;
    candidates.replaceChildren();
    reports.replaceChildren();
    if (torrentAutoAvailable && selected && source.value === "auto" && !demoMode) {
      resolveButton.disabled = true;
      hitmosSearch.hidden = true;
      hitmosForm.hidden = true;
      document.querySelector("#hitmos-advanced").hidden = true;
      document.querySelector("#youtube-advanced").hidden = true;
      externalSearch.hidden = true;
      play.disabled = false;
      play.setAttribute("aria-label", "Воспроизвести композицию");
      playbackState.textContent = "Нажмите Play";
      message.textContent = "Нажмите Play.";
      return;
    }
    const isHitmos = source.value === "hitmos";
    const isAuto = source.value === "auto";
    hitmosSearch.hidden = !(isHitmos || isAuto);
    hitmosForm.hidden = !(isHitmos || isAuto);
    document.querySelector("#hitmos-advanced").hidden = !(isHitmos || isAuto);
    document.querySelector(".youtube-consent").hidden = isHitmos;
    document.querySelector("#youtube-manual-form").hidden = isHitmos;
    document.querySelector("#youtube-advanced").hidden = isHitmos;
    externalSearch.hidden = isHitmos;
    playbackState.textContent = isHitmos ? "Выберите запись" : "Выберите источник";
    message.textContent = isHitmos ? "Подбираем запись по метаданным каталога. При неоднозначности потребуется ваш выбор." : "Разрешите подключение YouTube и выберите видео.";
    if ((isHitmos || isAuto || consent.checked) && selected && !demoMode) resolveSource();
  }
  source.addEventListener("change", sourceChanged);
  play.addEventListener("click", () => {
    if (source.value === "auto" && selected
        && activeKind !== "torrent" && activeKind !== "hitmos") {
      prepareAutomaticPlayback();
      return;
    }
    if (preparedSource) { resolveSource(preparedSource.id, true, preparedSource.source); return; }
    if (!ready) return;
    if (activeKind === "hitmos" || activeKind === "torrent") {
      if (!audio.paused) {
        if (activeKind === "torrent") torrentShouldResume = false;
        audio.pause();
      } else {
        if (activeKind === "torrent") torrentShouldResume = true;
        audio.play().catch(error => audioFailure(error.name));
      }
      return;
    }
    if (!player) return;
    if (player.getPlayerState() === 1) player.pauseVideo();
    else { frameArea.scrollIntoView({ block: "center", behavior: "instant" }); player.playVideo(); }
  });
  seek.addEventListener("change", () => {
    if (!ready) return;
    if (activeKind === "hitmos" || activeKind === "torrent") {
      if (audio.seekable.length && audio.seekable.end(audio.seekable.length - 1) > audio.seekable.start(0)) {
        audio.currentTime = Math.max(audio.seekable.start(0), Math.min(Number(seek.value), audio.seekable.end(audio.seekable.length - 1)));
      }
    }
    else player.seekTo(Number(seek.value), true);
  });
  volume.addEventListener("input", () => {
    if (!ready) return;
    if (activeKind === "hitmos" || activeKind === "torrent") { audio.muted = false; audio.volume = Number(volume.value) / 100; }
    else player.setVolume(Number(volume.value));
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden && ready && activeKind === "youtube") player.pauseVideo(); });
  new IntersectionObserver(entries => {
    if (!entries[0].isIntersecting && ready && player?.getPlayerState() === 1) player.pauseVideo();
  }).observe(frameArea);

  if (location.hostname === "127.0.0.1" || location.hostname === "localhost") {
    fetch("/api/experimental/playback/capabilities")
      .then(response => response.ok ? response.json() : null)
      .then(result => {
        torrentAutoAvailable = result?.available === true;
        if (torrentAutoAvailable && selected && source.value === "auto") sourceChanged();
      }).catch(() => {});
  }

  return {
    select(track) {
      stop();
      selected = track;
      section.hidden = false;
      candidates.replaceChildren();
      resolveButton.disabled = false;
      document.querySelector("#youtube-url").value = "";
      externalSearch.href = "https://www.youtube.com/results?" + new URLSearchParams({ search_query: `${track.artist || ""} ${track.title || ""}` });
      hitmosSearch.href = "https://ru.hitmox.org/search?" + new URLSearchParams({ q: `${track.artist || ""} ${track.title || ""}` });
      hitmosLink.hidden = true;
      document.querySelector("#hitmos-url").value = "";
      playbackState.textContent = "Выберите источник";
      sourceChanged();
      section.scrollIntoView({ block: "start", behavior: "instant" });
    },
  };
})();
