"use strict";

/* State. The queue is fetched when filters are applied and then kept: a labeled event stays in it
   (marked done), so ← goes back to it, and moving on needs no request. Decisions are mirrored here
   and written to the server in the background (saveQ). */
const S = {
  boot: null, settings: null, queue: [], index: 0, tab: null, lastTab: null, confidence: "High",
  decisions: new Map(),   // event_id -> my latest decision (local mirror of labels/<user>.jsonl)
  drafts: new Map(),      // event_id -> segments [{start, end, label}] (saved with the event label)
  history: [],            // event ids labeled this session, for U
  home: null, view: null, // the event's full window and the shown time window (UTC ms), shared by TIME_TABS
  rendered: null, yRendered: null, yMan: new Map(),  // ranges the plot was drawn with; y set by hand
  fig: null,              // what is drawn: {tab, time, meta, base (the figure's own shapes)}
  openedAt: null, selSeg: null, pendingSeg: null, draft: null,
  days: {},               // UTC date -> {n, labeled, skipped}: my progress per day under the filters
  cal: { from: null, to: null, anchor: null },  // the days the queue is limited to (null = all)
};
const $ = (q) => document.querySelector(q);
const $$ = (q) => Array.from(document.querySelectorAll(q));
const gd = document.getElementById("plot");
/* The sky maps (4 polar WebGL plots, ~30k points each) get their own plot that stays drawn while
   hidden: switching tabs then shows or hides it instead of tearing down and rebuilding both kinds
   of plot. It is redrawn only for another event or span. The time plot is skipped the same way
   when you come back to the tab it already shows. */
const sky = document.getElementById("sky");
const figIds = new WeakMap();
let figSeq = 0;
const figId = (fig) => { if (!figIds.has(fig)) figIds.set(fig, ++figSeq); return figIds.get(fig); };
const mainSig = (fig, tab) => `${figId(fig)}|${tab}|${S.view}|${JSON.stringify([...S.yMan])}`;

function showPane(which) {
  const el = which === "sky" ? sky : gd;
  gd.classList.toggle("hidden", which !== "plot");
  sky.classList.toggle("hidden", which !== "sky");
  // resized while hidden (window, queue toggle): fit it now that it shows
  if (el.data && el._shownW !== undefined && el.clientWidth !== el._shownW) Plotly.Plots.resize(el);
  el._shownW = el.clientWidth;
}
function purgePlots() {
  Plotly.purge(gd);
  Plotly.purge(sky);
  S.main = null;
  S.skyId = null;
  S.fig = null;
}
const FILTERS = ["status", "receiver", "constellation", "rx_type", "prn", "min_s4", "elev_mask", "order"];
const TIME_TABS = new Set(["overview", "highrate", "satellites", "sidereal"]);
const MIN = 60000;

async function api(url, opts = {}, onResponse = null) {
  const r = await fetch(url, opts);
  if (onResponse) onResponse(r);
  const p = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw Object.assign(new Error(p.error || `HTTP ${r.status}`), { status: r.status });
  return p;
}
const post = (url, body) => api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const enc = encodeURIComponent;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("error", err);
  t.classList.add("show");
  clearTimeout(toast.h);
  toast.h = setTimeout(() => t.classList.remove("show"), err ? 6000 : 2200);
}

function fmtTime(iso) {
  return new Date(iso).toISOString().replace("T", " ").slice(0, 16) + " UTC";
}
const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "–" : Number(v).toFixed(d));
/* Plotly's date axes read naive strings as UTC and ignore any offset: plot x <-> UTC ms */
const iso = (ms) => new Date(Math.round(ms)).toISOString().slice(0, 23);
const parseUTC = (s) => { s = String(s).replace(" ", "T"); return Date.parse(/Z|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z"); };
const hm = (ms) => new Date(ms).toISOString().slice(11, 16);
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const classColor = (label) => css(`--c${Math.max(0, S.boot.classes.indexOf(label))}`);

/* ------------------------------------------------------------------ setup */

function fill(sel, values) {
  const s = $(sel);
  values.forEach((v) => s.add(new Option(v, v)));
}

function buildLabelButtons() {
  const box = $("#label-buttons");
  box.innerHTML = "";
  S.boot.classes.forEach((c, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `label-btn c${i}`;
    b.dataset.label = c;
    b.innerHTML = `<kbd>${i + 1}</kbd> ${c}`;
    b.addEventListener("click", () => digit(i + 1));
    box.appendChild(b);
  });
  if (S.boot.is_reviewer) $("#f-status").add(new Option("Review queue (flagged by labelers)", "review_queue"));
  S.boot.classes.forEach((c) => $("#f-status").add(new Option(`My ${c} labels`, c)));
}

const enabledTabs = () => S.boot.tab_order.filter((t) => S.settings.tabs[t]);

function buildTabs() {
  const nav = $("#tabs");
  nav.innerHTML = "";
  const tabs = enabledTabs();
  tabs.forEach((t, i) => {
    const b = document.createElement("button");
    b.className = "tab";
    b.dataset.tab = t;
    b.innerHTML = `${S.boot.tab_titles[t]} <span class="muted small">⇧${i + 1}</span>`;
    b.addEventListener("click", () => showTab(t));
    nav.appendChild(b);
  });
  if (!tabs.includes(S.tab)) S.tab = tabs.includes(S.settings.default_tab) ? S.settings.default_tab : tabs[0];
  if (!tabs.includes(S.lastTab)) S.lastTab = null;
  markTab();
}

function markTab() {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === S.tab));
  $("#sky-hours-wrap").classList.toggle("hidden", S.tab !== "skyplot");
  $("#pad").closest("label").classList.toggle("hidden", ["skyplot", "indices", "network"].includes(S.tab));
  $("#gesture-hint").classList.toggle("hidden", !TIME_TABS.has(S.tab));
}

function cycleTab(d) {
  const tabs = enabledTabs();
  showTab(tabs[(tabs.indexOf(S.tab) + d + tabs.length) % tabs.length]);
}

/* ------------------------------------------------------------------ queue */

function query() {
  const p = new URLSearchParams();
  FILTERS.forEach((f) => {
    const v = $(`#f-${f}`).value.trim();
    if (v) p.set(f, v);
  });
  if (S.cal.from) {
    p.set("date_from", S.cal.from);
    p.set("date_to", S.cal.to);
    // a day (or range) is worked through by receiver and time, unless an order is picked in the filters
    if (!p.has("order")) p.set("order", "chronological");
  }
  return p;
}

/* ------------------------------------------------------------------ calendar
   Months of event days, as in the dashboard. A day's colour is how many events are still to do
   there (deeper = more); a tick means every event of the day is decided. Click a day to limit the
   queue to it, ⇧-click another for a range, click it again (or "All days") for every day.
   [ and ] move to the previous/next day with events left. */

const DAY_MS = 86400000;
const ymd = (t) => new Date(t).toISOString().slice(0, 10);
const left = (c) => c.n - c.labeled - c.skipped;

