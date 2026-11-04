/* Раскадровка: страница сервиса. Без сборки и зависимостей: ES-модуль,
   разметка шаблонами строк, состояние проекта приходит с сервера.

   #/            новый ролик и галерея
   #/library     то же, прокрутка к галерее
   #/p/<id>      проект: сценарий → кадры → съёмка */

const view = document.getElementById("view");
const AR = { "16:9": "1344 / 768", "9:16": "768 / 1344", "1:1": "1 / 1" };
const FORMAT_WORDS = { "16:9": "Горизонтальный", "9:16": "Вертикальный", "1:1": "Квадрат" };
const QUALITY = { draft: { title: "Черновик", minutes: 3 }, fast: { title: "Быстро", minutes: 6 } };
const STATE = { working: "В работе", ready: "Готов", failed: "Ошибка", draft: "Черновик" };
const IDEAS = [
  "Лиса бежит по снегу на рассвете",
  "Старый маяк в шторм",
  "Кофе у окна, за стеклом дождь",
  "Ночной Токио с высоты",
];
const DEFAULTS = { format: "16:9", seconds: 5, quality: "fast", auto: false };
const PREFS = "storyboard:prefs";
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

let config = {
  formats: ["16:9", "9:16", "1:1"], seconds: [5, 10], qualities: ["draft", "fast"],
  models: { script: "Qwen3.8-Flash-Next-NVFP4", frames: "Qwen-Image-2.1", video: "MiniMax-H3" },
};
/** Открытый проект: данные, вкладка, черновики полей. На главной null. */
let current = null;
let timer = 0;
let pending = null;

