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
const artistSearchResults = document.querySelector("#artist-search-results");
const artistSearchList = document.querySelector("#artist-search-list");
const catalogView = document.querySelector("#catalog-view");
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
let selectedTrackId = null;
const trackPlaybackStates = new Map();

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
  artistSearchList.replaceChildren();
  artistSearchResults.hidden = true;
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

function setTilePlaybackState(trackId, playbackState) {
  if (trackId == null) return;
  const id = String(trackId);
  if (playbackState === "idle") trackPlaybackStates.delete(id);
  else trackPlaybackStates.set(id, playbackState);
  document.querySelectorAll(".track-row").forEach(row => {
    if (row.dataset.trackId !== id) return;
    row.classList.toggle("is-loading", playbackState === "loading");
    row.classList.toggle("is-playing", playbackState === "playing");
    row.classList.toggle("is-paused", playbackState === "paused");
    row.classList.toggle("has-error", playbackState === "error");
    row.querySelector(".track-playback-indicator").textContent = ({
      loading: "Загрузка", playing: "Играет", paused: "Пауза", error: "Ошибка",
    })[playbackState] || "";
  });
  if (id === selectedTrackId) {
    const labels = {
      loading: "Подбираем источник и запускаем воспроизведение",
      playing: "Сейчас играет",
      paused: "Воспроизведение приостановлено",
      error: "Не удалось начать воспроизведение. Откройте раздел «Диагностика» для подробностей",
    };
    if (labels[playbackState]) status.textContent = labels[playbackState];
  }
}

function selectTrack(track, row) {
  const trackId = String(track.id);
  if (!musicPlayer.select(track)) return;
  if (selectedTrackId !== trackId) setTilePlaybackState(selectedTrackId, "idle");
  selectedTrackId = trackId;
  document.querySelectorAll(".track-row").forEach(item => {
    const selected = item === row;
    item.classList.toggle("selected", selected);
    item.querySelector(".track-tile").setAttribute("aria-pressed", String(selected));
  });
  setTilePlaybackState(trackId, "loading");
  document.querySelector("#player-title").textContent = track.title || "Без названия";
  document.querySelector("#player-artist").textContent = track.artist || "Исполнитель неизвестен";
  document.querySelector("#player-duration").textContent = duration(track.duration_seconds);
  addCover(document.querySelector("#player-cover"), track.cover_url);
  status.textContent = `Выбрана композиция ${track.title || "без названия"}. Ищем источник воспроизведения.`;
}

