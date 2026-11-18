// FindUrHome — фронтенд: поиск, фильтры, карта, избранное и ИИ-чат.
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const TYPES = {
  short: [["hotel", "Отель"], ["apartment", "Апартаменты"], ["hostel", "Хостел"], ["guest_house", "Гостевой дом"], ["vacation_rental", "Аренда у хозяев"], ["villa", "Вилла"]],
  long: [["apartment", "Квартира"], ["room", "Комната"], ["house", "Дом"], ["studio", "Студия"]],
};
const SOURCE_NAMES = {};
const state = { mode: "short", result: null, markers: {}, sources: [], chat: [] };

// --- избранное (localStorage может быть недоступен) -------------------
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* приватный режим */ } },
};
let favs = store.get("fuh-favs", {});

// --- карта --------------------------------------------------------------
const map = L.map("map", { zoomControl: true }).setView([30, 10], 2);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "© OpenStreetMap",
}).addTo(map);
const layer = L.layerGroup().addTo(map);

// --- режим --------------------------------------------------------------
function setMode(mode) {
  state.mode = mode;
  document.body.classList.toggle("long", mode === "long");
  $$(".mode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#priceHint").textContent = mode === "short" ? "за ночь" : "в месяц";
  $("#types").innerHTML = TYPES[mode]
    .map(([v, t]) => `<label><input type="checkbox" name="property_types" value="${v}" form="search"><span>${t}</span></label>`)
    .join("");
  renderSources();
}
$$(".mode button").forEach((b) => b.addEventListener("click", () => { setMode(b.dataset.mode); if (state.result) doSearch(); }));

// --- источники ----------------------------------------------------------
async function loadSources() {
  try {
    state.sources = await (await fetch("/api/sources")).json();
  } catch { state.sources = []; }
  state.sources.forEach((s) => (SOURCE_NAMES[s.id] = s.name));
  renderSources();
}
function renderSources() {
  const hasReal = state.sources.some((s) => s.enabled && s.id !== "demo" && s.modes.includes(state.mode));
  const row = (s) => {
      const checked = s.enabled && (s.id !== "demo" || !hasReal);
      return `<label class="src ${s.enabled ? "" : "off"}" title="${esc(s.description)}">
        <input type="checkbox" name="sources" value="${s.id}" form="search" ${checked ? "checked" : ""} ${s.enabled ? "" : "disabled"}>
        <span>${esc(s.name)}${s.enabled ? "" : `<small>нужен ключ: ${esc(s.needs)}</small>`}</span></label>`;
  };
  const list = state.sources.filter((s) => s.modes.includes(state.mode));
  const on = list.filter((s) => s.enabled), off = list.filter((s) => !s.enabled);
  $("#sources").innerHTML = on.map(row).join("") +
    (off.length ? `<details><summary>Подключить ещё (${off.length}) — нужны ключи</summary>${off.map(row).join("")}</details>` : "");
}

// --- поиск --------------------------------------------------------------
function formParams() {
  const f = new FormData($("#search"));
  const p = new URLSearchParams();
  p.set("mode", state.mode);
  for (const [k, v] of f.entries()) {
    if (v === "" || v == null) continue;
    if (state.mode === "long" && ["checkin", "checkout", "guests"].includes(k)) continue;
    p.append(k, v);
  }
  return p;
}

async function doSearch() {
  const p = formParams();
  if (!p.get("location")) return;
  history.replaceState(null, "", "?" + p.toString());
  $("#status").textContent = "Ищу на всех сайтах…";
  $("#list").innerHTML = "";
  $("#stats").innerHTML = "";
  try {
    const r = await fetch("/api/search?" + p.toString());
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    showResult(data);
  } catch (e) {
    $("#status").textContent = "Ошибка: " + e.message;
  }
}
$("#search").addEventListener("submit", (e) => { e.preventDefault(); doSearch(); });
$("#onlyPriced").addEventListener("change", () => state.result && render());
$("#onlyFav").addEventListener("change", () => state.result && render());

function showResult(data) {
  state.result = data;
  const a = data.area;
  if (a) map.fitBounds([[a.bbox[0], a.bbox[1]], [a.bbox[2], a.bbox[3]]], { maxZoom: 13 });
  $("#stats").innerHTML = Object.entries(data.source_stats)
    .map(([id, s]) => `<span class="stat ${s.ok ? "" : "err"}" title="${esc(s.error || "")}">${esc(SOURCE_NAMES[id] || id)}: ${s.ok ? s.count : "ошибка"}</span>`)
    .join("");
  render();
}