// ------------------------------------------------------------ мелочи
const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const icon = (name, cls = "") => `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

function plural(n, one, few, many) {
  const a = n % 10, b = n % 100;
  if (a === 1 && b !== 11) return one;
  if (a >= 2 && a <= 4 && (b < 12 || b > 14)) return few;
  return many;
}

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "только что";
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  if (s < 86400) return `${Math.floor(s / 3600)} ч назад`;
  return new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

function timecode(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60]
    .map((n) => String(n).padStart(2, "0")).join(":");
}

function minutes(p, take) {
  if (take?.estimated_s) return Math.max(1, Math.round(take.estimated_s / 60));
  return QUALITY[p.quality].minutes * (p.seconds / 5);
}

function prefs() {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(PREFS) || "{}") }; }
  catch { return { ...DEFAULTS }; }
}

function savePrefs(value) {
  try { localStorage.setItem(PREFS, JSON.stringify(value)); } catch { /* приватный режим */ }
}

async function api(method, path, body) {
  let response;
  try {
    response = await fetch(path, {
      method, headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error("Нет связи с сервисом. Проверьте интернет и повторите");
  }
  let data = null;
  try { data = await response.json(); } catch { /* не JSON */ }
  if (response.status === 401) throw new Error("Сессия закончилась. Обновите страницу и войдите снова");
  if (!response.ok) throw new Error(data?.error || `Сервис ответил ошибкой ${response.status}`);
  return data;
}

function toast(text, bad = false) {
  const el = document.createElement("div");
  el.className = bad ? "toast toast--bad" : "toast";
  el.innerHTML = `${icon(bad ? "alert" : "check")}<span>${esc(text)}</span>`;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 4500);
}

/** Опрос: один таймер на страницу; во фоновой вкладке ждёт возвращения. */
function poll(fn, ms) {
  clearTimeout(timer);
  timer = setTimeout(() => {
    if (document.hidden) pending = fn;
    else fn();
  }, ms);
}

function stopPolling() {
  clearTimeout(timer);
  pending = null;
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && pending) {
    const fn = pending;
    pending = null;
    fn();
  }
});

function setNav(which) {
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === which) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

function setCount(n) {
  const el = $("#count");
  el.hidden = !n;
  el.textContent = n;
}

function notice(text, act, label = "Повторить") {
  return `<div class="notice" role="alert">${icon("alert")}<div class="notice__body"><p>${esc(text)}</p>${
    act ? `<div class="row"><button class="btn" type="button" data-act="${act}">${icon("refresh")}${label}</button></div>` : ""
  }</div></div>`;
}

// ------------------------------------------------------------ маршруты
function route() {
  stopPolling();
  const hash = location.hash || "#/";
  const found = hash.match(/^#\/p\/([0-9a-f]{16})$/);
  setNav(found ? "" : hash === "#/library" ? "library" : "new");
  if (found) return openProject(found[1]);
  current = null;
  return home(hash === "#/library");
}

window.addEventListener("hashchange", () => {
  route();
  if (!location.hash.startsWith("#/library")) window.scrollTo({ top: 0 });
  view.focus({ preventScroll: true });
});

// ------------------------------------------------------------ главная
function seg(name, title, options, value) {
  return `<fieldset class="option"><legend class="label">${title}</legend><div class="seg">${options.map((o) =>
    `<label><input type="radio" name="${name}" value="${o.value}" ${String(o.value) === String(value) ? "checked" : ""}><span>${o.html}</span></label>`,
  ).join("")}</div></fieldset>`;
}

function station(no, iconName, title, model, text) {
  return `<div class="station"><span class="station__no">${icon(iconName, "icon--lg")}</span><div class="station__body">
    <span class="label">${no}</span><h3>${title}<span class="mono">${esc(model)}</span></h3><p>${text}</p></div></div>`;
}

function home(toLibrary) {
  const p = prefs();
  const m = config.models;
  view.innerHTML = `
  <section class="hero"><div class="wrap">
    <div>
      <div class="hero__copy">
        <span class="label">Студия коротких роликов</span>
        <h1>Ролик со звуком <em>по одной фразе</em></h1>
        <p class="hero__lead">Опишите идею своими словами. Три модели по очереди напишут сценарий, нарисуют кадры и снимут ролик, а вы выберете лучший вариант на каждом шаге.</p>
      </div>
      <form class="composer" id="composer" novalidate>
        <div class="composer__label"><label class="label" for="idea">Идея ролика</label><span class="composer__count" id="idea-count">0 / 600</span></div>
        <textarea id="idea" maxlength="600" placeholder="Например: старый маяк в шторм, волны разбиваются о скалы"></textarea>
        <div class="ideas" role="group" aria-label="Примеры идей">${IDEAS.map((i) => `<button type="button" class="idea">${esc(i)}</button>`).join("")}</div>
        <div class="options">
          ${seg("format", "Формат", config.formats.map((f) => ({ value: f, html: `<i class="ar ar--${f.replace(":", "x")}" aria-hidden="true"></i>${f}` })), p.format)}
          ${seg("seconds", "Длительность", config.seconds.map((s) => ({ value: s, html: `${s} с` })), p.seconds)}
          ${seg("quality", "Качество съёмки", config.qualities.map((q) => ({ value: q, html: `${QUALITY[q].title}<small data-minutes="${q}"></small>` })), p.quality)}
        </div>
        <div class="composer__go">
          <label class="switch"><input type="checkbox" id="auto" role="switch" ${p.auto ? "checked" : ""}><span class="switch__track" aria-hidden="true"></span><span class="switch__text"><b>Без остановок</b><small>Сценарий, кадр и съёмка подряд, без выбора</small></span></label>
          <button class="btn btn--acc btn--lg" id="go" type="submit"></button>
        </div>
        <p class="form-error" id="form-error" role="alert" hidden></p>
      </form>
    </div>
    <aside class="pipe" aria-label="Как это работает">
      <span class="label pipe__title">Три модели, три шага</span>
      ${station("01 · Сценарий", "pen", "Сценарий", m.script, "Превращает идею в план кадра: свет, движение камеры и звук. Промпты для следующих моделей пишет сам.")}
      ${station("02 · Кадры", "image", "Кадры", m.frames, "Рисует первый кадр в трёх вариантах. По желанию дорисует последний, сохранив героев и стиль.")}
      ${station("03 · Съёмка", "video", "Съёмка", m.video, "Снимает ролик со звуком, который начинается с выбранного кадра.")}
    </aside>
  </div></section>
  <section class="library" id="library" aria-labelledby="library-title"><div class="wrap">
    <div class="section-head"><h2 id="library-title">Мои ролики</h2><span class="label" id="library-count"></span></div>
    <div id="library-list"><div class="grid">${'<div class="card"><div class="cover skel"></div><div class="card__body"><span class="skel" style="height:16px;width:70%"></span><span class="skel" style="height:12px;width:45%"></span></div></div>'.repeat(3)}</div></div>
  </div></section>`;
  document.title = "Раскадровка";
  bindComposer();
  if (toLibrary) $("#library").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
  loadLibrary();
}

function bindComposer() {
  const form = $("#composer"), idea = $("#idea"), go = $("#go"), auto = $("#auto"), error = $("#form-error");
  const sync = () => {
    $("#idea-count").textContent = `${idea.value.length} / 600`;
    go.innerHTML = auto.checked ? `${icon("clapper")}Снять ролик` : `${icon("pen")}Написать сценарий`;
    const seconds = Number(new FormData(form).get("seconds")) || 5;
    form.querySelectorAll("[data-minutes]").forEach((el) => {
      el.textContent = `≈${QUALITY[el.dataset.minutes].minutes * (seconds / 5)} мин`;
    });
  };
  const fail = (text) => { error.textContent = text; error.hidden = !text; };
  idea.addEventListener("input", () => { fail(""); sync(); });
  form.addEventListener("change", sync);
  form.querySelectorAll(".idea").forEach((b) => b.addEventListener("click", () => {
    idea.value = b.textContent;
    fail("");
    sync();
    idea.focus();
  }));
  idea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) form.requestSubmit();
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = new FormData(form);
    const choice = { format: data.get("format"), seconds: Number(data.get("seconds")), quality: data.get("quality"), auto: auto.checked };
    savePrefs(choice);
    const text = idea.value.trim();
    if (text.length < 3) { fail("Опишите идею хотя бы парой слов"); idea.focus(); return; }
    go.disabled = true;
    go.innerHTML = '<span class="spin" aria-hidden="true"></span>Создаём проект';
    try {
      const project = await api("POST", "/api/projects", { idea: text, ...choice });
      location.hash = `#/p/${project.id}`;
    } catch (err) {
      fail(err.message);
      go.disabled = false;
      sync();
    }
  });
  sync();
}