function render(tracks) {
  const fragment = document.createDocumentFragment();
  tracks.forEach((track, index) => {
    const row = template.content.firstElementChild.cloneNode(true);
    const tile = row.querySelector(".track-tile");
    const id = String(track.id);
    const title = track.title || "\u0411\u0435\u0437 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u044f";
    const artist = track.artist || "\u0418\u0441\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c \u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u0435\u043d";
    row.dataset.trackId = id;
    row.querySelector(".track-number").textContent = String(index + 1).padStart(2, "0");
    row.querySelector(".track-title").textContent = title;
    row.querySelector(".track-title").title = title;
    row.querySelector(".track-artist").textContent = artist;
    const album = row.querySelector(".track-album");
    album.textContent = track.album || "\u0410\u043b\u044c\u0431\u043e\u043c \u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u0435\u043d";
    album.title = album.textContent;
    row.querySelector(".track-duration").textContent = duration(track.duration_seconds);
    tile.setAttribute("aria-label", `\u0412\u043e\u0441\u043f\u0440\u043e\u0438\u0437\u0432\u0435\u0441\u0442\u0438 ${title} \u2014 ${artist}, ${duration(track.duration_seconds)}`);
    tile.addEventListener("click", () => selectTrack(track, row));
    addCover(row.querySelector(".track-cover"), track.cover_url);
    if (id === selectedTrackId) {
      row.classList.add("selected");
      tile.setAttribute("aria-pressed", "true");
      const playbackState = trackPlaybackStates.get(id);
      if (playbackState) {
        row.classList.add(playbackState === "error" ? "has-error" : `is-${playbackState}`);
        row.querySelector(".track-playback-indicator").textContent = ({
          loading: "\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430", playing: "\u0418\u0433\u0440\u0430\u0435\u0442", paused: "\u041f\u0430\u0443\u0437\u0430", error: "\u041e\u0448\u0438\u0431\u043a\u0430",
        })[playbackState] || "";
      }
    }
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
  if (!catalogView.hidden) showSearchResults();
  cancelSearch();
  artistSearchList.replaceChildren();
  artistSearchResults.hidden = true;
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
  const artistQuery = query.replace(/\s+/g, " ");
  controller = new AbortController();
  count.textContent = "Ищем в каталоге Deezer";
  setState("Ищем вашу музыку…", "Ещё немного — собираем результаты.", { loading: true });
  try {
    const params = new URLSearchParams({ q: query, limit: "25" });
    const [response, artistsResponse] = await Promise.all([
      fetch(`${apiBaseUrl}/api/search?${params}`, { signal: controller.signal }),
      fetch(`${apiBaseUrl}/api/catalog/artists?${new URLSearchParams({ q: artistQuery, limit: "25" })}`, { signal: controller.signal }).catch(() => null),
    ]);
    if (current !== revision) return;
    let artistResults = [];
    if (artistsResponse?.ok) {
      try {
        const artistPayload = await artistsResponse.json();
        artistResults = Array.isArray(artistPayload?.data) ? artistPayload.data : [];
      } catch {
        // A failed artist lookup should not prevent the existing track search.
      }
    }
    if (!response.ok) {
      const messages = {
        422: "Проверьте поисковый запрос и попробуйте снова.",
        503: "Музыкальный каталог временно недоступен. Попробуйте немного позже.",
        504: "Поиск занял слишком много времени. Попробуйте ещё раз.",
      };
      throw new SearchError(messages[response.status] || "Не удалось загрузить результаты. Попробуйте ещё раз.");
    }
    const tracks = await response.json();
    renderArtistSearch(artistResults);
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
    const exactArtist = findExactArtist(artistResults, artistQuery);
    if (exactArtist) openArtist(exactArtist.id, true);
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

function normalizeArtistName(value) {
  return String(value || "").normalize("NFC").toLocaleLowerCase().trim().replace(/\s+/g, " ");
}

function findExactArtist(artists, query) {
  const normalizedQuery = normalizeArtistName(query);
  const exact = artists.filter(artist => normalizeArtistName(artist.name) === normalizedQuery);
  if (!exact.length) return null;
  if (exact.length === 1) return exact[0];
  const ranked = [...exact].sort((left, right) => (right.fan_count || 0) - (left.fan_count || 0));
  const leadingFans = ranked[0].fan_count || 0;
  const runnerUpFans = ranked[1].fan_count || 0;
  // Deezer may return duplicate artist profiles with exactly the same name.
  // Auto-open only when one profile is clearly canonical by its fan count.
  return leadingFans >= 100 && leadingFans >= Math.max(1, runnerUpFans) * 10
    ? ranked[0]
    : null;
}

input.addEventListener("input", () => {
  if (!catalogView.hidden) showSearchResults();
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

function renderArtistSearch(artists) {
  artistSearchList.replaceChildren();
  artistSearchResults.hidden = !artists.length;
  artists.slice(0, 10).forEach(artist => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "artist-result";
    const cover = makeCatalogCover(artist.picture_url);
    const label = document.createElement("span");
    label.textContent = artist.name;
    button.append(cover, label);
    button.addEventListener("click", () => openArtist(artist.id, true));
    artistSearchList.append(button);
  });
}

let catalogController;
let catalogRevision = 0;
let artistCatalog;
let catalogRoute = "search";
const catalogPageSize = 50;

function stopArtistLoads(state) {
  if (!state) return;
  state.releaseObserver?.disconnect();
  state.releaseObserver = null;
  state.pendingReleasePage = null;
  state.loadingReleases = false;
  state.loadingTracks = false;
}

async function catalogFetch(path, signal) {
  for (let attempt = 0; attempt < 3; attempt++) {
    let response;
    try {
      response = await fetch(`${apiBaseUrl}${path}`, { signal });
    } catch (error) {
      if (error.name === "AbortError" || attempt === 2) throw error;
      await waitForCatalogRetry(350 * (attempt + 1), signal);
      continue;
    }
    if (response.ok) return response.json();
    if ([429, 502, 503, 504].includes(response.status) && attempt < 2) {
      const retryAfter = Number(response.headers.get("Retry-After"));
      const delay = Number.isFinite(retryAfter) && retryAfter > 0
        ? Math.min(10000, retryAfter * 1000) : 350 * (attempt + 1);
      await waitForCatalogRetry(delay, signal);
      continue;
    }
    const error = new Error(response.status === 429
      ? "Deezer temporarily limited request frequency. Wait and retry."
      : `Deezer catalog unavailable (HTTP ${response.status}).`);
    error.status = response.status;
    throw error;
  }
  throw new Error("Deezer catalog temporarily unavailable.");
}

function waitForCatalogRetry(delay, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    let timer;
    const finish = () => {
      signal?.removeEventListener("abort", abort);
      resolve();
    };
    const abort = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reject(new DOMException("Aborted", "AbortError"));
    };
    timer = setTimeout(finish, delay);
    signal?.addEventListener("abort", abort, { once: true });
  });
}

function catalogShell(title = "Загружаем каталог Deezer…") {
  catalogView.replaceChildren();
  const back = document.createElement("button");
  back.type = "button";
  back.className = "catalog-back";
  back.textContent = "← К результатам поиска";
  back.addEventListener("click", showSearchResults);
  const heading = document.createElement("h1");
  heading.textContent = title;
  catalogView.append(back, heading);
}

function showSearchResults(pushState = true) {
  if (catalogRoute === "artist" && artistCatalog) artistCatalog.scrollY = window.scrollY;
  catalogController?.abort();
  stopArtistLoads(artistCatalog);
  catalogRevision++;
  catalogRoute = "search";
  catalogView.hidden = true;
  document.querySelector(".results-section").hidden = false;
  document.querySelector(".topbar .breadcrumb strong").textContent = "Поиск";
  if (pushState) history.pushState({}, "", "#search");
}

function formatYear(date) {
  return typeof date === "string" && /^\d{4}/.test(date) ? date.slice(0, 4) : "";
}

function catalogSection(title, note = "") {
  const section = document.createElement("section");
  section.className = "catalog-section";
  const heading = document.createElement("div");
  heading.className = "section-heading";
  const h2 = document.createElement("h2");
  h2.textContent = title;
  heading.append(h2);
  if (note) {
    const small = document.createElement("span");
    small.className = "result-count";
    small.textContent = note;
    heading.append(small);
  }
  section.append(heading);
  return section;
}

function makeReleaseCard(release) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "release-card";
  button.dataset.releaseId = String(release.id || "");
  const cover = makeCatalogCover(release.cover_url);
  const name = document.createElement("strong");
  name.textContent = release.title || "Без названия";
  const meta = document.createElement("small");
  meta.textContent = [release.type, formatYear(release.release_date), release.track_count ? `${release.track_count} треков` : ""].filter(Boolean).join(" · ");
  button.append(cover, name, meta);
  button.addEventListener("click", () => openAlbum(release.id, true));
  return button;
}

function appendCatalogTracks(target, tracks, offset = 0, ranking = false) {
  const fragment = document.createDocumentFragment();
  tracks.forEach((track, i) => {
    const row = template.content.firstElementChild.cloneNode(true);
    const tile = row.querySelector(".track-tile");
    const id = String(track.id);
    row.dataset.trackId = id;
    row.querySelector(".track-number").textContent = String(ranking ? offset + i + 1 : track.track_number || offset + i + 1).padStart(2, "0");
    row.querySelector(".track-title").textContent = track.title || "Без названия";
    row.querySelector(".track-artist").textContent = track.artist || "Исполнитель неизвестен";
    row.querySelector(".track-album").textContent = track.album || "";
    row.querySelector(".track-duration").textContent = duration(track.duration_seconds);
    tile.setAttribute("aria-label", `Воспроизвести ${track.title || "без названия"} — ${track.artist || "исполнитель неизвестен"}, ${duration(track.duration_seconds)}`);
    tile.addEventListener("click", () => selectTrack(track, row));
    addCover(row.querySelector(".track-cover"), track.cover_url);
    if (id === selectedTrackId) {
      row.classList.add("selected");
      tile.setAttribute("aria-pressed", "true");
      const playbackState = trackPlaybackStates.get(id);
      if (playbackState) {
        row.classList.add(playbackState === "error" ? "has-error" : `is-${playbackState}`);
        row.querySelector(".track-playback-indicator").textContent = ({ loading: "Загрузка", playing: "Играет", paused: "Пауза", error: "Ошибка" })[playbackState] || "";
      }
    }
    fragment.append(row);
  });
  target.append(fragment);
}

function makeCatalogCover(url) {
  const cover = document.createElement("span");
  cover.className = "cover";
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.classList.add("icon");
  icon.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-note");
  icon.append(use);
  cover.append(icon);
  addCover(cover, url);
  return cover;
}

async function openArtist(id, pushState = false) {
  if (!/^[0-9]{1,20}$/.test(String(id))) return;
  if (artistCatalog?.id === String(id) && catalogRoute !== "artist") {
    const keepTrackLoad = catalogRoute === "tracks" && artistCatalog.loadingTracks;
    if (!keepTrackLoad) {
      catalogController?.abort();
      stopArtistLoads(artistCatalog);
      catalogController = new AbortController();
      catalogRevision++;
    }
    document.querySelector(".results-section").hidden = true;
    catalogView.hidden = false;
    document.querySelector(".topbar .breadcrumb strong").textContent = "Исполнитель";
    catalogRoute = "artist";
    if (pushState) history.pushState({}, "", `#artist/${id}`);
    renderArtistPagePreservingScroll();
    window.scrollTo(0, artistCatalog.scrollY || 0);
    return;
  }
  catalogController?.abort();
  stopArtistLoads(artistCatalog);
  catalogController = new AbortController();
  const signal = catalogController.signal;
  const current = ++catalogRevision;
  document.querySelector(".results-section").hidden = true;
  catalogView.hidden = false;
  document.querySelector(".topbar .breadcrumb strong").textContent = "Исполнитель";
  catalogRoute = "artist";
  if (pushState) history.pushState({}, "", `#artist/${id}`);
  catalogShell();
  try {
    const [artist, top, page] = await Promise.all([
      catalogFetch(`/api/catalog/artists/${id}`, signal),
      catalogFetch(`/api/catalog/artists/${id}/top?limit=10`, signal),
      catalogFetch(`/api/catalog/artists/${id}/releases?index=0&limit=${catalogPageSize}`, signal),
    ]);
    if (current !== catalogRevision) return;
    const initialReleasePage = page.data || [];
    const initialReleases = uniqueReleases(initialReleasePage);
    const artistTop = Array.isArray(top) ? top : [];
    const rankedTop = artistTop.filter(track => Number.isSafeInteger(track.rank) && track.rank >= 0)
      .sort((left, right) => right.rank - left.rank);
    const unrankedTop = artistTop.filter(track => !Number.isSafeInteger(track.rank) || track.rank < 0);
    artistCatalog = { id: String(id), artist, top: [...rankedTop, ...unrankedTop].slice(0, 10), releases: initialReleases, releaseIds: new Set(initialReleases.map(item => String(item.id))), releaseOffset: initialReleasePage.length, hasMoreReleases: Boolean(page.has_more), releaseError: "", loadingReleases: false, pendingReleasePage: null, releaseObserver: null, releaseLoadButton: null, releaseGrids: null, albumSection: null, singleSection: null, otherSection: null, releaseActions: null, releaseEmptyNotes: {}, releaseErrorElement: null, loadedTrackAlbums: 0, tracks: [], trackIds: new Set(), trackFingerprints: new Map(), trackPage: 0, trackError: "", allTracksDone: false, loadingTracks: false, scrollY: 0, tracksScrollY: 0 };
    renderArtistPage();
  } catch (error) {
    if (error.name === "AbortError" || current !== catalogRevision) return;
    catalogShell("Не удалось открыть каталог");
    const message = document.createElement("p");
    message.className = "catalog-error";
    message.textContent = error.message;
    catalogView.append(message);
  }
}

function renderArtistPage() {
  const state = artistCatalog;
  if (!state) return;
  state.releaseObserver?.disconnect();
  state.releaseObserver = null;
  state.releaseLoadButton = null;
  catalogView.replaceChildren();
  const back = document.createElement("button");
  back.type = "button";
  back.className = "catalog-back";
  back.textContent = "← К результатам поиска";
  back.addEventListener("click", showSearchResults);
  const header = document.createElement("header");
  header.className = "artist-header";
  const cover = makeCatalogCover(state.artist.picture_url);
  const heading = document.createElement("div");
  heading.className = "catalog-heading";
  const h1 = document.createElement("h1");
  h1.textContent = state.artist.name;
  const summary = document.createElement("p");
  summary.textContent = `${state.artist.album_count ?? "Доступные"} релизов в каталоге Deezer · ID ${state.artist.id}`;
  heading.append(h1, summary);
  header.append(cover, heading);
  catalogView.append(back, header);

  const topSection = catalogSection("Популярные композиции", "Популярное Deezer");
  const topList = document.createElement("ol");
  topList.className = "track-list catalog-track-list";
  appendCatalogTracks(topList, state.top, 0, true);
  topSection.append(topList);
  const allTracksButton = document.createElement("button");
  allTracksButton.type = "button";
  allTracksButton.className = "catalog-more catalog-track-link";
  allTracksButton.textContent = "\u0412\u0441\u0435 \u0442\u0440\u0435\u043a\u0438";
  allTracksButton.addEventListener("click", () => openArtistTracks(true));
  topSection.append(allTracksButton);
  catalogView.append(topSection);

  const albums = state.releases.filter(item => ["album", "live", "compile"].includes(String(item.type || "").toLowerCase()));
  const eps = state.releases.filter(item => ["single", "ep"].includes(String(item.type || "").toLowerCase()));
  const other = state.releases.filter(item => !albums.includes(item) && !eps.includes(item));
  const albumSection = catalogSection("Альбомы и концертные релизы", "Постраничный каталог");
  const albumGrid = document.createElement("div"); albumGrid.className = "release-grid";
  albums.forEach(item => albumGrid.append(makeReleaseCard(item)));
  albumSection.append(albumGrid);
  const singleSection = catalogSection("Синглы и EP");
  const singleGrid = document.createElement("div"); singleGrid.className = "release-grid";
  eps.forEach(item => singleGrid.append(makeReleaseCard(item)));
  singleSection.append(singleGrid);
  state.releaseGrids = { albums: albumGrid, singles: singleGrid, other: null };
  state.albumSection = albumSection;
  state.singleSection = singleSection;
  state.otherSection = null;
  state.releaseActions = document.createElement("div"); state.releaseActions.className = "catalog-actions";
  state.releaseEmptyNotes = {};
  state.releaseErrorElement = null;
  if (other.length) {
    const otherSection = catalogSection("Другие доступные релизы");
    const otherGrid = document.createElement("div"); otherGrid.className = "release-grid";
    other.forEach(item => otherGrid.append(makeReleaseCard(item)));
    otherSection.append(otherGrid);
    catalogView.append(otherSection);
    state.otherSection = otherSection;
    state.releaseGrids.other = otherGrid;
  }
  if (state.releaseError) {
    state.releaseErrorElement = document.createElement("p"); state.releaseErrorElement.className = "catalog-error"; state.releaseErrorElement.textContent = state.releaseError; albumSection.append(state.releaseErrorElement);
  }
  albumSection.append(state.releaseActions);
  catalogView.append(albumSection, singleSection);
  syncReleaseEmptyStates(state);

  syncReleaseControls(state);
}

function openArtistTracks(pushState = true) {
  const state = artistCatalog;
  if (!state) return;
  if (catalogRoute === "artist") state.scrollY = window.scrollY;
  state.releaseObserver?.disconnect();
  state.releaseObserver = null;
  catalogRoute = "tracks";
  if (pushState) history.pushState({}, "", `#artist/${state.id}/tracks`);
  renderArtistTracksPage();
  window.scrollTo(0, 0);
  if (!state.allTracksDone && !state.loadingTracks) loadAllArtistTracks();
}

function renderArtistTracksPage() {
  const state = artistCatalog;
  if (!state || catalogRoute !== "tracks") return;
  catalogView.replaceChildren();
  const back = document.createElement("button");
  back.type = "button";
  back.className = "catalog-back";
  back.textContent = "\u2190 \u041a \u0438 \u0441\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044e";
  back.addEventListener("click", () => openArtist(state.id, true));
  const section = catalogSection("\u0412\u0441\u0435 \u0442\u0440\u0435\u043a\u0438", `${state.artist.name} \u00b7 Deezer`);
  const status = document.createElement("p");
  status.id = "artist-track-load-status";
  status.className = "catalog-note";
  if (state.loadingTracks) {
    status.textContent = `\u0421\u043e\u0431\u0438\u0440\u0430\u0435\u043c \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0435 \u0440\u0435\u043b\u0438\u0437\u044b \u0438 \u0442\u0440\u0435\u043a\u0438 (${state.tracks.length} \u0437\u0430\u0433\u0440\u0443\u0436\u0435\u043d\u043e). \u0420\u0435\u0439\u0442\u0438\u043d\u0433 \u043f\u043e\u044f\u0432\u0438\u0442\u0441\u044f \u043f\u043e\u0441\u043b\u0435 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u044f \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0438.`;
    section.append(status);
    if (state.trackError) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "catalog-more";
      retry.textContent = "\u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0443";
      retry.addEventListener("click", () => loadAllArtistTracks());
      section.append(retry);
      const error = document.createElement("p");
      error.className = "catalog-error";
      error.textContent = state.trackError;
      section.append(error);
    }
  } else if (state.trackError) {
    status.textContent = state.trackError;
    status.classList.add("catalog-error");
    section.append(status);
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "catalog-more";
    retry.textContent = "\u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0443";
    retry.addEventListener("click", () => loadAllArtistTracks());
    section.append(retry);
  } else if (!state.allTracksDone) {
    status.textContent = "\u041f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043a\u0430 \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0430\u2026";
    section.append(status);
  } else {
    const ranked = state.rankedTracks || [];
    const unranked = state.unrankedTracks || [];
    status.textContent = `${ranked.length} \u0442\u0440\u0435\u043a\u043e\u0432 \u0441 Deezer rank \u00b7 ${unranked.length} \u0431\u0435\u0437 \u043f\u043e\u043a\u0430\u0437\u0430\u0442\u0435\u043b\u044f rank`;
    section.append(status);
    if (ranked.length) {
      const rankedHeading = document.createElement("h3");
      rankedHeading.className = "catalog-list-heading";
      rankedHeading.textContent = "\u0420\u0435\u0439\u0442\u0438\u043d\u0433 \u043f\u043e \u043f\u043e\u043f\u0443\u043b\u044f\u0440\u043d\u043e\u0441\u0442\u0438 Deezer";
      const list = document.createElement("ol");
      list.className = "track-list catalog-track-list";
      const pageSize = 60;
      const pageCount = Math.max(1, Math.ceil(ranked.length / pageSize));
      state.rankedPage = Math.min(state.rankedPage || 0, pageCount - 1);
      appendCatalogTracks(list, ranked.slice(state.rankedPage * pageSize, (state.rankedPage + 1) * pageSize), state.rankedPage * pageSize, true);
      section.append(rankedHeading, list);
      appendTrackPageControls(section, state, "rankedPage", ranked.length, pageSize);
    }
    if (unranked.length) {
      const unrankedHeading = document.createElement("h3");
      unrankedHeading.className = "catalog-list-heading";
      unrankedHeading.textContent = "\u0411\u0435\u0437 \u043f\u043e\u043a\u0430\u0437\u0430\u0442\u0435\u043b\u044f \u043f\u043e\u043f\u0443\u043b\u044f\u0440\u043d\u043e\u0441\u0442\u0438 Deezer";
      const unrankedNote = document.createElement("p");
      unrankedNote.className = "catalog-note";
      unrankedNote.textContent = "Deezer \u043d\u0435 \u0432\u0435\u0440\u043d\u0443\u043b rank \u0434\u043b\u044f \u044d\u0442\u0438\u0445 \u043a\u043e\u043c\u043f\u043e\u0437\u0438\u0446\u0438\u0439; \u043e\u043d\u0438 \u043f\u043e\u043a\u0430\u0437\u0430\u043d\u044b \u043e\u0442\u0434\u0435\u043b\u044c\u043d\u043e \u0431\u0435\u0437 \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u043e\u0432\u043e\u0433\u043e \u043f\u043e\u0440\u044f\u0434\u043a\u0430.";
      const list = document.createElement("ol");
      list.className = "track-list catalog-track-list";
      const pageSize = 60;
      const pageCount = Math.max(1, Math.ceil(unranked.length / pageSize));
      state.unrankedPage = Math.min(state.unrankedPage || 0, pageCount - 1);
      const start = state.unrankedPage * pageSize;
      appendCatalogTracks(list, unranked.slice(start, start + pageSize), start, true);
      list.querySelectorAll(".track-number").forEach(number => { number.textContent = "\u2014"; });
      section.append(unrankedHeading, unrankedNote, list);
      appendTrackPageControls(section, state, "unrankedPage", unranked.length, pageSize);
    }
    if (!ranked.length && !unranked.length) {
      const empty = document.createElement("p");
      empty.className = "catalog-note";
      empty.textContent = "\u0412 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0445 \u0440\u0435\u043b\u0438\u0437\u0430\u0445 Deezer \u0442\u0440\u0435\u043a\u0438 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u044b.";
      section.append(empty);
    }
  }
  catalogView.append(back, section);
}