function buildCalendar() {
  const box = $("#cal");
  box.innerHTML = "";
  const dates = S.boot.dates;
  if (!dates.length) return;
  const start = Date.parse(dates[0] + "T00:00:00Z"), end = Date.parse(dates[dates.length - 1] + "T00:00:00Z");
  const fmt = new Intl.DateTimeFormat("en", { month: "short", year: "numeric", timeZone: "UTC" });
  const frag = document.createDocumentFragment();
  const s0 = new Date(start);
  for (let m = new Date(Date.UTC(s0.getUTCFullYear(), s0.getUTCMonth(), 1)); m <= end;
       m = new Date(Date.UTC(m.getUTCFullYear(), m.getUTCMonth() + 1, 1))) {
    const sec = document.createElement("div");
    sec.className = "month";
    sec.innerHTML = `<h3>${fmt.format(m)}</h3>`;
    const g = document.createElement("div");
    g.className = "grid7";
    g.innerHTML = [..."SMTWTFS"].map((w) => `<div class="wd">${w}</div>`).join("") + "<div></div>".repeat(m.getUTCDay());
    const n = new Date(Date.UTC(m.getUTCFullYear(), m.getUTCMonth() + 1, 0)).getUTCDate();
    for (let d = 1; d <= n; d++) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "day";
      b.textContent = d;
      b.dataset.date = ymd(Date.UTC(m.getUTCFullYear(), m.getUTCMonth(), d));
      b.disabled = true;
      g.appendChild(b);
    }
    sec.appendChild(g);
    frag.appendChild(sec);
  }
  box.appendChild(frag);
  box.addEventListener("click", (e) => {
    const b = e.target.closest(".day");
    if (!b || b.disabled) return;
    const d = b.dataset.date;
    if (e.shiftKey && S.cal.anchor) { const r = [S.cal.anchor, d].sort(); selectDays(r[0], r[1], false); }
    else if (S.cal.from === d && S.cal.to === d) selectDays(null, null);
    else selectDays(d, d);
  });
}

async function loadDays() {
  const p = query();
  ["status", "order", "date_from", "date_to"].forEach((k) => p.delete(k));
  S.days = await api(`/api/days?${p}`);
  paintCalendar();
}

function paintDay(b) {
  const c = S.days[b.dataset.date];
  b.className = "day";
  b.textContent = Number(b.dataset.date.slice(8));
  b.disabled = !c;
  if (!c) { b.title = ""; return; }
  const r = left(c), done = c.labeled + c.skipped;
  if (r === 0) b.classList.add("done");
  else b.classList.add(`l${Math.max(1, Math.ceil((4 * Math.log1p(r)) / Math.log1p(S.calMax || 1)))}`);
  if (done && r) b.insertAdjacentHTML("beforeend", `<span class="bar-done"><i style="width:${(100 * done) / c.n}%"></i></span>`);
  b.title = `${b.dataset.date}: ${c.n} event${c.n === 1 ? "" : "s"}, ${c.labeled} labeled, ${c.skipped} skipped, ${r} left`;
  const sel = S.cal.from && b.dataset.date >= S.cal.from && b.dataset.date <= S.cal.to;
  b.classList.toggle("in", !!sel);
  b.classList.toggle("cur", !!current() && current().date === b.dataset.date);
}

function paintCalendar() {
  S.calMax = Math.max(1, ...Object.values(S.days).map(left));
  $$("#cal .day").forEach(paintDay);
  renderCalSel();
}

function repaintDay(date) {
  const b = date && $(`#cal .day[data-date="${date}"]`);
  if (b) paintDay(b);
}