function priceText(l, cur) {
  if (l.price_conv == null) return l.price != null ? `${Math.round(l.price)} ${esc(l.currency || "")}` : "цена на сайте";
  const unit = { night: "/ночь", month: "/мес", total: "всего" }[l.price_unit] || "";
  return `${Math.round(l.price_conv).toLocaleString("ru-RU")} ${cur} <small>${unit}</small>`;
}

function render() {
  const d = state.result;
  let items = d.listings;
  if ($("#onlyPriced").checked) items = items.filter((l) => l.price_conv != null);
  if ($("#onlyFav").checked) items = items.filter((l) => favs[l.id]);
  $("#status").textContent = `Найдено ${d.total}${items.length !== d.listings.length ? `, показано ${items.length}` : ""} · ${d.area?.name?.split(",").slice(0, 2).join(",") || ""}`;
  const cur = d.query.currency;
  $("#list").innerHTML = items.map((l) => {
    const img = l.images?.[0];
    const also = l.also_on?.filter((o) => o.source).length
      ? `<div class="also">Также на: ${l.also_on.map((o) => `${esc(SOURCE_NAMES[o.source] || o.source)}${o.price ? ` ${Math.round(o.price)}` : ""}`).join(", ")}</div>` : "";
    return `<article class="card" data-id="${esc(l.id)}">
      <div class="img" style="${img ? `background-image:url('${esc(img)}')` : ""}">${img ? "" : "⌂"}</div>
      <div class="body">
        <h4 title="${esc(l.title)}">${esc(l.title)}</h4>
        <div class="meta">
          ${l.rating ? `<span class="rating">${l.rating.toFixed(1)}</span>` : ""}
          ${l.reviews ? `<span>${l.reviews} отз.</span>` : ""}
          ${l.rooms != null ? `<span>${l.rooms} комн.</span>` : ""}
          ${l.area_m2 ? `<span>${Math.round(l.area_m2)} м²</span>` : ""}
          ${l.distance_km != null ? `<span>${l.distance_km} км до центра</span>` : ""}
        </div>
        ${also}
        <div class="foot">
          <span class="price">${priceText(l, cur)}</span>
          <span>
            <span class="badge">${esc(SOURCE_NAMES[l.source] || l.source)}</span>
            ${l.url ? `<a class="open" href="${esc(l.url)}" target="_blank" rel="noopener">Открыть ↗</a>` : ""}
            <button class="fav ${favs[l.id] ? "on" : ""}" data-fav="${esc(l.id)}" title="В избранное">★</button>
          </span>
        </div>
      </div></article>`;
  }).join("");

  layer.clearLayers();
  state.markers = {};
  items.forEach((l) => {
    if (l.lat == null || l.lon == null) return;
    const label = l.price_conv != null ? Math.round(l.price_conv) : "•";
    const m = L.marker([l.lat, l.lon], {
      icon: L.divIcon({ className: "", html: `<div class="pin">${label}</div>`, iconSize: null }),
    }).bindPopup(`<b>${esc(l.title)}</b><br>${priceText(l, cur)}<br>${l.url ? `<a href="${esc(l.url)}" target="_blank" rel="noopener">Открыть ↗</a>` : ""}`);
    m.on("click", () => highlight(l.id, true));
    m.addTo(layer);
    state.markers[l.id] = m;
  });
}

function highlight(id, scroll) {
  $$(".card.hl").forEach((c) => c.classList.remove("hl"));
  $$(".pin.hl").forEach((p) => p.classList.remove("hl"));
  const card = $(`.card[data-id="${CSS.escape(id)}"]`);
  card?.classList.add("hl");
  if (scroll) card?.scrollIntoView({ behavior: "smooth", block: "center" });
  state.markers[id]?.getElement()?.querySelector(".pin")?.classList.add("hl");
}

$("#list").addEventListener("click", (e) => {
  const fav = e.target.closest("[data-fav]");
  if (fav) {
    const id = fav.dataset.fav;
    if (favs[id]) delete favs[id];
    else favs[id] = state.result.listings.find((l) => l.id === id) || true;
    store.set("fuh-favs", favs);
    fav.classList.toggle("on", !!favs[id]);
    return;
  }
  if (e.target.closest("a")) return;
  const card = e.target.closest(".card");
  if (!card) return;
  const m = state.markers[card.dataset.id];
  if (m) { map.setView(m.getLatLng(), Math.max(map.getZoom(), 15)); m.openPopup(); }
  highlight(card.dataset.id, false);
});