function card(d) {
  const meta = [d.format, `${d.seconds} с`, d.takes ? `${d.takes} ${plural(d.takes, "дубль", "дубля", "дублей")}` : "", ago(d.created)]
    .filter(Boolean).join(" · ");
  return `<a class="card" href="#/p/${d.id}">
    <div class="cover"${d.cover ? ` style="--img:url('${esc(d.cover)}')"` : ""}>
      ${d.cover ? `<img src="${esc(d.cover)}" alt="" loading="lazy">` : `<span class="cover__empty">${icon("film", "icon--lg")}</span>`}
      <span class="badge badge--${d.state}">${STATE[d.state]}</span>
      ${d.video ? `<span class="cover__play">${icon("play")}</span>` : ""}
    </div>
    <div class="card__body"><span class="card__title">${esc(d.title)}</span><span class="card__meta">${esc(meta)}</span></div>
  </a>`;
}

async function loadLibrary() {
  try {
    const { data } = await api("GET", "/api/projects");
    setCount(data.length);
    const box = $("#library-list");
    if (!box || current) return;                    // ушли со страницы
    $("#library-count").textContent = data.length ? `${data.length} ${plural(data.length, "проект", "проекта", "проектов")}` : "";
    box.innerHTML = data.length
      ? `<div class="grid">${data.map(card).join("")}</div>`
      : `<div class="empty">${icon("film")}<b>Здесь появятся ваши ролики</b><span>Опишите идею выше: проект сохранится, даже если закрыть страницу.</span></div>`;
    if (data.some((d) => d.state === "working")) poll(loadLibrary, 10000);
  } catch (err) {
    const box = $("#library-list");
    if (box) box.innerHTML = notice(err.message);
  }
}

// ------------------------------------------------------------ проект
async function openProject(id) {
  current = { id, project: null, panel: "", shownPanel: "", stage: "", take: "", sig: "", drafts: {}, scriptSig: "", openPrompts: false, confirmDelete: false };
  view.innerHTML = '<div class="loading"><span class="spin" aria-label="Загрузка проекта"></span></div>';
  await refresh(true);
}

async function refresh(first = false) {
  const state = current;
  if (!state) return;
  try {
    apply(await api("GET", `/api/projects/${state.id}`), state);
  } catch (err) {
    if (current !== state) return;
    if (first) {
      view.innerHTML = `<div class="wrap missing"><h1>Проект не открылся</h1><p>${esc(err.message)}</p><a class="btn btn--acc" href="#/">${icon("plus")}Новый ролик</a></div>`;
      return;
    }
    toast(err.message, true);
  }
  if (current === state) schedule();
}