function renderCalSel() {
  const { from, to } = S.cal;
  const short = (d) => new Date(d + "T00:00:00Z").toLocaleDateString("en", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
  const byTime = from && !$("#f-order").value ? ", by receiver and time" : "";
  $("#cal-sel-text").textContent = (!from ? "events, all days" : from === to ? `events on ${short(from)}` : `events, ${short(from)} – ${short(to)}`) + byTime;
  $("#cal-all").classList.toggle("hidden", !from);
}

async function selectDays(from, to, setAnchor = true) {
  S.cal.from = from;
  S.cal.to = to;
  if (setAnchor) S.cal.anchor = from;
  paintCalendar();
  S.index = 0;
  await loadQueue();
}

/* the previous/next day (d = -1/+1) from the selected or current one that has events (left to do, in the to-do view) */
function stepDay(d) {
  const ref = S.cal.from ? (d > 0 ? S.cal.to : S.cal.from) : current() ? current().date : null;
  const dates = Object.keys(S.days).filter((k) => (unlabeledView() ? left(S.days[k]) > 0 : S.days[k].n > 0)).sort();
  if (!dates.length) return;
  let pick;
  if (!ref) pick = d > 0 ? dates[0] : dates[dates.length - 1];
  else pick = d > 0 ? dates.find((k) => k > ref) : [...dates].reverse().find((k) => k < ref);
  if (!pick) { toast(d > 0 ? "No later day with events left" : "No earlier day with events left"); return; }
  selectDays(pick, pick);
  const b = $(`#cal .day[data-date="${pick}"]`);
  if (b) b.scrollIntoView({ block: "nearest" });
}

/* the day counts follow my decisions without asking the server */
function countDecision(q, before, after) {
  const c = q && S.days[q.date];
  if (!c || before === after) return;
  if (before === "label") c.labeled--; else if (before === "skip") c.skipped--;
  if (after === "label") c.labeled++; else if (after === "skip") c.skipped++;
  repaintDay(q.date);
}

function renderFilterSum() {
  const parts = [];
  FILTERS.forEach((f) => {
    const el = $(`#f-${f}`), v = el.value.trim();
    if (!v || f === "elev_mask" || f === "order") return;
    if (f === "status" && v === "unlabeled") return;
    parts.push(f === "min_s4" ? `S4 ≥ ${v}` : el.tagName === "SELECT" ? el.selectedOptions[0].textContent : v);
  });
  $("#filter-sum").textContent = parts.length ? `· ${parts.join(" · ")}` : "";
}

let summaryTimer = 0;
const summarySoon = () => { clearTimeout(summaryTimer); summaryTimer = setTimeout(() => loadSummary().catch(() => {}), 400); };
async function loadSummary() {
  const p = new URLSearchParams();
  if ($("#f-elev_mask").value) p.set("elev_mask", $("#f-elev_mask").value);
  const s = await api(`/api/summary?${p}`);
  const done = s.labeled + s.skipped;
  const pct = s.total ? Math.round((100 * s.labeled) / s.total) : 0;
  $("#progress-percent").textContent = `${pct}%`;
  $("#progress-fill").style.width = `${pct}%`;
  $("#progress-text").textContent = `${s.labeled.toLocaleString()} labeled · ${s.skipped} skipped · ${(s.total - done).toLocaleString()} left`;
  $("#class-counts").innerHTML = S.boot.classes.map((c, i) => `<span class="dot c${i}"></span>${c} ${s.by_class[c] || 0}`).join(" · ")
    + (S.boot.is_reviewer ? ` · <b>${s.review_queue}</b> awaiting review` : "");
}

async function loadQueue(keepId = null) {
  const p = await api(`/api/queue?${query()}`);
  S.queue = p.events;
  const pending = new Set(saveQ.map((j) => j.id));
  for (const e of S.queue) {  // the server's decision, unless a newer one of ours is still being written
    if (pending.has(e.event_id)) continue;
    if (e.decision) S.decisions.set(e.event_id, e.decision); else S.decisions.delete(e.event_id);
  }
  $("#queue-count").textContent = p.count.toLocaleString();
  const list = $("#queue-list");
  list.innerHTML = "";
  S.queue.slice(0, 300).forEach((e, i) => {
    const li = document.createElement("li");
    li.dataset.id = e.event_id;
    li.innerHTML = `<span class="dot"></span><b>${e.receiver}</b> ${e.prn} <span class="muted">${e.start_utc.slice(0, 16).replace("T", " ")}</span> <span class="s4">${num(e.peak_s4)}</span>`;
    li.addEventListener("click", () => { S.index = i; loadEvent(); });
    list.appendChild(li);
    markQueueItem(e.event_id);
  });
  if (!S.queue.length) {
    showEmpty($("#f-status").value === "unlabeled"
      ? (S.cal.from ? "Nothing left to label on these days. ] goes to the next day with events left." : "Nothing left to label with these filters. Well done!")
      : "No events match these filters.");
    return;
  }
  const k = keepId ? S.queue.findIndex((e) => e.event_id === keepId) : -1;
  S.index = k >= 0 ? k : Math.min(S.index, S.queue.length - 1);
  loadEvent();
}

function markQueueItem(id) {
  const li = $(`#queue-list li[data-id="${CSS.escape(id)}"]`);
  if (!li) return;
  const d = S.decisions.get(id), dot = li.querySelector(".dot");
  li.classList.toggle("done", !!d);
  dot.classList.toggle("skipped", !!d && d.action === "skip");
  dot.style.background = d && d.action === "label" ? classColor(d.label) : "";
}

function highlightQueue() {
  $$("#queue-list li").forEach((li, i) => li.classList.toggle("current", i === S.index));
  const cur = $$("#queue-list li")[S.index];
  if (cur) cur.scrollIntoView({ block: "nearest" });
}

const current = () => S.queue[S.index];
const decided = (id) => S.decisions.has(id);
const unlabeledView = () => $("#f-status").value === "unlabeled";

/* the next k events to show after index i: in the to-do view the undecided ones, else the following ones */
function upcoming(i, k) {
  const out = [];
  for (let j = i + 1; j < S.queue.length && out.length < k; j++) {
    if (!unlabeledView() || !decided(S.queue[j].event_id)) out.push(j);
  }
  return out;
}

function showEmpty(text) {
  S.index = S.queue.length;
  S.fig = null;
  $("#ev-title").textContent = S.queue.length ? "All done here" : "No events match";
  $("#ev-time").textContent = "";
  $("#ev-facts").innerHTML = "";
  $("#ev-pos").textContent = S.queue.length ? `${S.queue.length} / ${S.queue.length}` : "";
  $("#prev").disabled = !S.queue.length;
  $("#next").disabled = true;
  $("#saved-line").textContent = "";
  purgePlots();
  showPane("plot");
  showMsg(text);
  highlightQueue();
}

/* ------------------------------------------------------------------ event */

function homeWindow(q) {
  const pad = Number($("#pad").value || S.settings.pad_minutes) * MIN;
  return [parseUTC(q.start_utc) - pad, parseUTC(q.end_utc) + pad];
}

function loadEvent() {
  const q = current();
  if (!q) return;
  S.home = homeWindow(q);
  S.view = S.home.slice();
  S.yMan.clear();
  S.selSeg = S.pendingSeg = S.draft = null;
  S.openedAt = null;
  $("#ev-title").textContent = `${q.receiver} · ${q.prn}`;
  $("#ev-time").textContent = `${fmtTime(q.start_utc)} – ${q.end_utc.slice(11, 16)} · ${q.n_minutes} min · local ${num(q.local_hour, 1)} h`;
  $("#ev-facts").innerHTML = [
    ["Peak S4", num(q.peak_s4)], ["Other freq", num(q.peak_s4_other)],
    ["Elevation", `${Math.round(q.min_elev)}–${Math.round(q.max_elev)}°`], ["Azimuth", `${Math.round(q.mean_azim)}°`],
    ["Type", q.rx_type.toUpperCase()], ["Day", q.day_reason || "–"],
  ].map(([k, v]) => `<span><em>${k}</em>${v}</span>`).join("");
  $("#ev-pos").textContent = `${S.index + 1} / ${S.queue.length}`;
  $("#prev").disabled = S.index === 0;
  $("#next").disabled = S.index >= S.queue.length - 1;
  const d = S.decisions.get(q.event_id) || {};
  if (!S.drafts.has(q.event_id)) S.drafts.set(q.event_id, (d.segments || []).map((s) => ({ ...s })));
  setConfidence(d.confidence || "High");
  $("#for-review").checked = !!d.for_review;
  $("#notes").value = d.notes || "";
  renderDecision();
  renderSegments();
  $("#review-panel").classList.add("hidden");
  if (S.boot.is_reviewer) {
    const id = q.event_id;
    api(`/api/event/${enc(id)}`).then((ev) => { if (current() && current().event_id === id) renderReviewer(ev); }).catch(() => {});
  }
  highlightQueue();
  const was = $("#cal .day.cur");
  if (was) paintDay(was);
  const b = $(`#cal .day[data-date="${q.date}"]`);
  if (b) { paintDay(b); if (!S.cal.from) b.scrollIntoView({ block: "nearest" }); }
  showTab(S.tab);
}

function renderDecision() {
  const q = current();
  if (!q) return;
  const d = S.decisions.get(q.event_id);
  $$(".label-btn").forEach((b) => b.classList.toggle("active", !!d && d.action === "label" && b.dataset.label === d.label));
  const pending = saveQ.some((j) => j.id === q.event_id);
  $("#saved-line").textContent = !d ? "" : (d.action === "skip" ? "Skipped" : `Labeled ${d.label} (${d.confidence})`)
    + (pending ? " · saving…" : d.time_utc ? ` · saved ${d.time_utc.slice(0, 16).replace("T", " ")} UTC` : " · saved");
}

function renderReviewer(ev) {
  const panel = $("#review-panel");
  if (!S.boot.is_reviewer || !ev.all_decisions || !ev.all_decisions.length) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  $("#others").innerHTML = ev.all_decisions.map((r) => `<div class="other"><b>${r.user}</b>: ${r.action === "skip" ? "skipped" : `${r.label} (${r.confidence})`}${r.for_review ? " · <b>for review</b>" : ""}${r.notes ? ` · “${r.notes}”` : ""}</div>`).join("")
    + `<div class="label-row">${S.boot.classes.map((c) => `<button class="btn ghost final" data-final="${c}">Final: ${c}</button>`).join("")}</div>`;
  $$(".final").forEach((b) => b.addEventListener("click", async () => {
    try {
      await post(`/api/event/${enc(ev.event_id)}/review`, { final_label: b.dataset.final, notes: $("#notes").value });
    } catch (e) { toast(`Not saved: ${e.message}`, true); return; }
    toast(`Final label: ${b.dataset.final}`);
    summarySoon();
    if ($("#f-status").value === "review_queue") await loadQueue(); else move(1);
  }));
}

function showMsg(text) {
  const m = $("#plot-msg");
  m.textContent = text || "";
  m.classList.toggle("hidden", !text);
}

/* ------------------------------------------------------------------ figures: cache, prefetch */

/* Figures are cached across events (most recently used kept) and fetched ahead in the
   background: the current event's other tabs, then the next events. Background fetches wait
   while one you are waiting for is running, so they never slow it down. */
const FIG_MAX = 48;
const figs = new Map();  // key -> Promise of the /tab response
let fgActive = 0;
const bgWant = [];
let bgBusy = false;

function figParams(tab, extra) {
  const p = new URLSearchParams();
  if ($("#pad").value) p.set("pad_minutes", $("#pad").value);
  if (tab === "skyplot" && $("#sky-hours").value) p.set("skyplot_hours", $("#sky-hours").value);
  for (const [k, v] of Object.entries(extra || {})) p.set(k, v);
  return p;
}
const figKey = (id, tab, p) => `${id}|${tab}|${p}`;

function fetchFig(id, tab, p) {
  const key = figKey(id, tab, p);
  let hit = figs.get(key);
  if (hit) { figs.delete(key); figs.set(key, hit); return hit; }
  const t0 = performance.now();
  hit = api(`/api/event/${enc(id)}/tab/${tab}?${p}`, {}, (r) => {
    const st = r.headers.get("Server-Timing") || "";
    const ms = (k) => Number((st.match(new RegExp(`${k};dur=(\\d+)`)) || [])[1] || 0);
    timing.set(key, { build: ms("build"), json: ms("json"), fetch: Math.round(performance.now() - t0) });
  });
  hit.catch(() => figs.delete(key));
  figs.set(key, hit);
  while (figs.size > FIG_MAX) figs.delete(figs.keys().next().value);
  return hit;
}

async function getFig(id, tab, extra) {
  fgActive++;
  try { return await fetchFig(id, tab, figParams(tab, extra)); } finally { fgActive--; pumpPrefetch(); }
}

function prefetchPlan() {
  bgWant.length = 0;
  const q = current();
  if (!q) return;
  const tabs = enabledTabs(), at = Math.max(0, tabs.indexOf(S.tab));
  const ring = tabs.slice(at).concat(tabs.slice(0, at));  // from this tab on, in Tab order
  const want = (i, t) => bgWant.push([S.queue[i].event_id, t]);
  ring.forEach((t) => want(S.index, t));
  const next = upcoming(S.index, 2);
  next.forEach((i) => want(i, S.tab));
  if (next.length) ring.forEach((t) => want(next[0], t));
  if (S.lastTab && next[1] !== undefined) want(next[1], S.lastTab);
  pumpPrefetch();
}

async function pumpPrefetch() {
  if (bgBusy || fgActive) return;
  while (bgWant.length && !fgActive) {
    const [id, tab] = bgWant.shift();
    const p = figParams(tab);
    if (figs.has(figKey(id, tab, p))) continue;
    bgBusy = true;
    try { await fetchFig(id, tab, p); } catch { /* shown if you open it */ } finally { bgBusy = false; }
  }
}

/* 20 Hz: the home window is a min/max envelope; zoomed in (or panned past what is loaded) it asks
   for the visible range with one view-width either side, at full rate. */
function hrExtra() {
  const [a, b] = S.view, [h0, h1] = S.home, span = b - a;
  if (span >= (h1 - h0) * 0.98) return null;
  return { t0: iso(Math.max(h0, a - span)), t1: iso(Math.min(h1, b + span)) };
}
function hrStale() {
  const m = S.fig && S.fig.meta;
  if (!m || m.lo_ms === undefined) return false;
  const [a, b] = S.view, [h0, h1] = S.home;
  const lo = m.lo_ms <= h0 + 1000 ? -Infinity : m.lo_ms, hi = m.hi_ms >= h1 - 1000 ? Infinity : m.hi_ms;
  if (a < lo || b > hi) return true;
  return m.reduced && b - a < (m.hi_ms - m.lo_ms) / 2;
}

let showSeq = 0;
async function showTab(tab) {
  if (!tab) return;
  if (tab !== S.tab) { S.lastTab = S.tab; S.tab = tab; }
  markTab();
  const q = current();
  if (!q) return;
  const seq = ++showSeq, id = q.event_id, began = performance.now();
  const spin = setTimeout(() => $("#spinner").classList.remove("hidden"), 150);
  let res;
  try {
    res = await getFig(id, tab, tab === "highrate" ? hrExtra() : null);
  } catch (e) {
    if (seq === showSeq) { showPane("plot"); Plotly.purge(gd); S.main = S.fig = null; showMsg(e.message); }
    return;
  } finally {
    clearTimeout(spin);
    if (seq === showSeq) $("#spinner").classList.add("hidden");
  }
  if (seq !== showSeq) return;
  if (res.missing) { showPane("plot"); Plotly.purge(gd); S.main = S.fig = null; showMsg(res.missing); prefetchPlan(); return; }
  showMsg("");
  if (tab === "skyplot") {
    showPane("sky");
    S.fig = { tab, time: false, meta: {} };
    if (S.skyId !== figId(res.figure)) {
      delete res.figure.layout.uirevision;
      await Plotly.react(sky, res.figure.data, res.figure.layout, PLOT_CONFIG);
      S.skyId = figId(res.figure);
      sky._shownW = sky.clientWidth;
    }
  } else {
    showPane("plot");
    if (S.main && S.main.sig === mainSig(res.figure, tab)) S.fig = S.main.state;  // still drawn
    else await render(res.figure, tab);
  }
  if (seq !== showSeq) return;
  logTiming(id, tab, began);
  if (!S.openedAt) S.openedAt = Date.now();
  if (TIME_TABS.has(tab)) (self.requestIdleCallback || setTimeout)(() => traceTimes(res.figure));  // ready for the first zoom
  prefetchPlan();
}

/* Where a tab's wait went, in the browser console (shown at the "Verbose"/debug level):
   build = server making the figure, json = server encoding it, fetch = request until the body
   arrived (includes build, json and the transfer), total = from asking for the tab until drawn. */
const timing = new Map();
function logTiming(id, tab, began) {
  const key = [...timing.keys()].reverse().find((k) => k.startsWith(`${id}|${tab}|`));
  const t = key ? timing.get(key) : null;
  console.debug(`[scintlab] ${tab} ${id}: total ${Math.round(performance.now() - began)} ms`
    + (t ? ` (server build ${t.build}, json ${t.json}, fetch ${t.fetch}${t.used ? ", from cache" : ""})` : " (from cache)"));
  if (t) t.used = true;
  if (timing.size > 200) timing.delete(timing.keys().next().value);
}

const PLOT_CONFIG = { responsive: true, displaylogo: false, displayModeBar: false, scrollZoom: false, doubleClick: false };
const yName = (ya) => "yaxis" + ya._id.slice(1);  // layout key of a full axis: y -> yaxis, y3 -> yaxis3
/* A y range set by hand is kept per quantity where the server names one (layout.meta.yq: C/N₀, S4,
   elevation), so it carries across tabs as the shared ranges do; other axes keep theirs per tab. */
const yKey = (tab, ax, meta) => (meta && meta.yq && meta.yq[ax] ? `q:${meta.yq[ax]}` : `${tab}|${ax}`);

/* Only the points within one view-width either side of the view are drawn (as the dashboard
   does): zoomed in on a 40-minute figure, a redraw then handles a few thousand points instead of
   all of them. Long time-x traces get their times parsed once per figure (fig.t). */
const SLICE_MIN = 1500;
function traceTimes(fig) {
  if (!fig.t) {
    fig.t = fig.data.map((tr) => {
      if (tr.type !== "scatter" || !Array.isArray(tr.x) || tr.x.length < SLICE_MIN || typeof tr.x[0] !== "string") return null;
      const t = Float64Array.from(tr.x, parseUTC);
      for (let i = 1; i < t.length; i++) if (!(t[i] >= t[i - 1])) return null;  // not sorted: draw it whole
      return t;
    });
  }
  return fig.t;
}
const lowerBound = (t, v) => { let a = 0, b = t.length; while (a < b) { const m = (a + b) >> 1; if (t[m] < v) a = m + 1; else b = m; } return a; };
function sliceRange() {
  const [a, b] = S.view, [h0, h1] = S.home, span = b - a;
  return span >= (h1 - h0) * 0.98 ? null : [a - span, b + span];
}
function dataFor(fig, range) {
  if (!range) return fig.data;
  const ts = traceTimes(fig);
  return fig.data.map((tr, k) => {
    const t = ts[k];
    if (!t) return tr;
    const i0 = Math.max(0, lowerBound(t, range[0]) - 1), i1 = Math.min(t.length, lowerBound(t, range[1]) + 1);
    const out = { ...tr, x: tr.x.slice(i0, i1), y: tr.y.slice(i0, i1) };
    for (const key of ["customdata", "text", "hovertext"]) if (Array.isArray(tr[key]) && tr[key].length === t.length) out[key] = tr[key].slice(i0, i1);
    return out;
  });
}

async function render(fig, tab) {
  const layout = fig.layout, time = TIME_TABS.has(tab);
  if (!fig.baseShapes) fig.baseShapes = layout.shapes || [];
  delete layout.uirevision;  // ranges come from S.view and S.yMan, not from the last figure
  layout.dragmode = false;
  if (time) {
    // the server's y ranges, kept so a range set by hand can be undone (0 / double-click)
    if (!fig.yServer) fig.yServer = Object.fromEntries(Object.keys(layout).filter((k) => /^yaxis\d*$/.test(k)).map((k) => [k, layout[k].range]));
    for (const k of Object.keys(layout)) {
      if (/^xaxis\d*$/.test(k)) layout[k].range = S.view.map(iso);
      if (/^yaxis\d*$/.test(k)) layout[k].range = S.yMan.get(yKey(tab, k, layout.meta)) || fig.yServer[k];
    }
  }
  layout.shapes = fig.baseShapes.concat(time ? segShapes() : []);
  const range = time ? sliceRange() : null;
  S.fig = { tab, time, meta: layout.meta || {}, base: fig.baseShapes, src: fig, sliced: range };
  clearLive();
  await Plotly.react(gd, dataFor(fig, range), layout, PLOT_CONFIG);
  noteRendered();
}

/* what the plot was drawn with: ranges (for the live transform) and each shape's layer offset
   (measured here, once per draw: reading it mid-gesture forces a layout per shape per frame) */
function noteRendered() {
  const P = gd._fullLayout && gd._fullLayout._plots;
  S.rendered = S.view && S.view.slice();
  S.yRendered = P ? Object.fromEntries(Object.entries(P).map(([id, p]) => [id, p.yaxis.range.slice()])) : null;
  noteShapes();
  if (S.fig && S.fig.src) S.main = { sig: mainSig(S.fig.src, S.fig.tab), state: S.fig };
  gd._shownW = gd.clientWidth;
}
function noteShapes() {
  const L = gd._fullLayout;
  S.shapeEls = [];
  if (!L || !L.shapes) return;
  for (const el of gd.querySelectorAll(".shapelayer path[data-index]")) {
    const sh = L.shapes[+el.dataset.index];
    if (!sh || !(/^x\d*$/.test(sh.xref) || /^y\d*$/.test(sh.yref))) continue;
    const pm = el.parentNode.getCTM(), sm = el.ownerSVGElement.getCTM();
    S.shapeEls.push({ el, xref: sh.xref, yref: sh.yref, ox: pm.e - sm.e, oy: pm.f - sm.f });
  }
}

/* ------------------------------------------------------------------ pan / zoom (as the dashboard) */

/* During a gesture the drawn SVG is moved with a transform, as Plotly's own drag does (plot group
   translate+scale, clip rect counter-transformed), and drawn for real once the gesture pauses. */
function liveTransform(base, to) {
  const L = gd._fullLayout;
  if (!L || !L._plots || !base.x) return;
  const t = {};
  for (const id in L._plots) {
    const p = L._plots[id], xa = p.xaxis, ya = p.yaxis;
    if (!p.plot || !p.clipRect) continue;
    const kx = (base.x[1] - base.x[0]) / (to.x[1] - to.x[0]);
    const tx = (base.x[0] - to.x[0]) / (to.x[1] - to.x[0]) * xa._length;
    let ky = 1, ty = 0;
    const by = base.y && base.y[id], toy = to.y && to.y[id];
    if (by && toy) {
      ky = (by[1] - by[0]) / (toy[1] - toy[0]);
      ty = (toy[1] - by[1]) / (toy[1] - toy[0]) * ya._length;
    }
    p.plot.node().setAttribute("transform", `translate(${xa._offset + tx},${ya._offset + ty}) scale(${kx},${ky})`);
    p.clipRect.node().setAttribute("transform", `translate(${-tx / kx},${-ty / ky}) scale(${1 / kx},${1 / ky})`);
    t[xa._id] = { o: xa._offset, k: kx, d: tx };
    t[ya._id] = { o: ya._offset, k: ky, d: ty };
  }
  // shapes: the event band and marked ranges follow x; threshold lines follow their panel's y.
  // Their layer may itself be offset (ox, oy, from noteShapes), so map through it. Writes only.
  for (const { el, xref, yref, ox, oy } of S.shapeEls || []) {
    const fx = t[xref], fy = t[yref];
    if (!fx && !fy) continue;
    const kx = fx ? fx.k : 1, ky = fy ? fy.k : 1;
    const dx = fx ? fx.o + fx.d - fx.k * fx.o + (fx.k - 1) * ox : 0;
    const dy = fy ? fy.o + fy.d - fy.k * fy.o + (fy.k - 1) * oy : 0;
    el.setAttribute("transform", `translate(${dx},${dy}) scale(${kx},${ky})`);
  }
  gd._live = true;
}
function clearLive() {
  const L = gd._fullLayout;
  if (!gd._live || !L || !L._plots) return;
  for (const id in L._plots) {
    const p = L._plots[id];
    if (!p.plot || !p.clipRect) continue;
    p.plot.node().setAttribute("transform", `translate(${p.xaxis._offset},${p.yaxis._offset})`);
    p.clipRect.node().removeAttribute("transform");
  }
  for (const { el } of S.shapeEls || []) el.removeAttribute("transform");
  gd._live = false;
}
function goLive() {
  const P = gd._fullLayout && gd._fullLayout._plots;
  if (!P || !S.rendered) return;
  const base = { x: S.rendered, y: {} }, to = { x: S.view, y: {} };
  for (const id in P) {
    base.y[id] = S.yRendered && S.yRendered[id];
    to.y[id] = S.yMan.get(yKey(S.tab, yName(P[id].yaxis), S.fig && S.fig.meta)) || base.y[id];
  }
  liveTransform(base, to);
}

let settleTimer = 0;
const settle = () => { clearTimeout(settleTimer); settleTimer = setTimeout(commitView, 120); };
function viewChanged(a, b) {
  const [h0, h1] = S.home, span = Math.min(b - a, h1 - h0);  // stay inside the event's window
  a = Math.min(Math.max(a, h0), h1 - span);
  S.view = [a, a + span];
  goLive();
  settle();
}

/* once the gesture pauses: one real redraw at the new ranges, of the points around the view (the
   20 Hz tab fetches its samples once the envelope is too coarse or runs out) */
async function commitView() {
  if (!S.fig || !S.fig.time || !gd._fullLayout || !gd._fullLayout._plots) return;
  if (S.fig.tab === "highrate" && hrStale()) { showTab("highrate"); return; }
  const want = sliceRange(), have = S.fig.sliced, [a, b] = S.view;
  // re-cut the traces when the view leaves what is drawn, or is much narrower than it (fewer points)
  const recut = !want !== !have || (want && (a < have[0] || b > have[1] || (b - a) * 3 < (have[1] - have[0]) / 2));
  if (recut) { await render(S.fig.src, S.fig.tab); return; }
  const upd = { "xaxis.range": S.view.map(iso) };
  for (const id in gd._fullLayout._plots) {
    const ax = yName(gd._fullLayout._plots[id].yaxis), r = S.yMan.get(yKey(S.fig.tab, ax, S.fig.meta));
    if (r) upd[`${ax}.range`] = r;
  }
  clearLive();
  await Plotly.relayout(gd, upd);
  noteRendered();
}

/* back to the event's whole window and the server's y ranges: redraw from the cached figure */
function resetView() {
  if (!S.home || !S.fig || !S.fig.time) return;
  clearTimeout(settleTimer);
  S.view = S.home.slice();
  S.yMan.clear();
  showTab(S.tab);
}

function plotAtY(clientY) {
  const P = gd._fullLayout && gd._fullLayout._plots, top = gd.getBoundingClientRect().top;
  for (const id in P || {}) {
    const ya = P[id].yaxis, y = clientY - top;
    if (y >= ya._offset && y <= ya._offset + ya._length) return id;
  }
  return null;
}
function applyY(id, dy, zoom, clientY) {
  const p = gd._fullLayout && gd._fullLayout._plots[id];
  if (!p) return;
  const ya = p.yaxis, key = yKey(S.tab, yName(ya), S.fig && S.fig.meta);
  let [lo, hi] = S.yMan.get(key) || ya.range;
  if (zoom) {
    const f = Math.exp(zoom * 0.002), top = gd.getBoundingClientRect().top + ya._offset;
    const c = hi - Math.min(1, Math.max(0, (clientY - top) / ya._length)) * (hi - lo);
    lo = c - (c - lo) * f; hi = c + (hi - c) * f;
  }
  const shift = -dy / ya._length * (hi - lo);  // content follows the fingers, as page scrolling does
  if (!(hi - lo > 0) || !isFinite(lo + hi)) return;
  S.yMan.set(key, [lo + shift, hi + shift]);
  goLive();
  settle();
}

/* Trackpad: sideways swipe pans time, pinch / ⌘-scroll zooms time at the cursor, ⇧-scroll pans the
   panel under the cursor in y and ⌘⇧-scroll zooms it. Plain vertical scrolling scrolls the page:
   each gesture is locked to the direction it starts in, so a vertical scroll that drifts sideways
   stays a page scroll. */
const wheel = { dx: 0, zoom: 0, x: 0, raf: 0, ydy: 0, yzoom: 0, y: 0, plot: null, lock: null, last: 0 };
gd.addEventListener("wheel", (e) => {
  if (!S.fig || !S.fig.time || !S.view || !gd._fullLayout || !gd._fullLayout.xaxis) return;
  const now = performance.now();
  if (now - wheel.last > 200) wheel.lock = null;
  wheel.last = now;
  const px = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? gd.clientWidth : 1;
  if (e.shiftKey) {
    const id = plotAtY(e.clientY);
    if (!id) return;
    e.preventDefault();
    if (id !== wheel.plot) { if (wheel.plot) applyWheel(); wheel.plot = id; }
    // shift can turn vertical scrolling sideways (macOS mouse wheels, Firefox): take the larger component
    const d = (Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX) * px;
    if (e.metaKey || e.ctrlKey) wheel.yzoom += d; else wheel.ydy += d;
    wheel.y = e.clientY;
  } else if (e.metaKey || e.ctrlKey) {  // pinch arrives as ctrl+wheel
    e.preventDefault();
    wheel.zoom += e.deltaY * px;
    wheel.x = e.clientX;
  } else {
    if (!wheel.lock) wheel.lock = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? "x" : "page";
    if (wheel.lock === "page") return;
    e.preventDefault();  // also keeps the browser from taking the swipe as back/forward
    wheel.dx += e.deltaX * px;
  }
  if (!wheel.raf) wheel.raf = requestAnimationFrame(applyWheel);
}, { passive: false });

const MIN_SPAN = 10000;  // zoom in down to 10 s (a few hundred 20 Hz samples)
function applyWheel() {
  if (wheel.raf) cancelAnimationFrame(wheel.raf);
  if (wheel.ydy || wheel.yzoom) applyY(wheel.plot, wheel.ydy, wheel.yzoom, wheel.y);
  Object.assign(wheel, { ydy: 0, yzoom: 0, plot: null });
  const ax = gd._fullLayout && gd._fullLayout.xaxis;
  if (!ax || !S.view) { Object.assign(wheel, { dx: 0, zoom: 0, raf: 0 }); return; }
  const [a, b] = S.view, span = b - a;
  let lo = a, hi = b;
  if (wheel.zoom) {
    const f = Math.exp(wheel.zoom * 0.002);
    const left = gd.getBoundingClientRect().left + ax._offset;
    const c = a + Math.min(1, Math.max(0, (wheel.x - left) / ax._length)) * span;
    lo = c - (c - a) * f; hi = c + (b - c) * f;
    if (hi - lo < MIN_SPAN) { lo = a; hi = b; }
  }
  const shift = wheel.dx / ax._length * (hi - lo);
  Object.assign(wheel, { dx: 0, zoom: 0, x: 0, raf: 0 });
  if (lo === a && hi === b && !shift) return;
  viewChanged(lo + shift, hi + shift);
}

gd.addEventListener("dblclick", () => resetView());

/* ------------------------------------------------------------------ marked ranges (segments) */

const segs = () => (current() ? S.drafts.get(current().event_id) || [] : []);

function rgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}
function segShapes() {
  const out = [], list = segs().map((s, i) => ({ a: parseUTC(s.start), b: parseUTC(s.end), label: s.label, i }));
  if (S.draft) list.push({ a: S.draft[0], b: S.draft[1], label: null, i: -1 });
  for (const { a, b, label, i } of list) {
    const sel = i === S.selSeg || i === S.pendingSeg || i === -1;
    const c = label ? classColor(label) : "#667085";
    out.push({ type: "rect", xref: "x", yref: "paper", x0: iso(a), x1: iso(b), y0: 0, y1: 1, layer: "below",
               fillcolor: rgba(c, sel ? 0.28 : 0.16), line: { width: 0 } });
    for (const x of [a, b]) {
      out.push({ type: "line", xref: "x", yref: "paper", x0: iso(x), x1: iso(x), y0: 0, y1: 1, layer: "below",
                 line: { color: c, width: sel ? 2 : 1, dash: label ? "solid" : "dot" } });
    }
  }
  return out;
}
async function drawShapes() {
  if (!S.fig || !S.fig.time || !gd._fullLayout || !gd._fullLayout.xaxis) return;
  if (gd._live) { clearTimeout(settleTimer); await commitView(); }
  if (!S.fig || !S.fig.time) return;
  await Plotly.relayout(gd, { shapes: S.fig.base.concat(segShapes()) });
  noteShapes();
}

