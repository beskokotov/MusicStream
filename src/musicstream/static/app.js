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
  status.textContent = `Выбрана композиция ${track.title || "без названия"}. Воспроизведение пока недоступно.`;
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
    const response = await fetch(`/api/search?${params}`, { signal: controller.signal });
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

reset();