/** Новое состояние проекта: вкладка идёт за этапом, если он сменился. */
function apply(project, state = current, panel = "") {
  if (!state || current !== state) return;
  state.project = project;
  if (panel) state.panel = panel;
  if (project.stage !== state.stage) {
    if (!panel) state.panel = project.stage;
    state.stage = project.stage;
  }
  const scriptSig = project.script.title + project.script.frame_prompt + project.script.video_prompt + project.script.end_prompt;
  if (scriptSig !== state.scriptSig) {
    state.scriptSig = scriptSig;
    state.drafts = {};
  }
  render();
}

function schedule() {
  const p = current?.project;
  if (!p) return;
  const busy = p.script.status === "working" || p.frames.status === "working";
  const shooting = p.takes.some((t) => t.status === "working");
  if (busy || shooting) poll(() => refresh(), busy ? 2500 : 6000);
}

function signature(p) {
  return JSON.stringify({
    ...p, updated: 0,
    takes: p.takes.map((t) => [t.id, t.status, t.error, t.url]),
  });
}

function render() {
  const state = current;
  const p = state.project;
  const sig = [signature(p), state.panel, state.take, state.confirmDelete, state.openPrompts].join("|");
  if (sig === state.sig) return live();
  state.sig = sig;
  const focused = document.activeElement?.id;
  view.innerHTML = projectHtml(p);
  if (state.panel !== state.shownPanel) {         // появление только при смене шага
    state.shownPanel = state.panel;
    $("#panel").classList.add("panel--enter");
  }
  view.querySelectorAll("[data-draft]").forEach((el) => {
    if (el.id in state.drafts) el.value = state.drafts[el.id];
  });
  if (focused) document.getElementById(focused)?.focus({ preventScroll: true });
  document.title = `${p.script.title || "Новый проект"} · Раскадровка`;
  live();
}

/** То, что меняется каждую секунду, обновляется без перерисовки страницы. */
function live() {
  const now = Date.now() / 1000;
  document.querySelectorAll("[data-timecode]").forEach((el) => {
    el.textContent = timecode(now - Number(el.dataset.timecode));
  });
  document.querySelectorAll("[data-elapsed]").forEach((el) => {
    const since = Number(el.dataset.elapsed);
    el.textContent = since ? `Прошло ${timecode(now - since).slice(3)}.` : "";
  });
  const p = current?.project;
  const bar = $("[data-bar]");
  if (!p || !bar) return;
  const take = shownTake(p);
  if (!take) return;
  bar.classList.toggle("bar--queued", !!take.queued);
  bar.setAttribute("aria-valuenow", String(take.progress));
  bar.firstElementChild.style.width = `${take.progress}%`;
  $("[data-progress-text]").textContent = take.queued ? "В очереди" : `${take.progress}%`;
  $("[data-shoot-text]").textContent = take.queued ? "Ролик ждёт очереди на съёмку." : "MiniMax снимает ролик.";
}
setInterval(() => { if (current?.project && !document.hidden) live(); }, 1000);

function shownTake(p) {
  return p.takes.find((t) => t.id === current.take) || p.takes[p.takes.length - 1];
}

function stepState(p, id) {
  const f = p.frames, last = p.takes[p.takes.length - 1];
  if (id === "script") {
    return { state: p.script.status === "ready" ? "done" : p.script.status === "failed" ? "failed" : "working", open: true };
  }
  if (id === "frames") {
    const state = f.status === "working" ? "working" : f.status === "failed" ? "failed" : f.chosen_start ? "done" : "";
    return { state, open: p.script.status === "ready" || f.start.length > 0 };
  }
  const state = !last ? "" : last.status === "working" ? "working" : p.takes.some((t) => t.status === "ready") ? "done" : "failed";
  return { state, open: p.takes.length > 0 || !!f.chosen_start };
}