function appendTrackPageControls(section, state, key, total, pageSize) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const page = state[key] || 0;
  if (pageCount <= 1) return;
  const actions = document.createElement("div");
  actions.className = "catalog-actions";
  if (page > 0) {
    const previous = document.createElement("button");
    previous.type = "button";
    previous.className = "catalog-more";
    previous.textContent = "\u041f\u0440\u0435\u0434\u044b\u0434\u0443\u0449\u0438\u0435 \u0442\u0440\u0435\u043a\u0438";
    previous.addEventListener("click", () => { state[key]--; renderArtistTracksPage(); window.scrollTo(0, 0); });
    actions.append(previous);
  }
  if (page + 1 < pageCount) {
    const next = document.createElement("button");
    next.type = "button";
    next.className = "catalog-more";
    next.textContent = "\u0421\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0435 \u0442\u0440\u0435\u043a\u0438";
    next.addEventListener("click", () => { state[key]++; renderArtistTracksPage(); window.scrollTo(0, 0); });
    actions.append(next);
  }
  const label = document.createElement("span");
  label.className = "catalog-note";
  label.textContent = `\u0421\u0442\u0440\u0430\u043d\u0438\u0446\u0430 ${page + 1} \u0438\u0437 ${pageCount}`;
  actions.append(label);
  section.append(actions);
}