function renderSegments() {
  const list = segs(), q = current();
  const saved = q && (S.decisions.get(q.event_id) || {}).segments;
  const dirty = !!q && JSON.stringify(list) !== JSON.stringify(saved || []);
  $("#seg-count").textContent = list.length || dirty ? `(${list.length}${dirty ? "; not saved yet: press a label key" : ""})` : "";
  if (list.length) $("#segments-box").open = true;
  $("#seg-list").innerHTML = list.map((s, i) => `<li class="${i === S.selSeg || i === S.pendingSeg ? "sel" : ""}">${s.start.slice(11, 16)}–${s.end.slice(11, 16)} UTC → <b>${s.label || `press 1–${S.boot.classes.length}`}</b> <button class="link" data-del="${i}">remove</button></li>`).join("");
  $$("[data-del]").forEach((b) => b.addEventListener("click", () => removeSeg(Number(b.dataset.del))));
}
function removeSeg(i) {
  const list = segs();
  if (i === null || i < 0 || i >= list.length) return;
  list.splice(i, 1);
  S.selSeg = S.pendingSeg = null;
  renderSegments();
  drawShapes();
}
function classifySeg(i, label) {
  segs()[i].label = label;
  S.selSeg = S.pendingSeg = null;
  renderSegments();
  drawShapes();
  toast(`Range → ${label}. Saved with the event label.`);
}