function projectHtml(p) {
  const m = config.models;
  const steps = [["script", "01", "Сценарий", m.script], ["frames", "02", "Кадры", m.frames], ["video", "03", "Съёмка", m.video]];
  const chips = [`${FORMAT_WORDS[p.format]} ${p.format}`, `${p.seconds} с`, QUALITY[p.quality].title, p.auto ? "Без остановок" : "", ago(p.created)]
    .filter(Boolean).map((c) => `<span class="chip">${esc(c)}</span>`).join("");
  const actions = current.confirmDelete
    ? `<div class="confirm" role="group" aria-label="Удаление"><span>Удалить проект вместе с кадрами и роликами?</span>
         <button class="btn btn--ghost" type="button" data-act="cancel-delete">Отмена</button>
         <button class="btn btn--bad" type="button" data-act="delete">${icon("trash")}Удалить</button></div>`
    : `<button class="btn btn--ghost" type="button" data-act="ask-delete">${icon("trash")}Удалить</button>`;
  return `<div class="project"><div class="wrap" style="--ar:${AR[p.format]}">
    <a class="back" href="#/library">${icon("left")}Все ролики</a>
    <div class="phead">
      <div class="phead__text"><h1>${esc(p.script.title || p.idea)}</h1><div class="chips">${chips}</div></div>
      <div class="phead__actions">${actions}</div>
    </div>
    <ol class="steps" aria-label="Шаги">${steps.map(([id, no, title, model]) => {
      const { state, open } = stepState(p, id);
      const dot = state === "done" ? icon("check", "icon--sm") : state === "working" ? '<span class="spin"></span>'
        : state === "failed" ? icon("alert", "icon--sm") : no;
      return `<li><button class="step ${state ? `step--${state}` : ""}" type="button" data-panel="${id}" ${current.panel === id ? 'aria-current="step"' : ""} ${open ? "" : "disabled"}>
        <span class="step__dot">${dot}</span><span class="step__text"><b>${title}</b><small>${esc(model)}</small></span></button></li>`;
    }).join("")}</ol>
    <section class="panel" id="panel">${current.panel === "frames" ? framesPanel(p) : current.panel === "video" ? videoPanel(p) : scriptPanel(p)}</section>
  </div></div>`;
}

function panelHead(title, text, iconName, model) {
  return `<div class="panel__head"><div><h2>${title}</h2><p>${text}</p></div><span class="model-tag">${icon(iconName, "icon--sm")}${esc(model)}</span></div>`;
}

function working(title, text, since) {
  return `<div class="working" role="status"><span class="spin" aria-hidden="true"></span><div><b>${title}<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span></b><small>${text} <span data-elapsed="${since || 0}"></span></small></div></div>`;
}

// 1. Сценарий
function scriptPanel(p) {
  const s = p.script, m = config.models;
  const head = panelHead("Сценарий", "Модель превращает идею в план одного кадра и пишет промпты для следующих шагов.", "sparkles", m.script);
  const quote = `<blockquote class="idea-quote">${esc(p.idea)}</blockquote>`;
  const fact = (iconName, title, value) => value
    ? `<div class="fact"><span class="fact__icon">${icon(iconName)}</span><div><small>${title}</small><span>${esc(value)}</span></div></div>` : "";
  const field = (id, title, model, value) => `<div class="field"><label for="${id}">${title}<small>${esc(model)}</small></label>
    <textarea id="${id}" data-draft spellcheck="false" ${s.status === "working" ? "disabled" : ""}>${esc(value)}</textarea></div>`;
  if (s.status !== "ready" && !s.video_prompt) {
    if (s.status === "failed") return head + notice(s.error, "retry-script") + quote;
    return head + working("Qwen пишет сценарий", "Обычно это меньше минуты. Если модель только просыпается, первый ответ займёт пару минут.", s.started)
      + quote + '<div class="skel-lines" aria-hidden="true"><span class="skel" style="width:40%;height:26px"></span><span class="skel"></span><span class="skel" style="width:85%"></span><span class="skel" style="width:60%"></span></div>';
  }
  const frames = p.frames;
  const next = frames.start.length || frames.status === "working"
    ? `<button class="btn btn--acc btn--lg" type="button" data-panel="frames">К кадрам${icon("right")}</button>`
    : `<button class="btn btn--acc btn--lg" type="button" data-act="draw-start" ${s.status === "working" ? "disabled" : ""}>${icon("image")}Нарисовать кадры</button>`;
  return head
    + (s.status === "working" ? working("Qwen переписывает сценарий", "Старый вариант виден, пока не готов новый.", s.started) : "")
    + (s.status === "failed" ? notice(s.error, "retry-script") : "")
    + `<div class="script">
      <div>
        ${quote}
        <h3 class="script__title">${esc(s.title)}</h3>
        <p class="script__log">${esc(s.logline)}</p>
        <div class="facts">${fact("sun", "Настроение", s.mood)}${fact("move", "Камера", s.camera)}${fact("sound", "Звук", s.sound)}</div>
      </div>
      <div class="prompts">
        <details id="prompts" ${current.openPrompts ? "open" : ""}>
          <summary>${icon("pen")}Промпты для моделей${icon("down")}</summary>
          <div class="prompts__body">
            ${field("frame_prompt", "Первый кадр", m.frames, s.frame_prompt)}
            ${field("video_prompt", "Движение и звук", m.video, s.video_prompt)}
            ${field("end_prompt", "Последний кадр", m.frames, s.end_prompt)}
            <div class="row"><button class="btn" type="button" data-act="save-script" ${s.status === "working" ? "disabled" : ""}>${icon("check")}Сохранить правки</button></div>
          </div>
        </details>
        <form class="field" id="rewrite">
          <label for="note">Переписать сценарий<small>${esc(m.script)}</small></label>
          <div class="rewrite"><input class="input" id="note" data-draft maxlength="300" placeholder="Что поменять? Например: ночь вместо дня">
          <button class="btn" type="submit" ${s.status === "working" ? "disabled" : ""}>${icon("refresh")}Переписать</button></div>
        </form>
      </div>
    </div>
    <div class="panel__foot"><p>Дальше ${esc(m.frames)} нарисует первый кадр в трёх вариантах.</p>${next}</div>`;
}