async function loadAllArtistTracks() {
  const state = artistCatalog;
  const controller = catalogController;
  if (!state || !controller || state.loadingTracks || state.allTracksDone) return;
  const signal = controller.signal;
  state.loadingTracks = true;
  state.trackError = "";
  renderArtistTracksPage();
  try {
    while (!signal.aborted && controller === catalogController) {
      if (state.loadedTrackAlbums >= state.releases.length) {
        if (!state.hasMoreReleases) break;
        const page = await requestNextReleasePage(state, signal);
        if (!page && !state.hasMoreReleases) break;
        continue;
      }
      const batchEnd = Math.min(state.loadedTrackAlbums + 2, state.releases.length);
      const albums = state.releases.slice(state.loadedTrackAlbums, batchEnd);
      const pages = await Promise.all(albums.map(release => loadReleaseTracks(release.id, signal)));
      if (signal.aborted || controller !== catalogController || state !== artistCatalog) return;
      pages.forEach((page, index) => (page.data || []).forEach(track => {
        if (!track.album) track.album = albums[index].title;
        if (!track.cover_url) track.cover_url = albums[index].cover_url;
        if (!track.artist) track.artist = state.artist.name;
        if (!isCatalogDuplicate(state, track)) state.tracks.push(track);
      }));
      state.loadedTrackAlbums = batchEnd;
      const progress = document.querySelector("#artist-track-load-status");
      if (progress) progress.textContent = `\u0417\u0430\u0433\u0440\u0443\u0436\u0435\u043d\u043e ${state.tracks.length} \u0442\u0440\u0435\u043a\u043e\u0432 \u0438\u0437 ${state.releases.length} \u0440\u0435\u043b\u0438\u0437\u043e\u0432; \u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u043c \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0435 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u044b \u043a\u0430\u0442\u0430\u043b\u043e\u0433\u0430\u2026`;
    }
    if (signal.aborted || controller !== catalogController || state !== artistCatalog) return;
    state.allTracksDone = state.loadedTrackAlbums >= state.releases.length && !state.hasMoreReleases;
    if (state.allTracksDone) {
      state.rankedTracks = state.tracks.filter(track => Number.isSafeInteger(track.rank) && track.rank >= 0)
        .sort((left, right) => right.rank - left.rank);
      state.unrankedTracks = state.tracks.filter(track => !Number.isSafeInteger(track.rank) || track.rank < 0);
    }
  } catch (error) {
    if (error.name !== "AbortError" && controller === catalogController) state.trackError = error.message;
  } finally {
    if (controller === catalogController && state === artistCatalog) {
      state.loadingTracks = false;
      if (catalogRoute === "tracks") renderArtistTracksPage();
    }
  }
}

async function loadMoreReleases() {
  const state = artistCatalog;
  const signal = catalogController?.signal;
  if (!state?.hasMoreReleases || !signal) return;
  try {
    await requestNextReleasePage(state, signal);
  } catch (error) {
    if (error.name !== "AbortError") status.textContent = error.message;
  }
}