// --- синхронизация формы с запросом от ИИ -------------------------------
function applyQueryToForm(q) {
  if (q.mode !== state.mode) setMode(q.mode);
  const f = $("#search");
  const set = (name, v) => { const el = document.querySelector(`[name="${name}"][form="search"], #search [name="${name}"]`); if (el) el.value = v ?? ""; };
  set("location", q.location); set("checkin", q.checkin); set("checkout", q.checkout);
  set("guests", q.guests); set("price_min", q.price_min); set("price_max", q.price_max);
  set("rooms_min", q.rooms_min ?? ""); set("rating_min", q.rating_min ?? "");
  set("currency", q.currency); set("sort", q.sort);
  $$('[name="property_types"]').forEach((c) => (c.checked = (q.property_types || []).includes(c.value)));
  const p = formParams();
  history.replaceState(null, "", "?" + p.toString());
  void f;
}

// --- ИИ-чат ---------------------------------------------------------------
const chatLog = $("#chatLog");
function linkify(text) {
  return esc(text).replace(/https?:\/\/[^\s)]+/g, (u) => `<a href="${u}" target="_blank" rel="noopener">${u.length > 40 ? u.slice(0, 40) + "…" : u}</a>`)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
}
function addMsg(role, text) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.innerHTML = role === "assistant" ? linkify(text) : esc(text);
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
  return div;
}
function pageContext() {
  const r = state.result;
  if (!r) return { mode: state.mode };
  return {
    mode: state.mode,
    query: r.query,
    shown_top: r.listings.slice(0, 10).map((l, i) => ({ n: i + 1, id: l.id, title: l.title, price: l.price_conv, source: l.source, rooms: l.rooms, rating: l.rating })),
    favorites: Object.values(favs).filter((x) => x && x.title).slice(0, 10).map((l) => ({ title: l.title, price: l.price_conv, source: l.source })),
  };
}
async function sendChat(text) {
  $("#chatSuggest").style.display = "none";
  state.chat.push({ role: "user", content: text });
  addMsg("user", text);
  const typing = addMsg("assistant typing", "Думаю и ищу");
  try {
    const r = await fetch("/api/chat", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ messages: state.chat.slice(-20), context: pageContext() }),
    });
    const data = await r.json();
    typing.remove();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    state.chat.push({ role: "assistant", content: data.reply });
    addMsg("assistant", data.reply || "…");
    if (data.search) {
      applyQueryToForm(data.search.query);
      if (data.search.area?.city) $('#search [name="location"]').value = data.search.area.city;
      showResult(data.search);
      addMsg("note", "↳ результаты показаны в списке и на карте");
    }
  } catch (e) {
    typing.remove();
    addMsg("note", "Ошибка: " + e.message);
    state.chat.pop();
  }
}
$("#chatForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const ta = e.target.msg;
  const text = ta.value.trim();
  if (!text) return;
  ta.value = "";
  sendChat(text);
});
$("#chatForm textarea").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chatForm").requestSubmit(); }
});
$$("#chatSuggest button").forEach((b) => b.addEventListener("click", () => sendChat(b.textContent)));
$("#chatClose").addEventListener("click", () => { $("#chat").classList.remove("open"); document.body.classList.add("chat-closed"); });
$("#chatToggle").addEventListener("click", () => { $("#chat").classList.add("open"); document.body.classList.remove("chat-closed"); });

async function initChat() {
  let provider = "basic";
  try { provider = (await (await fetch("/api/ai/status")).json()).provider; } catch { /* офлайн */ }
  $("#aiMode").textContent = { anthropic: "Claude", openai: "LLM", basic: "простой режим" }[provider] || provider;
  addMsg("assistant", "Привет! Расскажите, что ищете: город, на сколько, бюджет и что важно (тишина, метро, рабочее место, животные…). Я поищу сразу на всех сайтах и помогу выбрать.");
  if (provider === "basic") addMsg("note", "ИИ-ключ не настроен — понимаю только простые запросы. См. README.");
}

// --- старт ------------------------------------------------------------------
(async function init() {
  const p = new URLSearchParams(location.search);
  setMode(p.get("mode") === "long" ? "long" : "short");
  const tomorrow = new Date(Date.now() + 14 * 864e5);
  const after = new Date(tomorrow.getTime() + 2 * 864e5);
  $('[name="checkin"]').value = tomorrow.toISOString().slice(0, 10);
  $('[name="checkout"]').value = after.toISOString().slice(0, 10);
  await loadSources();
  for (const [k, v] of p.entries()) {
    const el = document.querySelector(`#search [name="${k}"], [name="${k}"][form="search"]:not([type=checkbox])`);
    if (el) el.value = v;
  }
  initChat();
  if (p.get("location")) doSearch();
})();