// 2. Кадры
function frameRadio(kind, frame, index, checked, locked) {
  return `<label class="frame"><input type="radio" name="${kind}" value="${frame.id}" ${checked ? "checked" : ""} ${locked ? "disabled" : ""} aria-label="Вариант ${index + 1}">
    <img src="${esc(frame.url)}" alt="" loading="lazy"><span class="frame__no">${String(index + 1).padStart(2, "0")}</span>
    <span class="frame__check">${icon("check", "icon--sm")}</span></label>`;
}

function framesPanel(p) {
  const f = p.frames, m = config.models;
  const busy = f.status === "working";
  const tall = p.format === "9:16" ? " frames--tall" : "";
  const skeletons = (n) => '<div class="frame frame--skel skel" aria-hidden="true"></div>'.repeat(n);
  const shootingNow = p.takes.some((t) => t.status === "working");
  const head = panelHead("Кадры", "Ролик начнётся с первого кадра. Последний кадр задаёт, чем он закончится.", "image", m.frames);
  const error = f.status === "failed" ? notice(f.error, f.doing === "end" ? "draw-end" : "draw-start") : "";

  if (!f.start.length && !busy) {
    return head + error + `<div class="empty">${icon("image")}<b>Кадров пока нет</b><span>${esc(m.frames)} нарисует три варианта первого кадра по сценарию.</span>
      <button class="btn btn--acc" type="button" data-act="draw-start">${icon("image")}Нарисовать кадры</button></div>`;
  }
  const startBlock = `<div class="frames-block">
    <div class="frames-block__head"><div><h3>Первый кадр</h3><p>Выберите вариант, с которого начнётся ролик.</p></div>
      <button class="btn" type="button" data-act="draw-start" ${busy ? "disabled" : ""}>${icon("refresh")}Ещё варианты</button></div>
    ${busy && f.doing === "start" ? working(`${esc(m.frames)} рисует три варианта`, "Обычно это полминуты–минута.", f.started) : ""}
    <div class="frames${tall}" role="radiogroup" aria-label="Первый кадр">
      ${busy && f.doing === "start" ? skeletons(3) : ""}${f.start.map((fr, i) => frameRadio("start", fr, i, fr.id === f.chosen_start, busy)).join("")}
    </div></div>`;

  let endBlock = "";
  if (f.chosen_start) {
    const drawingEnd = busy && f.doing === "end";
    const choices = f.end.length || drawingEnd
      ? `<div class="frames${tall}" role="radiogroup" aria-label="Последний кадр">
          <label class="frame frame--none"><input type="radio" name="end" value="" ${f.chosen_end ? "" : "checked"} ${busy ? "disabled" : ""}><span><b>Без последнего кадра</b>Финал решит модель</span><span class="frame__check">${icon("check", "icon--sm")}</span></label>
          ${drawingEnd ? skeletons(2) : ""}${f.end.map((fr, i) => frameRadio("end", fr, i, fr.id === f.chosen_end, busy)).join("")}
        </div>`
      : "";
    endBlock = `<div class="frames-block">
      <div class="frames-block__head"><div><h3>Последний кадр <span class="label">по желанию</span></h3><p>Модель дорисует финал из первого кадра, герои и стиль сохранятся. Без него MiniMax закончит ролик сам.</p></div>
        <button class="btn" type="button" data-act="draw-end" ${busy ? "disabled" : ""}>${icon("flag")}${f.end.length ? "Ещё варианты" : "Нарисовать последний кадр"}</button></div>
      ${drawingEnd ? working(`${esc(m.frames)} рисует финал`, "Правка первого кадра, обычно до минуты.", f.started) : ""}
      ${choices}</div>`;
  }

  const startFrame = f.start.find((fr) => fr.id === f.chosen_start);
  const endFrame = f.end.find((fr) => fr.id === f.chosen_end);
  const thumb = (frame, empty) => `<span class="reel__thumb">${frame ? `<img src="${esc(frame.url)}" alt="">` : empty}</span>`;
  const go = shootingNow
    ? `<button class="btn btn--acc btn--lg" type="button" data-panel="video">${icon("video")}Смотреть съёмку</button>`
    : `<button class="btn btn--acc btn--lg" type="button" data-act="shoot" ${!f.chosen_start || busy ? "disabled" : ""}>${icon("clapper")}Снять ролик</button>`;
  return head + error + startBlock + endBlock + `<div class="panel__foot">
    <div class="reel">${thumb(startFrame, "нет кадра")}${icon("right", "reel__arrow")}${thumb(endFrame, "финал модели")}
      <p>${p.seconds} с · ${QUALITY[p.quality].title.toLowerCase()} · около ${minutes(p)} мин</p></div>${go}</div>`;
}