function uniqueReleases(releases) {
  const ids = new Set();
  return releases.filter(item => {
    const id = String(item?.id || "");
    if (!id || ids.has(id)) return false;
    ids.add(id);
    return true;
  });
}

function releaseGroup(release) {
  const type = String(release.type || "").toLowerCase();
  if (["album", "live", "compile"].includes(type)) return "albums";
  if (["single", "ep"].includes(type)) return "singles";
  return "other";
}

function appendReleaseItems(state, releases) {
  if (state !== artistCatalog || catalogView.hidden) return;
  releases.forEach(release => {
    const id = String(release?.id || "");
    if (!id || state.releaseIds.has(id)) return;
    state.releaseIds.add(id);
    state.releases.push(release);
    const group = releaseGroup(release);
    if (group === "other" && !state.releaseGrids.other) {
      state.otherSection = catalogSection("Другие доступные релизы");
      state.releaseGrids.other = document.createElement("div");
      state.releaseGrids.other.className = "release-grid";
      state.otherSection.append(state.releaseGrids.other);
      catalogView.insertBefore(state.otherSection, state.albumSection);
    }
    state.releaseGrids[group]?.append(makeReleaseCard(release));
  });
  syncReleaseEmptyStates(state);
}

function syncReleaseEmptyStates(state) {
  if (state !== artistCatalog || !state.albumSection || !state.singleSection) return;
  const pending = state.hasMoreReleases || state.loadingReleases;
  const groups = [
    ["albums", state.releaseGrids.albums, state.albumSection, "В доступных релизах исполнителя нет альбомов."],
    ["singles", state.releaseGrids.singles, state.singleSection, "В доступных релизах исполнителя нет синглов или EP."],
  ];
  groups.forEach(([name, grid, section, message]) => {
    const existing = state.releaseEmptyNotes[name];
    if (grid.childElementCount || pending) {
      existing?.remove();
      delete state.releaseEmptyNotes[name];
      return;
    }
    if (!existing) {
      const note = document.createElement("p");
      note.className = "catalog-note";
      note.textContent = message;
      section.append(note);
      state.releaseEmptyNotes[name] = note;
    }
  });
  const noReleases = state.releaseEmptyNotes.none;
  if (state.releases.length || pending) {
    noReleases?.remove();
    delete state.releaseEmptyNotes.none;
  } else if (!noReleases) {
    const note = document.createElement("p");
    note.className = "catalog-note";
    note.textContent = "Deezer не вернул доступных релизов для этого исполнителя.";
    state.albumSection.append(note);
    state.releaseEmptyNotes.none = note;
  }
  if (state.releaseError) {
    if (!state.releaseErrorElement) {
      state.releaseErrorElement = document.createElement("p");
      state.releaseErrorElement.className = "catalog-error";
      state.albumSection.append(state.releaseErrorElement);
    }
    state.releaseErrorElement.textContent = state.releaseError;
  } else {
    state.releaseErrorElement?.remove();
    state.releaseErrorElement = null;
  }
}

function syncReleaseControls(state) {
  if (state !== artistCatalog || !state.releaseActions) return;
  state.releaseObserver?.disconnect();
  state.releaseObserver = null;
  if (!state.hasMoreReleases) {
    if (state.releaseLoadButton) {
      state.releaseLoadButton.disabled = true;
      state.releaseLoadButton.textContent = "Все релизы из каталога загружены";
    }
    return;
  }
  if (!state.releaseLoadButton) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "catalog-more";
    button.addEventListener("click", loadMoreReleases);
    state.releaseLoadButton = button;
    state.releaseActions.append(button);
  }
  state.releaseLoadButton.disabled = state.loadingReleases;
  state.releaseLoadButton.textContent = state.loadingReleases
    ? "Загружаем релизы…"
    : state.releaseError ? "Повторить загрузку релизов" : "Загрузить ещё релизы";
  if (!state.loadingReleases && !state.releaseError) {
    state.releaseObserver = observeCatalogSentinel(state.releaseLoadButton, loadMoreReleases);
  }
}

async function requestNextReleasePage(state, signal) {
  if (state.pendingReleasePage) return state.pendingReleasePage;
  if (state !== artistCatalog || !state.hasMoreReleases || signal.aborted) return null;
  state.loadingReleases = true;
  state.releaseError = "";
  syncReleaseControls(state);
  syncReleaseEmptyStates(state);
  const index = state.releaseOffset;
  let request;
  request = (async () => {
    try {
      const page = await catalogFetch(`/api/catalog/artists/${state.id}/releases?index=${index}&limit=${catalogPageSize}`, signal);
      if (signal.aborted || state !== artistCatalog) return null;
      const raw = Array.isArray(page.data) ? page.data : [];
      state.releaseOffset = index + raw.length;
      state.hasMoreReleases = Boolean(page.has_more) && raw.length > 0;
      appendReleaseItems(state, raw);
      return page;
    } catch (error) {
      if (error.name !== "AbortError" && state === artistCatalog) {
        state.releaseError = error.message;
        status.textContent = error.message;
        syncReleaseEmptyStates(state);
      }
      throw error;
    } finally {
      if (state.pendingReleasePage === request) {
        state.pendingReleasePage = null;
        state.loadingReleases = false;
        if (state === artistCatalog && !signal.aborted) {
          syncReleaseEmptyStates(state);
          syncReleaseControls(state);
        }
      }
    }
  })();
  state.pendingReleasePage = request;
  syncReleaseControls(state);
  return request;
}

function renderArtistPagePreservingScroll() {
  const x = window.scrollX;
  const y = window.scrollY;
  renderArtistPage();
  window.scrollTo(x, y);
}

async function loadReleaseTracks(albumId, signal) {
  const all = [];
  let index = 0;
  let hasMore = true;
  while (hasMore && index <= 100000) {
    const page = await catalogFetch(`/api/catalog/albums/${albumId}/tracks?index=${index}&limit=100`, signal);
    all.push(...(page.data || []));
    index += (page.data || []).length;
    hasMore = Boolean(page.has_more) && (page.data || []).length > 0;
  }
  return { data: all };
}

function isCatalogDuplicate(state, track) {
  const id = String(track.id || "");
  if (!id || state.trackIds.has(id)) return true;
  state.trackIds.add(id);
  const markers = /\b(live|remix|remaster(?:ed)?|acoustic|demo|edit|instrumental|karaoke|version|extended|radio mix|concert)\b/i;
  const title = String(track.title || "").trim();
  const album = String(track.album || "").trim();
  const artist = String(track.artist || "").trim();
  if (!title || !artist || markers.test(title) || markers.test(album)
      || !Number.isFinite(track.duration_seconds) || track.duration_seconds <= 0) return false;
  const normalize = value => value.normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .toLowerCase().replace(/[^a-z0-9а-яё]+/g, " ").trim().replace(/\s+/g, " ");
  const key = `${normalize(artist)}|${normalize(title)}`;
  const durations = state.trackFingerprints.get(key) || [];
  if (durations.some(value => Math.abs(value - track.duration_seconds) <= 2)) return true;
  durations.push(track.duration_seconds);
  state.trackFingerprints.set(key, durations);
  return false;
}