/* x (px from the plot's left) of a pointer inside one of the panels, else null (legends and the
   gaps between panels keep their normal clicks) */
function plotX(e) {
  const L = gd._fullLayout;
  if (!L || !L.xaxis || plotAtY(e.clientY) === null) return null;
  const x = e.clientX - gd.getBoundingClientRect().left;
  return x >= L.xaxis._offset && x <= L.xaxis._offset + L.xaxis._length ? x : null;
}
const xToT = (x) => { const ax = gd._fullLayout.xaxis, [a, b] = S.view; return a + (x - ax._offset) / ax._length * (b - a); };
const tToX = (t) => { const ax = gd._fullLayout.xaxis, [a, b] = S.view; return ax._offset + (t - a) / (b - a) * ax._length; };
const snap = (t) => Math.round(t / MIN) * MIN;

function edgeAt(x) {
  const list = segs();
  const order = list.map((s, i) => i).sort((i, j) => (j === S.selSeg) - (i === S.selSeg));
  for (const i of order) {
    if (Math.abs(tToX(parseUTC(list[i].start)) - x) <= 5) return { i, side: "start" };
    if (Math.abs(tToX(parseUTC(list[i].end)) - x) <= 5) return { i, side: "end" };
  }
  return null;
}
gd.addEventListener("mousemove", (e) => {
  if (e.buttons || !S.fig || !S.fig.time) return;
  const x = plotX(e);
  gd.classList.toggle("edge", x !== null && !e.metaKey && !!edgeAt(x));
});