// 3. Съёмка
function videoPanel(p) {
  const m = config.models;
  const head = panelHead("Съёмка", "MiniMax снимает ролик со звуком по выбранным кадрам и сценарию.", "video", m.video);
  const take = shownTake(p);
  const tall = p.format === "9:16" ? " stage--tall" : "";
  if (!take) {
    return head + `<div class="empty">${icon("clapper")}<b>Съёмка ещё не начиналась</b><span>Выберите первый кадр и нажмите «Снять ролик».</span>
      <button class="btn btn--acc" type="button" data-act="shoot">${icon("clapper")}Снять ролик</button></div>`;
  }
  const working = p.takes.some((t) => t.status === "working");
  const number = p.takes.indexOf(take) + 1;
  let body;
  if (take.status === "working") {
    body = `<div class="stage${tall}">
      ${take.start ? `<img class="stage__bg" src="${esc(take.start)}" alt="">` : ""}
      <div class="stage__over">
        <div class="stage__top"><span class="rec">REC</span><span class="timecode" data-timecode="${take.started}">00:00:00</span></div>
        <div class="stage__bottom" role="status">
          <p><span data-shoot-text>MiniMax снимает ролик.</span> Обычно это около ${minutes(p, take)} мин. Страницу можно закрыть: ролик сохранится в «Моих роликах».</p>
          <div class="bar" data-bar role="progressbar" aria-label="Съёмка" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><i></i></div>
          <div class="progress-line"><span data-progress-text></span><span>Дубль ${number} · ${p.seconds} с</span></div>
        </div>
      </div></div>`;
  } else if (take.status === "failed") {
    body = notice(`Дубль ${number} не снялся: ${take.error}`, working ? "" : "shoot", "Снять заново");
  } else {
    body = `<div class="stage${tall}"><video src="${esc(take.url)}" controls playsinline preload="metadata" ${take.start ? `poster="${esc(take.start)}"` : ""}></video></div>`;
  }
  const takes = p.takes.length > 1
    ? `<div class="takes" role="group" aria-label="Дубли">${p.takes.map((t, i) =>
      `<button class="take" type="button" data-take="${t.id}" aria-pressed="${t.id === take.id}"><span class="badge badge--${t.status}"></span>Дубль ${i + 1}</button>`).join("")}</div>`
    : "";
  const download = take.status === "ready"
    ? `<a class="btn" href="${esc(take.url)}?download=1" download>${icon("download")}Скачать</a>` : "";
  return head + body + takes + `<div class="done-row">
    <p class="label">${take.status === "ready" ? `Дубль ${number} · снят ${ago(take.finished || take.started)}` : `Дубль ${number}`}</p>
    <div class="row">${download}
      <button class="btn" type="button" data-act="shoot" ${working ? "disabled" : ""}>${icon("refresh")}Ещё дубль</button>
      <a class="btn btn--acc" href="#/">${icon("plus")}Новый ролик</a></div></div>`;
}