async function openAlbum(id, pushState = false) {
  if (!/^[0-9]{1,20}$/.test(String(id))) return;
  if (catalogRoute === "artist" && artistCatalog) artistCatalog.scrollY = window.scrollY;
  catalogController?.abort();
  stopArtistLoads(artistCatalog);
  catalogController = new AbortController();
  const signal = catalogController.signal;
  const current = ++catalogRevision;
  document.querySelector(".results-section").hidden = true;
  catalogView.hidden = false;
  document.querySelector(".topbar .breadcrumb strong").textContent = "Альбом";
  catalogRoute = "album";
  if (pushState) history.pushState({}, "", `#album/${id}`);
  catalogShell();
  try {
    const [album, page] = await Promise.all([
      catalogFetch(`/api/catalog/albums/${id}`, signal),
      catalogFetch(`/api/catalog/albums/${id}/tracks?index=0&limit=${catalogPageSize}`, signal),
    ]);
    if (current !== catalogRevision) return;
    catalogView.replaceChildren();
    const back = document.createElement("button"); back.type = "button"; back.className = "catalog-back"; back.textContent = "← К исполнителю";
    back.addEventListener("click", () => album.artist_id ? openArtist(album.artist_id, true) : showSearchResults());
    const header = document.createElement("header"); header.className = "album-header";
    const cover = makeCatalogCover(album.cover_url);
    const info = document.createElement("div"); info.className = "catalog-heading";
    const h1 = document.createElement("h1"); h1.textContent = album.title;
    const meta = document.createElement("p"); meta.textContent = [album.artist, formatYear(album.release_date), album.type, album.track_count ? `${album.track_count} треков` : ""].filter(Boolean).join(" · ");
    info.append(h1, meta); header.append(cover, info);
    const section = catalogSection("Трек-лист Deezer", `ID альбома ${album.id}`);
    const trackList = document.createElement("ol"); trackList.className = "track-list catalog-track-list";
    (page.data || []).forEach(track => { if (!track.album) track.album = album.title; if (!track.cover_url) track.cover_url = album.cover_url; if (!track.artist) track.artist = album.artist; });
    appendCatalogTracks(trackList, page.data || []);
    section.append(trackList);
    const actions = document.createElement("div"); actions.className = "catalog-actions";
    if (page.has_more) {
      let offset = (page.data || []).length;
      const more = document.createElement("button"); more.type = "button"; more.className = "catalog-more"; more.textContent = "Загрузить следующий фрагмент";
      more.addEventListener("click", async () => {
        more.disabled = true;
        try {
          const next = await catalogFetch(`/api/catalog/albums/${id}/tracks?index=${offset}&limit=${catalogPageSize}`, signal);
          (next.data || []).forEach(track => { if (!track.album) track.album = album.title; if (!track.cover_url) track.cover_url = album.cover_url; if (!track.artist) track.artist = album.artist; });
          appendCatalogTracks(trackList, next.data || [], offset);
          offset += (next.data || []).length;
          more.disabled = !next.has_more;
          more.textContent = next.has_more ? "Загрузить следующий фрагмент" : "Трек-лист завершён";
        } catch (error) { if (error.name !== "AbortError") { status.textContent = error.message; more.disabled = false; } }
      });
      actions.append(more);
    }
    section.append(actions); catalogView.append(back, header, section);
  } catch (error) {
    if (error.name === "AbortError" || current !== catalogRevision) return;
    catalogShell("Не удалось открыть альбом");
    const message = document.createElement("p"); message.className = "catalog-error"; message.textContent = error.message; catalogView.append(message);
  }
}