/* drag = mark a range, drag a range's edge = resize it, ⌘-drag = pan, click = select a range */
gd.addEventListener("mousedown", (e) => {
  if (e.button !== 0 || !S.fig || !S.fig.time || !S.view) return;
  const x = plotX(e);
  if (x === null) return;
  e.preventDefault(); e.stopPropagation();
  const cx = e.clientX, t0 = xToT(x), pan = e.metaKey, v0 = S.view.slice();
  const edge = pan ? null : edgeAt(x);
  if (edge && edge.i !== S.selSeg) { S.selSeg = edge.i; S.pendingSeg = null; renderSegments(); drawShapes(); }
  const q = current(), evS = parseUTC(q.start_utc), evE = parseUTC(q.end_utc);
  let moved = false;
  const move = (ev) => {
    const dx = ev.clientX - cx;
    if (!moved && Math.abs(dx) < 4) return;
    moved = true;
    if (edge) {
      const s = segs()[edge.i], t = Math.min(evE, Math.max(evS, snap(xToT(x + dx))));
      if (edge.side === "start") s.start = new Date(Math.min(t, parseUTC(s.end) - MIN)).toISOString();
      else s.end = new Date(Math.max(t, parseUTC(s.start) + MIN)).toISOString();
      drawShapes();
    } else if (pan) {
      const dt = -dx / gd._fullLayout.xaxis._length * (v0[1] - v0[0]);
      viewChanged(v0[0] + dt, v0[1] + dt);
    } else {
      const t1 = xToT(x + dx);
      S.draft = [Math.min(t0, t1), Math.max(t0, t1)];
      drawShapes();
    }
  };
  const up = () => {
    removeEventListener("mousemove", move);
    removeEventListener("mouseup", up);
    if (edge) { if (moved) renderSegments(); return; }
    if (pan) return;
    const list = segs();
    if (!moved) {  // click: select the range under the cursor, or none (an unclassified one is dropped)
      const hit = list.map((s, i) => i).filter((i) => t0 >= parseUTC(list[i].start) && t0 <= parseUTC(list[i].end)).pop();
      let sel = hit === undefined ? null : hit;
      if (S.pendingSeg !== null && sel !== S.pendingSeg) {
        list.splice(S.pendingSeg, 1);
        if (sel !== null && sel > S.pendingSeg) sel -= 1;
        S.pendingSeg = null;
      }
      S.selSeg = sel;
      renderSegments();
      drawShapes();
      return;
    }
    // whole minutes, inside the event (the event label covers the rest)
    const a = Math.max(evS, Math.floor(S.draft[0] / MIN) * MIN), b = Math.min(evE, Math.ceil(S.draft[1] / MIN) * MIN);
    S.draft = null;
    if (b <= a) { drawShapes(); toast("The range does not overlap the shaded event", true); return; }
    if (S.pendingSeg !== null) list.splice(S.pendingSeg, 1);  // one unclassified range at a time
    list.push({ start: new Date(a).toISOString(), end: new Date(b).toISOString(), label: null });
    S.pendingSeg = list.length - 1;
    S.selSeg = null;
    renderSegments();
    drawShapes();
    toast(`${hm(a)}–${hm(b)}: press 1–${S.boot.classes.length} for its class, Esc to cancel`);
  };
  addEventListener("mousemove", move);
  addEventListener("mouseup", up);
}, true);