// ------------------------------------------------------------ действия
async function run(button, request, panel = "") {
  const state = current;
  const html = button?.innerHTML;
  if (button) {
    button.disabled = true;
    button.innerHTML = `<span class="spin" aria-hidden="true"></span>${button.textContent.trim()}`;
  }
  try {
    const project = await request();
    if (current !== state) return;
    state.sig = "";
    apply(project, state, panel);
    schedule();
  } catch (err) {
    toast(err.message, true);
    if (button?.isConnected) {
      button.disabled = false;
      button.innerHTML = html;
    }
  }
}

view.addEventListener("click", (e) => {
  const button = e.target.closest("[data-act], [data-panel], [data-take]");
  if (!button || !current?.project) return;
  const id = current.id;
  if (button.dataset.panel) {
    current.panel = button.dataset.panel;
    render();
    return;
  }
  if (button.dataset.take) {
    current.take = button.dataset.take;
    render();
    return;
  }
  const p = current.project;
  switch (button.dataset.act) {
    case "retry-script":
      return run(button, () => api("POST", `/api/projects/${id}/script`, {}));
    case "draw-start":
      return run(button, () => api("POST", `/api/projects/${id}/frames`, { kind: "start" }), "frames");
    case "draw-end":
      return run(button, () => api("POST", `/api/projects/${id}/frames`, { kind: "end" }), "frames");
    case "shoot":
      current.take = "";
      return run(button, () => api("POST", `/api/projects/${id}/takes`), "video");
    case "save-script": {
      const body = {};
      for (const key of ["frame_prompt", "video_prompt", "end_prompt"]) {
        const value = $(`#${key}`)?.value.trim();
        if (value !== undefined && value !== p.script[key]) body[key] = value;
      }
      if (!Object.keys(body).length) return toast("Правок нет");
      return run(button, async () => {
        const project = await api("PUT", `/api/projects/${id}/script`, body);
        current.drafts = {};
        toast("Промпты сохранены");
        return project;
      });
    }
    case "ask-delete":
    case "cancel-delete":
      current.confirmDelete = button.dataset.act === "ask-delete";
      return render();
    case "delete":
      return (async () => {
        button.disabled = true;
        try {
          await api("DELETE", `/api/projects/${id}`);
          toast("Проект удалён");
          location.hash = "#/library";
        } catch (err) {
          toast(err.message, true);
          button.disabled = false;
        }
      })();
    default:
      return undefined;
  }
});

view.addEventListener("submit", (e) => {
  if (e.target.id !== "rewrite" || !current?.project) return;
  e.preventDefault();
  const note = $("#note").value.trim();
  run(e.submitter, async () => {
    const project = await api("POST", `/api/projects/${current.id}/script`, { note });
    delete current.drafts.note;
    return project;
  });
});

view.addEventListener("change", (e) => {
  const input = e.target;
  if (!current?.project || !input.matches(".frame input[type=radio]")) return;
  const body = input.name === "start" ? { start: input.value } : { end: input.value };
  run(null, () => api("PUT", `/api/projects/${current.id}/frames`, body));
});

view.addEventListener("input", (e) => {
  if (current && e.target.matches("[data-draft]")) current.drafts[e.target.id] = e.target.value;
});

view.addEventListener("toggle", (e) => {
  if (current && e.target.id === "prompts") current.openPrompts = e.target.open;
}, true);

// ------------------------------------------------------------ старт
(async () => {
  try {
    config = await api("GET", "/api/config");
  } catch { /* страница работает и с настройками по умолчанию */ }
  const m = config.models;
  $("#models").textContent = `${m.script} · ${m.frames} · ${m.video}`;
  route();
})();