window.addEventListener("popstate", () => {
  const tracksMatch = location.hash.match(/^#artist\/(\d+)\/tracks$/);
  if (tracksMatch) {
    if (artistCatalog?.id === tracksMatch[1]) openArtistTracks(false);
    else openArtist(tracksMatch[1], false).then(() => openArtistTracks(false));
    return;
  }
  const match = location.hash.match(/^#(artist|album)\/(\d+)$/);
  if (match) (match[1] === "artist" ? openArtist : openAlbum)(match[2], false);
  else showSearchResults(false);
});

function observeCatalogSentinel(element, callback) {
  if (!("IntersectionObserver" in window)) return;
  const observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) {
      observer.disconnect();
      callback();
    }
  }, { rootMargin: "220px" });
  observer.observe(element);
}





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
  const bufferedVisual = document.querySelector("#player-buffered");
  const heardVisual = document.querySelector("#player-heard");
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
  let seekDragging = false;
  let bufferedRangesKey = "";
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
  const hitmosPlaybackCache = new Map(); // Deezer ID -> short lived in-memory Hitmos source
  // Previous browser measurements: Hitmos playback became active in 1.85–2.52 s
  // after a resolved stream was assigned. Keep a modest margin for CDN variance.
  // The server allows up to 10 s for source lookup and up to 8 s inside discovery.
  const HITMOS_LOOKUP_TIMEOUT_MS = 11000;
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
    seekDragging = false;
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
    clearProgressVisuals();
    document.querySelector("#player-time").textContent = "—:—";
  }

  function fail(text, stage = null, code = null) {
    controls(false);
    setTilePlaybackState(selected?.id, "error");
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

  function clearBufferedVisual() {
    bufferedVisual.replaceChildren();
    bufferedRangesKey = "";
  }

  function clearProgressVisuals() {
    clearBufferedVisual();
    heardVisual.style.width = "0%";
  }

  function updateBufferedVisual(total) {
    const ranges = [];
    try {
      for (let index = 0; index < audio.buffered.length; index += 1) {
        const start = Math.max(0, audio.buffered.start(index));
        const end = Math.min(total, audio.buffered.end(index));
        if (Number.isFinite(start) && Number.isFinite(end) && end > start) ranges.push([start, end]);
      }
    } catch {
      // TimeRanges can become empty while a source is being replaced.
    }
    const key = ranges.map(([start, end]) => `${start.toFixed(3)}:${end.toFixed(3)}`).join(",");
    if (key === bufferedRangesKey) return;
    bufferedVisual.replaceChildren(...ranges.map(([start, end]) => {
      const segment = document.createElement("span");
      segment.className = "progress-buffered-segment";
      segment.style.left = `${(start / total) * 100}%`;
      segment.style.width = `${((end - start) / total) * 100}%`;
      return segment;
    }));
    bufferedRangesKey = key;
  }

  function updateProgress() {
    const usesAudio = ["hitmos", "torrent"].includes(activeKind);
    if (!ready || (!usesAudio && !player?.getCurrentTime)) {
      clearProgressVisuals();
      return;
    }
    const current = usesAudio ? audio.currentTime : player.getCurrentTime();
    const total = usesAudio ? audio.duration : player.getDuration();
    if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(current)) {
      seek.max = "1";
      if (!seekDragging) seek.value = "0";
      if (usesAudio) seek.disabled = true;
      document.querySelector("#player-time").textContent = "\u2014:\u2014";
      document.querySelector("#player-duration").textContent = "\u2014:\u2014";
      clearProgressVisuals();
      return;
    }
    const position = Math.max(0, Math.min(current, total));
    seek.max = String(total);
    if (!seekDragging) seek.value = String(position);
    if (usesAudio) {
      seek.disabled = audio.seekable.length === 0
        || audio.seekable.end(audio.seekable.length - 1) <= audio.seekable.start(0);
      updateBufferedVisual(total);
    } else {
      clearBufferedVisual();
    }
    heardVisual.style.width = `${(position / total) * 100}%`;
    document.querySelector("#player-time").textContent = duration(position);
    document.querySelector("#player-duration").textContent = duration(total);
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
    // Lab-mode playback accepts only the opaque same-origin route minted by
    // MusicStream after validating a URL from the Hitmos resolver.
    if (/^\/api\/audio\/hitmos-stream\/[A-Za-z0-9_-]{32,64}$/.test(url)) return true;
    try {
      const parsed = new URL(url);
      const prefix = ({ "ru.hitmox.org": "/get", "cdn7.deliciousoranges.com": "/s2/get", "cdn13.deliciousoranges.com": "/s1/get",
        "cdn14.deliciousoranges.com": "/s1/get", "cdn11.deliciousoranges.com": "/s1/get", "s4.deliciouspeaches.com": "/get" })[parsed.hostname];
      return Boolean(prefix) && parsed.protocol === "https:"
        && (!parsed.port || parsed.port === "443") && !parsed.username && !parsed.password && !parsed.search && !parsed.hash
        && new RegExp(`^${prefix}/(?:cuts/[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{32}/${id}/[A-Za-z0-9_-]+|music/[0-9]{8}/[A-Za-z0-9_-]+_${id})\\.mp3$`).test(parsed.pathname);
    } catch { return false; }
  }

  function audioWaiting() {
    if (!["hitmos", "torrent"].includes(activeKind) || (audio.paused && audio.readyState >= 1)) return;
    setTilePlaybackState(selected?.id, "loading");
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

  function playHitmos(item, automatic = false) {
    if (!validHitmosStream(item.stream_url, item.id)) { fail("Адрес аудио не прошёл проверку браузерного адаптера.", "stream_resolution", "client_url_rejected"); return; }
    if (!automatic) stop();
    activeKind = "hitmos";
    setTilePlaybackState(selected?.id, "loading");
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
    setTilePlaybackState(track.id, "loading");
    automaticHitmos = { startedAt, current, track, timer: null, phase: "search" };
    const cachedPlayback = hitmosPlaybackCache.get(String(track.id));
    if (cachedPlayback && cachedPlayback.expiresAt > Date.now()) {
      automaticHitmos.phase = "audio_response";
      automaticHitmos.timing = { fallback_reason: null, reused_hitmos_source: true };
      automaticHitmos.timer = setTimeout(() => {
        if (current === generation && automaticHitmos) audioFailure("cached_hitmos_audio_error");
      }, HITMOS_AUDIO_START_TIMEOUT_MS);
      playHitmos(cachedPlayback.item, true);
      return;
    }
    if (cachedPlayback) hitmosPlaybackCache.delete(String(track.id));
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
    if (/^[0-9]{1,20}$/.test(String(track.id))) params.set("deezer_id", String(track.id));
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
      if (current !== generation) return;
      const candidates = discovered.selection === "automatic" && discovered.selected_source === "hitmos"
        ? (discovered.candidates || []).filter(item => item.id === discovered.selected_id
          && item.match?.status === "likely" && /^[0-9]{1,12}$/.test(item.id))
        : (discovered.candidates || []).filter(item => item.source === "hitmos"
          && item.match?.status !== "mismatch" && /^[0-9]{1,12}$/.test(item.id));
      if (!candidates.length) {
        const hitmosStatus = (discovered.sources || []).find(item => item.source === "hitmos");
        return fallback(hitmosStatus?.reason || discovered.reason || "hitmos_no_exact_candidate");
      }

      automaticHitmos.phase = "stream_resolution";
      let stream = null;
      let currentCandidateId = null;
      let lastReason = "hitmos_stream_unavailable";
      const candidateFailures = [];
      const resolveCandidate = async candidate => {
        const resolveParams = new URLSearchParams({ title: track.title, artist: track.artist,
          provider: "hitmos", hitmos_id: candidate.id });
        if (/^[0-9]{1,20}$/.test(String(track.id))) resolveParams.set("deezer_id", String(track.id));
        if (typeof track.duration_seconds === "number") resolveParams.set("duration_seconds", String(track.duration_seconds));
        if (typeof track.explicit_lyrics === "boolean") resolveParams.set("explicit_lyrics", String(track.explicit_lyrics));
        const resolveStarted = performance.now();
        const resolved = await fetchHitmos(`/api/audio/resolve?${resolveParams}`, HITMOS_LOOKUP_TIMEOUT_MS, "hitmos_resolve_timeout");
        timing.hitmos_resolve_ms = (timing.hitmos_resolve_ms || 0) + Math.round(performance.now() - resolveStarted);
        if (current !== generation) return { stale: true };
        if (!resolved.ok) return { reason: `hitmos_stream_http_${resolved.status}` };
        const prepared = await resolved.json();
        if (current !== generation) return { stale: true };
        const playable = (prepared.candidates || []).find(item => item.id === candidate.id
          && item.match?.status === "likely" && item.method === "html_audio"
          && validHitmosStream(item.stream_url, item.id));
        return prepared.status === "available" && playable
          ? { stream: playable } : { reason: prepared.reason || "hitmos_stream_unavailable" };
      };
      let nextCandidateIndex = 0;
      for (let index = 0; index < candidates.length; index += 1) {
        const candidate = candidates[index];
        const resolved = await resolveCandidate(candidate);
        if (resolved.stale) return;
        if (resolved.stream) {
          stream = resolved.stream;
          currentCandidateId = stream.id;
          nextCandidateIndex = index + 1;
          break;
        }
        lastReason = resolved.reason || lastReason;
        candidateFailures.push({ id: candidate.id, reason: lastReason });
      }
      if (!stream) {
        console.info("MusicStream Hitmos candidates exhausted", {
          deezer_id: String(track.id), candidate_failures: candidateFailures,
        });
        return fallback(candidateFailures.length ? "hitmos_candidates_unavailable" : lastReason);
      }

      hitmosPlaybackCache.set(String(track.id), {
        item: { ...stream, title: track.title, artist: track.artist },
        expiresAt: Date.now() + 45_000,
      });
      while (hitmosPlaybackCache.size > 64) hitmosPlaybackCache.delete(hitmosPlaybackCache.keys().next().value);

      const context = automaticHitmos;
      context.tryNextCandidate = async reason => {
        if (current !== generation || automaticHitmos !== context || context.retryInProgress) return true;
        context.retryInProgress = true;
        if (currentCandidateId) candidateFailures.push({ id: currentCandidateId, reason });
        if (context.timer) clearTimeout(context.timer);
        hitmosPlaybackCache.delete(String(track.id));
        audio.pause();
        audio.removeAttribute("src");
        audio.load();
        context.phase = "stream_resolution";
        playbackState.textContent = "РџСЂРѕРІРµСЂСЏРµРј СЃР»РµРґСѓСЋС‰РёР№ Hitmos РёСЃС‚РѕС‡РЅРёРєвЂ¦";
        message.textContent = "РџРµСЂРІС‹Р№ Р°СѓРґРёРѕРёСЃС‚РѕС‡РЅРёРє РЅРµ РѕС‚РІРµС‚РёР». РџСЂРѕРІРµСЂСЏРµРј СЃР»РµРґСѓСЋС‰РёР№ РІР°СЂРёР°РЅС‚ Hitmos.";
        for (let index = nextCandidateIndex; index < candidates.length; index += 1) {
          const candidate = candidates[index];
          let resolved;
          try {
            resolved = await resolveCandidate(candidate);
          } catch (error) {
            if (current !== generation || automaticHitmos !== context) return true;
            lastReason = error.message === "hitmos_resolve_timeout" ? error.message : "hitmos_resolve_failed";
            candidateFailures.push({ id: candidate.id, reason: lastReason });
            continue;
          }
          if (resolved.stale || current !== generation || automaticHitmos !== context) return true;
          nextCandidateIndex = index + 1;
          if (!resolved.stream) {
            lastReason = resolved.reason || "hitmos_stream_unavailable";
            candidateFailures.push({ id: candidate.id, reason: lastReason });
            continue;
          }
          const nextStream = resolved.stream;
          currentCandidateId = nextStream.id;
          hitmosPlaybackCache.set(String(track.id), {
            item: { ...nextStream, title: track.title, artist: track.artist },
            expiresAt: Date.now() + 45_000,
          });
          context.phase = "audio_response";
          context.retryInProgress = false;
          if (context.timer) clearTimeout(context.timer);
          context.timer = setTimeout(() => {
            if (current === generation && automaticHitmos === context)
              void context.tryNextCandidate("hitmos_audio_start_timeout");
          }, HITMOS_AUDIO_START_TIMEOUT_MS);
          console.info("MusicStream Hitmos candidate retry", {
            deezer_id: String(track.id), candidate_id: candidate.id, previous_reason: reason,
          });
          playHitmos(nextStream, true);
          return true;
        }
        context.retryInProgress = false;
        console.info("MusicStream Hitmos candidates exhausted", {
          deezer_id: String(track.id), candidate_failures: candidateFailures,
        });
        fallback(reason || lastReason || "hitmos_candidates_unavailable");
        return true;
      };
      automaticHitmos.phase = "audio_response";
      automaticHitmos.timing = timing;
      automaticHitmos.timer = setTimeout(() => {
        if (current === generation && automaticHitmos === context)
          void context.tryNextCandidate("hitmos_audio_start_timeout");
      }, HITMOS_AUDIO_START_TIMEOUT_MS);
      console.info("MusicStream Auto timing", {
        ...timing, chosen: "hitmos_pending_audio", hitmos_url_resolved_ms: Math.round(performance.now() - startedAt),
      });
      playHitmos(stream, true);
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
      if (context.track?.id != null) hitmosPlaybackCache.delete(String(context.track.id));
      if (context.timer) clearTimeout(context.timer);
      if (context.tryNextCandidate && errorName !== "NotAllowedError") {
        void context.tryNextCandidate(reason);
        return;
      }
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
    setTilePlaybackState(selected?.id, "playing");
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
    if (selected?.id != null && trackPlaybackStates.get(String(selected.id)) === "error") return;
    setTilePlaybackState(selected?.id, automaticHitmos ? "loading" : "paused");
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
  ["durationchange", "seeking", "seeked", "emptied"].forEach(eventName => {
    audio.addEventListener(eventName, updateProgress);
  });
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
      setTilePlaybackState(selected?.id, "idle");
      play.textContent = "▶";
      play.setAttribute("aria-label", "Воспроизвести аудио");
      return;
    }
    if (activeKind === "hitmos") {
      playbackState.textContent = "Завершено · Hitmos";
      setTilePlaybackState(selected?.id, "idle");
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
    if (/^[0-9]{1,20}$/.test(String(selected.id))) params.set("deezer_id", String(selected.id));
    if (candidateId) params.set(provider === "hitmos" ? "hitmos_id" : "candidate_id", candidateId);
    if (provider === "auto") params.set("allow_youtube", String(consent.checked));
    if (typeof selected.duration_seconds === "number") params.set("duration_seconds", String(selected.duration_seconds));
    if (typeof selected.explicit_lyrics === "boolean") params.set("explicit_lyrics", String(selected.explicit_lyrics));
    try {
      const response = await fetch(`${apiBaseUrl}/api/audio/resolve?${params}`, { signal: request.signal });
      if (!response.ok) {
        const error = new Error("source lookup failed");
        error.status = response.status;
        error.retryAfter = response.headers.get("Retry-After");
        throw error;
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
        if (/^(ru\.hitmox\.org|cdn(?:7|11|13|14|17)\.deliciousoranges\.com|s4\.deliciouspeaches\.com|unapproved)$/.test(result.diagnostics.redirect_host || "")) safe.redirect_host = result.diagnostics.redirect_host;
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
        if (!chosen && provider === "auto" && result.selection === "choose"
            && items.some(item => item.source === "hitmos" && item.match?.status === "likely")) {
          play.disabled = false;
          play.setAttribute("aria-label", "Проверить и воспроизвести аудио");
        }
        if (chosen && (chosenSource === "hitmos" ? /^[0-9]{1,12}$/ : /^[a-zA-Z0-9_-]{11}$/).test(chosen.id)) {
          if (chosenSource === "hitmos" && validHitmosStream(chosen.stream_url, chosen.id)) {
            hitmosPlaybackCache.set(String(selected.id), {
              item: { ...chosen, title: selected.title, artist: selected.artist },
              expiresAt: Date.now() + 45_000,
            });
          }
          if (playAfterResolve && chosenSource === "hitmos" && validHitmosStream(chosen.stream_url, chosen.id)) { playHitmos(chosen); return; }
          if (playAfterResolve && chosenSource === "youtube" && consent.checked) { cue(chosen.id, chosen, true); return; }
          preparedSource = { ...chosen, source: chosenSource };
          play.disabled = false;
          play.setAttribute("aria-label", `Проверить источник и воспроизвести ${chosenSource === "hitmos" ? "Hitmos" : "YouTube"}`);
          playbackState.textContent = "Готово к проверке · Play";
        } else if (result.selection === "choose") playbackState.textContent = "Выберите запись";
      }
    } catch (error) {
      if (current === generation && error.name !== "AbortError") {
        const wait = Number(error.retryAfter);
        const retryText = Number.isFinite(wait) && wait > 0 ? ` Повторите через ${wait} сек.` : "";
        fail(error.status ? `API вернул HTTP ${error.status}${error.status === 429 ? `: лимит запросов.${retryText}` : error.status === 504 ? ": таймаут" : ""}. Это не ошибка сопоставления; можно открыть источник.` : "Не удалось связаться с API. Сопоставление ещё не подтверждено; можно открыть источник.", candidateId ? "stream_resolution" : "source_search", "resolve_request_failed");
      }
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
  seek.addEventListener("pointerdown", () => { seekDragging = true; });
  seek.addEventListener("input", () => { seekDragging = true; });
  seek.addEventListener("pointercancel", () => { seekDragging = false; updateProgress(); });
  seek.addEventListener("blur", () => { seekDragging = false; updateProgress(); });
  seek.addEventListener("change", () => {
    seekDragging = false;
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
        if (torrentAutoAvailable && selected && source.value === "auto"
            && !automaticHitmos && !["hitmos", "torrent"].includes(activeKind)) sourceChanged();
      }).catch(() => {});
  }

  return {
    select(track) {
      const sameTrack = selected?.id != null && String(selected.id) === String(track.id);
      if (sameTrack) {
        if (automaticHitmos?.current === generation) return false;
        if ((activeKind === "hitmos" || activeKind === "torrent") && ready) {
          if (audio.ended) audio.currentTime = 0;
          if (audio.paused) audio.play().catch(error => audioFailure(error.name));
          return false;
        }
        if (activeKind === "youtube" && player) {
          if (player.getPlayerState() !== 1) player.playVideo();
          return false;
        }
      }
      stop();
      selected = track;
      source.value = "auto";
      section.hidden = false;
      candidates.replaceChildren();
      reports.replaceChildren();
      resolveButton.disabled = false;
      document.querySelector("#youtube-url").value = "";
      externalSearch.href = "https://www.youtube.com/results?" + new URLSearchParams({ search_query: `${track.artist || ""} ${track.title || ""}` });
      hitmosSearch.href = "https://ru.hitmox.org/search?" + new URLSearchParams({ q: `${track.artist || ""} ${track.title || ""}` });
      hitmosLink.hidden = true;
      document.querySelector("#hitmos-url").value = "";
      setTilePlaybackState(track.id, "loading");
      prepareAutomaticPlayback();
      return true;
    },
  };
})();