/* ------------------------------------------------------------------ labeling: background saves */

/* Each decision goes into saveQ and is written in order (the label file is a log: the last line
   wins). Moving on never waits for it; a failed write is retried, and closing the page while
   something is unsaved asks first. */
const saveQ = [];
let saving = false;

function renderSaveState(err) {
  const el = $("#save-state");
  el.classList.toggle("error", !!err);
  el.textContent = err ? `${saveQ.length} not saved yet, retrying (${err})` : saveQ.length ? `saving ${saveQ.length}…` : "";
}

async function pumpSave() {
  if (saving || !saveQ.length) return;
  saving = true;
  const job = saveQ[0];
  try {
    const res = await post(`/api/event/${enc(job.id)}/label`, job.body);
    saveQ.shift();
    if (res.saved && res.saved.action !== "clear" && !saveQ.some((j) => j.id === job.id)) S.decisions.set(job.id, res.saved);
    renderSaveState();
    summarySoon();
    if (current() && current().event_id === job.id) renderDecision();
  } catch (e) {
    if (e.status >= 400 && e.status < 500) {  // the server refused it: retrying won't help
      saveQ.shift();
      S.decisions.delete(job.id);
      markQueueItem(job.id);
      toast(`Not saved (${job.id}): ${e.message}`, true);
      loadDays().catch(() => {});
      renderSaveState();
    } else {
      job.tries = (job.tries || 0) + 1;
      renderSaveState(e.message);
      await sleep(Math.min(15000, 1000 * 2 ** job.tries));
    }
  } finally {
    saving = false;
    if (saveQ.length) pumpSave();
  }
}
addEventListener("beforeunload", (e) => { if (saveQ.length) { e.preventDefault(); e.returnValue = ""; } });

function record(body, message) {
  const q = current();
  if (!q) return null;
  const id = q.event_id;
  const before = (S.decisions.get(id) || {}).action;
  body = {
    ...body, notes: $("#notes").value, for_review: $("#for-review").checked,
    segments: body.action === "label" ? segs().filter((s) => s.label).map((s) => ({ start: s.start, end: s.end, label: s.label })) : [],
    elevation_mask_deg: Number($("#f-elev_mask").value || S.settings.elevation_mask_deg),
    seconds_on_event: S.openedAt ? Math.round((Date.now() - S.openedAt) / 1000) : null,
  };
  if (body.action === "clear") S.decisions.delete(id);
  else S.decisions.set(id, { ...body, event_id: id });
  if (body.action === "label") {
    S.history = S.history.filter((h) => h !== id).concat(id);
    S.drafts.set(id, body.segments.map((s) => ({ ...s })));
  }
  countDecision(q, before, body.action === "clear" ? undefined : body.action);
  saveQ.push({ id, body });
  renderSaveState();
  pumpSave();
  markQueueItem(id);
  toast(message);
  return id;
}

function decide(label) {
  if (!current()) return;
  if (segs().some((s) => !s.label)) { toast(`Give the marked range a class first (1–${S.boot.classes.length}), or Esc`, true); return; }
  record({ action: "label", label, confidence: S.confidence }, `${label} (${S.confidence})`);
  advance();
}
function skip() {
  if (!current()) return;
  record({ action: "skip" }, "Skipped");
  advance();
}
function clearDecision() {
  if (!current() || !decided(current().event_id)) return;
  record({ action: "clear" }, "Label cleared");
  renderDecision();
  renderSegments();
}

/* number keys: classify the marked/selected range if there is one, else label the event */
function digit(n) {
  const label = S.boot.classes[n - 1];
  if (!label || !current()) return;
  const i = S.pendingSeg !== null ? S.pendingSeg : S.selSeg;
  if (i !== null && segs()[i]) classifySeg(i, label);
  else decide(label);
}

function advance() {
  const next = upcoming(S.index, 1)[0];
  if (next === undefined) {
    if (unlabeledView()) {
      showEmpty(S.cal.from ? "Nothing left to label on these days. ] goes to the next day with events left, ← goes back."
        : "Nothing left to label with these filters. ← goes back.");
      return;
    }
    renderDecision();
    return;
  }
  S.index = next;
  loadEvent();
}

function move(d) {
  const i = S.index + d;
  if (i < 0 || i >= S.queue.length) return;
  S.index = i;
  loadEvent();
}

function backToLast() {
  for (let k = S.history.length - 1; k >= 0; k--) {
    const i = S.queue.findIndex((e) => e.event_id === S.history[k]);
    if (i >= 0) { S.index = i; loadEvent(); return; }
  }
  toast("Nothing labeled yet in this session");
}

function setConfidence(c) {
  S.confidence = c;
  $$("[data-conf]").forEach((b) => b.classList.toggle("on", b.dataset.conf === c));
}

/* ------------------------------------------------------------------ settings */

function openSettings() {
  const st = S.settings;
  $("#set-tabs").innerHTML = S.boot.tab_order.map((t) => [t, S.boot.tab_titles[t]]).map(([t, n]) => `<label class="check"><input type="checkbox" data-tab="${t}" ${st.tabs[t] ? "checked" : ""}/> ${n}</label>`).join("");
  const dt = $("#set-default_tab");
  dt.innerHTML = "";
  S.boot.tab_order.forEach((t) => dt.add(new Option(S.boot.tab_titles[t], t)));
  ["default_tab", "queue_order", "pad_minutes", "comparison_satellites", "elevation_mask_deg", "highrate_decimate_hz"]
    .forEach((k) => { $(`#set-${k}`).value = st[k]; });
  ["show_tau", "show_s4_corrected"].forEach((k) => { $(`#set-${k}`).checked = !!st[k]; });
  $("#settings").showModal();
}

async function saveSettings() {
  const tabs = {};
  $$("#set-tabs [data-tab]").forEach((c) => { tabs[c.dataset.tab] = c.checked; });
  if (!Object.values(tabs).some(Boolean)) { toast("Keep at least one tab", true); return; }
  const body = { tabs };
  ["default_tab", "queue_order"].forEach((k) => { body[k] = $(`#set-${k}`).value; });
  ["pad_minutes", "comparison_satellites", "elevation_mask_deg", "highrate_decimate_hz"].forEach((k) => { body[k] = Number($(`#set-${k}`).value); });
  ["show_tau", "show_s4_corrected"].forEach((k) => { body[k] = $(`#set-${k}`).checked; });
  S.settings = await post("/api/settings", body);
  $("#pad").value = S.settings.pad_minutes;
  $("#f-elev_mask").value = S.settings.elevation_mask_deg;
  buildTabs();
  figs.clear();  // figures depend on the settings
  toast("Settings saved");
  summarySoon();
  loadDays().catch(() => {});
  await loadQueue(current() && current().event_id);
}

/* ------------------------------------------------------------------ keys */

function toggleQueue() {
  document.body.classList.toggle("no-queue");
  setTimeout(() => {  // the shown plot now; the hidden one when it is next shown (showPane)
    const el = S.tab === "skyplot" ? sky : gd;
    if (el.data) { Plotly.Plots.resize(el); el._shownW = el.clientWidth; }
  }, 50);
}

function onEscape() {
  if (S.pendingSeg !== null) { removeSeg(S.pendingSeg); return true; }
  if (S.draft) { S.draft = null; drawShapes(); return true; }
  if (S.selSeg !== null) { S.selSeg = null; renderSegments(); drawShapes(); return true; }
  return false;
}

function bind() {
  $("#filters").addEventListener("submit", async (e) => {
    e.preventDefault();
    document.activeElement.blur();
    S.index = 0;
    summarySoon();
    renderFilterSum();
    loadDays().catch((err) => toast(err.message, true));
    await loadQueue();
  });
  $("#cal-all").addEventListener("click", () => selectDays(null, null));
  $("#prev").addEventListener("click", () => move(-1));
  $("#next").addEventListener("click", () => move(1));
  $("#skip").addEventListener("click", skip);
  $("#clear").addEventListener("click", clearDecision);
  $$("[data-conf]").forEach((b) => b.addEventListener("click", () => setConfidence(b.dataset.conf)));
  $("#toggle-queue").addEventListener("click", toggleQueue);
  $("#open-settings").addEventListener("click", openSettings);
  $("#open-help").addEventListener("click", () => $("#help").showModal());
  $("#settings").addEventListener("close", () => { if ($("#settings").returnValue === "save") saveSettings(); });
  $("#pad").addEventListener("change", () => { if (current()) { S.home = homeWindow(current()); S.view = S.home.slice(); } showTab(S.tab); });
  $("#sky-hours").addEventListener("change", () => showTab(S.tab));
  $("#for-review").addEventListener("change", (e) => e.target.blur());
  // a clicked button keeps focus, and Space/Enter would press it again: give the keys back to the page
  document.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b && !b.closest("dialog")) b.blur(); });
  document.addEventListener("keydown", (e) => {
    const el = document.activeElement, tag = el && el.tagName;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) {
      if (e.key === "Escape" || (e.key === "Enter" && el.id === "notes")) { e.preventDefault(); el.blur(); }
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey || $("dialog[open]")) return;
    if (e.key === "Tab") { e.preventDefault(); cycleTab(e.shiftKey ? -1 : 1); return; }
    const d = e.code && e.code.startsWith("Digit") ? Number(e.code.slice(5)) : NaN;
    if (e.shiftKey && d >= 1) {
      const t = $$(".tab")[d - 1];
      if (t) { e.preventDefault(); showTab(t.dataset.tab); }
      return;
    }
    if (!e.shiftKey && d >= 1 && d <= S.boot.classes.length) { e.preventDefault(); digit(d); return; }
    if (!e.shiftKey && d === 0) { e.preventDefault(); resetView(); return; }
    if (e.code === "Backquote") { e.preventDefault(); if (S.lastTab) showTab(S.lastTab); return; }
    const k = e.key.toLowerCase();
    if (k === "h") setConfidence("High");
    else if (k === "l") setConfidence("Low");
    else if (k === "r") $("#for-review").checked = !$("#for-review").checked;
    else if (k === "s") skip();
    else if (k === "n") $("#notes").focus();
    else if (k === "u") backToLast();
    else if (k === "q") toggleQueue();
    else if (e.key === "]") stepDay(1);
    else if (e.key === "[") stepDay(-1);
    else if (k === "?") $("#help").showModal();
    else if (e.key === "Escape") { if (!onEscape()) return; }
    else if ((e.key === "Delete" || e.key === "Backspace") && S.selSeg !== null) removeSeg(S.selSeg);
    else if (e.key === "ArrowRight") move(1);
    else if (e.key === "ArrowLeft") move(-1);
    else return;
    e.preventDefault();
  });
}

async function init() {
  try {
    S.boot = await api("/api/bootstrap");
    S.settings = S.boot.settings;
    $("#user-name").textContent = S.boot.user + (S.boot.is_reviewer ? " (reviewer)" : "");
    fill("#f-receiver", S.boot.receivers);
    fill("#f-constellation", S.boot.constellations);
    $("#pad").value = S.settings.pad_minutes;
    $("#f-elev_mask").value = S.settings.elevation_mask_deg;
    buildLabelButtons();
    buildTabs();
    bind();
    buildCalendar();
    renderFilterSum();
    loadSummary().catch((e) => toast(e.message, true));
    await loadDays().catch((e) => toast(e.message, true));
    await loadQueue();
  } catch (e) {
    toast(e.message, true);
    $("#ev-title").textContent = "Could not start: " + e.message;
  }
}
init();
