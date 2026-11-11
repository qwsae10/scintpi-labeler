"use strict";

const S = {
  boot: null, settings: null, queue: [], index: 0, event: null, tab: null,
  confidence: "High", segments: [], figCache: new Map(), openedAt: Date.now(), busy: false,
};
const $ = (q) => document.querySelector(q);
const $$ = (q) => Array.from(document.querySelectorAll(q));
const FILTERS = ["status", "receiver", "date", "constellation", "rx_type", "prn", "min_s4", "elev_mask", "order"];

async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  const p = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(p.error || `HTTP ${r.status}`);
  return p;
}
const post = (url, body) => api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("error", err);
  t.classList.add("show");
  clearTimeout(toast.h);
  toast.h = setTimeout(() => t.classList.remove("show"), err ? 6000 : 2200);
}

function fmtTime(iso) {
  const d = new Date(iso);
  return d.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}
const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "–" : Number(v).toFixed(d));

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
    b.addEventListener("click", () => decide(c));
    box.appendChild(b);
  });
  const seg = $("#seg-class");
  S.boot.classes.forEach((c) => seg.add(new Option(c, c)));
  if (S.boot.is_reviewer) {
    $("#f-status").add(new Option("Review queue (flagged by labelers)", "review_queue"));
  }
  S.boot.classes.forEach((c) => $("#f-status").add(new Option(`My ${c} labels`, c)));
}

function buildTabs() {
  const nav = $("#tabs");
  nav.innerHTML = "";
  const tabs = S.boot.tab_order.filter((t) => S.settings.tabs[t]);
  tabs.forEach((t, i) => {
    const b = document.createElement("button");
    b.className = "tab";
    b.dataset.tab = t;
    b.innerHTML = `${S.boot.tab_titles[t]} <span class="muted small">⇧${i + 1}</span>`;
    b.addEventListener("click", () => showTab(t));
    nav.appendChild(b);
  });
  if (!tabs.includes(S.tab)) S.tab = tabs.includes(S.settings.default_tab) ? S.settings.default_tab : tabs[0];
  markTab();
}

function markTab() {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === S.tab));
  $("#sky-hours-wrap").classList.toggle("hidden", S.tab !== "skyplot");
  $("#pad").closest("label").classList.toggle("hidden", ["skyplot", "indices", "network"].includes(S.tab));
}

/* ------------------------------------------------------------------ queue */

function query() {
  const p = new URLSearchParams();
  FILTERS.forEach((f) => {
    const v = $(`#f-${f}`).value.trim();
    if (v) p.set(f, v);
  });
  return p;
}

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
  $("#queue-count").textContent = p.count.toLocaleString();
  const list = $("#queue-list");
  list.innerHTML = "";
  S.queue.slice(0, 300).forEach((e, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<b>${e.receiver}</b> ${e.prn} <span class="muted">${e.start_utc.slice(0, 16).replace("T", " ")}</span> <span class="s4">${num(e.peak_s4)}</span>`;
    li.addEventListener("click", () => { S.index = i; loadEvent(); });
    list.appendChild(li);
  });
  if (!S.queue.length) {
    S.event = null;
    $("#ev-title").textContent = "No events match";
    $("#ev-time").textContent = "";
    $("#ev-facts").innerHTML = "";
    $("#ev-pos").textContent = "";
    Plotly.purge("plot");
    showMsg($("#f-status").value === "unlabeled" ? "Nothing left to label with these filters. Well done!" : "No events match these filters.");
    return;
  }
  const k = keepId ? S.queue.findIndex((e) => e.event_id === keepId) : -1;
  S.index = k >= 0 ? k : Math.min(S.index, S.queue.length - 1);
  await loadEvent();
}

function highlightQueue() {
  $$("#queue-list li").forEach((li, i) => li.classList.toggle("current", i === S.index));
  const cur = $$("#queue-list li")[S.index];
  if (cur) cur.scrollIntoView({ block: "nearest" });
}

/* ------------------------------------------------------------------ event */

async function loadEvent() {
  const q = S.queue[S.index];
  if (!q) return;
  S.figCache.clear();
  const ev = await api(`/api/event/${encodeURIComponent(q.event_id)}`);
  S.event = ev;
  S.openedAt = Date.now();
  $("#ev-title").textContent = `${ev.receiver} · ${ev.prn}`;
  $("#ev-time").textContent = `${fmtTime(ev.start_utc)} – ${ev.end_utc.slice(11, 16)} · ${ev.n_minutes} min · local ${num(ev.local_hour, 1)} h`;
  $("#ev-facts").innerHTML = [
    ["Peak S4", num(ev.peak_s4)], ["Other freq", num(ev.peak_s4_other)],
    ["Elevation", `${Math.round(ev.min_elev)}–${Math.round(ev.max_elev)}°`], ["Azimuth", `${Math.round(ev.mean_azim)}°`],
    ["Type", ev.rx_type.toUpperCase()], ["Day", ev.day_reason || "–"],
  ].map(([k, v]) => `<span><em>${k}</em>${v}</span>`).join("");
  $("#ev-pos").textContent = `${S.index + 1} / ${S.queue.length}`;
  $("#prev").disabled = S.index === 0;
  $("#next").disabled = S.index >= S.queue.length - 1;
  const d = ev.decision || {};
  S.segments = d.segments ? [...d.segments] : [];
  setConfidence(d.confidence || "High");
  $("#for-review").checked = !!d.for_review;
  $("#notes").value = d.notes || "";
  $$(".label-btn").forEach((b) => b.classList.toggle("active", d.action === "label" && b.dataset.label === d.label));
  $("#saved-line").textContent = d.time_utc
    ? (d.action === "skip" ? `Skipped ${d.time_utc.slice(0, 16)}` : `Saved as ${d.label} (${d.confidence}) ${d.time_utc.slice(0, 16)} UTC`)
    : "";
  renderSegments();
  renderReviewer(ev);
  highlightQueue();
  await showTab(S.tab);
}

function renderReviewer(ev) {
  const panel = $("#review-panel");
  if (!S.boot.is_reviewer || !ev.all_decisions || !ev.all_decisions.length) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  $("#others").innerHTML = ev.all_decisions.map((r) => `<div class="other"><b>${r.user}</b>: ${r.action === "skip" ? "skipped" : `${r.label} (${r.confidence})`}${r.for_review ? " · <b>for review</b>" : ""}${r.notes ? ` · “${r.notes}”` : ""}</div>`).join("")
    + `<div class="label-row">${S.boot.classes.map((c) => `<button class="btn ghost final" data-final="${c}">Final: ${c}</button>`).join("")}</div>`;
  $$(".final").forEach((b) => b.addEventListener("click", async () => {
    await post(`/api/event/${encodeURIComponent(ev.event_id)}/review`, { final_label: b.dataset.final, notes: $("#notes").value });
    toast(`Final label: ${b.dataset.final}`);
    await loadSummary();
    if ($("#f-status").value === "review_queue") await loadQueue(); else move(1);
  }));
}

function showMsg(text) {
  const m = $("#plot-msg");
  m.textContent = text || "";
  m.classList.toggle("hidden", !text);
}

async function showTab(tab) {
  if (!tab) return;
  S.tab = tab;
  markTab();
  if (!S.event) return;
  const p = new URLSearchParams();
  if ($("#pad").value) p.set("pad_minutes", $("#pad").value);
  if (tab === "skyplot" && $("#sky-hours").value) p.set("skyplot_hours", $("#sky-hours").value);
  const key = `${S.event.event_id}|${tab}|${p}`;
  $("#spinner").classList.remove("hidden");
  showMsg("");
  try {
    let res = S.figCache.get(key);
    if (!res) {
      res = await api(`/api/event/${encodeURIComponent(S.event.event_id)}/tab/${tab}?${p}`);
      S.figCache.set(key, res);
    }
    if (S.tab !== tab) return;
    if (res.missing) { Plotly.purge("plot"); showMsg(res.missing); return; }
    await Plotly.react("plot", res.figure.data, res.figure.layout,
      { responsive: true, displaylogo: false, scrollZoom: true, modeBarButtonsToRemove: ["lasso2d", "select2d"] });
    bindZoomReload();
  } catch (e) {
    Plotly.purge("plot");
    showMsg(e.message);
  } finally {
    $("#spinner").classList.add("hidden");
  }
  prefetch();
}

function bindZoomReload() {
  // In the 20 Hz tab, a zoom reloads the visible range at full rate (the full window is an envelope).
  const gd = $("#plot");
  if (gd._zoomBound) return;
  gd._zoomBound = true;
  gd.on("plotly_relayout", (ev) => {
    if (S.tab !== "highrate" || !S.event) return;
    const k0 = Object.keys(ev).find((k) => /^xaxis\d*\.range\[0\]$/.test(k));
    const a = k0 ? ev[k0] : undefined, b = k0 ? ev[k0.replace("[0]", "[1]")] : undefined;
    const auto = Object.keys(ev).some((k) => /^xaxis\d*\.autorange$/.test(k));
    clearTimeout(bindZoomReload.h);
    if (a === undefined && !auto) return;
    bindZoomReload.h = setTimeout(async () => {
      const p = new URLSearchParams();
      if ($("#pad").value) p.set("pad_minutes", $("#pad").value);
      if (a !== undefined) { p.set("t0", String(a).replace(" ", "T")); p.set("t1", String(b).replace(" ", "T")); }
      $("#spinner").classList.remove("hidden");
      try {
        const res = await api(`/api/event/${encodeURIComponent(S.event.event_id)}/tab/highrate?${p}`);
        if (res.figure && S.tab === "highrate") await Plotly.react("plot", res.figure.data, res.figure.layout);
      } catch (e) { toast(e.message, true); } finally { $("#spinner").classList.add("hidden"); }
    }, 450);
  });
}

function prefetch() {
  // Load the next event's first tab in the background so moving on is quick.
  const nxt = S.queue[S.index + 1];
  if (!nxt || prefetch.last === nxt.event_id) return;
  prefetch.last = nxt.event_id;
  fetch(`/api/event/${encodeURIComponent(nxt.event_id)}/tab/${S.tab}`).catch(() => {});
}

/* ------------------------------------------------------------------ labeling */

function setConfidence(c) {
  S.confidence = c;
  $$("[data-conf]").forEach((b) => b.classList.toggle("on", b.dataset.conf === c));
}

async function save(body, message) {
  if (!S.event || S.busy) return false;
  S.busy = true;
  try {
    await post(`/api/event/${encodeURIComponent(S.event.event_id)}/label`, {
      ...body, notes: $("#notes").value, for_review: $("#for-review").checked, segments: S.segments,
      elevation_mask_deg: Number($("#f-elev_mask").value || S.settings.elevation_mask_deg),
      seconds_on_event: Math.round((Date.now() - S.openedAt) / 1000),
    });
    toast(message);
    return true;
  } catch (e) {
    toast(`Not saved: ${e.message}`, true);
    return false;
  } finally {
    S.busy = false;
  }
}

async function decide(label) {
  const id = S.event && S.event.event_id;
  if (!(await save({ action: "label", label, confidence: S.confidence }, `Saved: ${label} (${S.confidence})`))) return;
  await afterSave(id);
}

async function afterSave(id) {
  await loadSummary();
  const status = $("#f-status").value;
  if (status === "unlabeled" || status === "skipped" || status === "review_queue") {
    await loadQueue();  // the saved event drops out; the same index is now the next event
  } else {
    S.event.decision = { action: "label" };
    if (S.index < S.queue.length - 1) { S.index += 1; await loadEvent(); } else { await loadQueue(id); }
  }
}

async function move(d) {
  const i = S.index + d;
  if (i < 0 || i >= S.queue.length) return;
  S.index = i;
  await loadEvent();
}

/* ------------------------------------------------------------------ segments */

function renderSegments() {
  $("#seg-count").textContent = S.segments.length ? `(${S.segments.length})` : "";
  $("#segments-box").open = S.segments.length > 0;
  $("#seg-list").innerHTML = S.segments.map((s, i) => `<li>${s.start.slice(11, 16)}–${s.end.slice(11, 16)} UTC → <b>${s.label}</b> <button class="link" data-del="${i}">remove</button></li>`).join("");
  $$("[data-del]").forEach((b) => b.addEventListener("click", () => { S.segments.splice(Number(b.dataset.del), 1); renderSegments(); }));
}

function addSegment() {
  const gd = $("#plot");
  const xr = gd.layout && gd.layout.xaxis && gd.layout.xaxis.range;
  if (!xr || S.tab === "skyplot" || S.tab === "indices") { toast("Zoom a time plot first", true); return; }
  const floor = (v) => { const d = new Date(String(v).replace(" ", "T") + (String(v).endsWith("Z") ? "" : "Z")); d.setUTCSeconds(0, 0); return d; };
  const a = floor(xr[0]);
  const b = floor(xr[1]); b.setUTCMinutes(b.getUTCMinutes() + 1);
  const evS = new Date(S.event.start_utc), evE = new Date(S.event.end_utc);
  const start = new Date(Math.max(a, evS)), end = new Date(Math.min(b, evE));
  if (end <= start) { toast("The zoomed range does not overlap the shaded event", true); return; }
  S.segments.push({ start: start.toISOString(), end: end.toISOString(), label: $("#seg-class").value });
  renderSegments();
  toast("Range added. It is saved when you press a label key.");
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
  $("#settings").showModal();
}

async function saveSettings() {
  const tabs = {};
  $$("#set-tabs [data-tab]").forEach((c) => { tabs[c.dataset.tab] = c.checked; });
  if (!Object.values(tabs).some(Boolean)) { toast("Keep at least one tab", true); return; }
  const body = { tabs };
  ["default_tab", "queue_order"].forEach((k) => { body[k] = $(`#set-${k}`).value; });
  ["pad_minutes", "comparison_satellites", "elevation_mask_deg", "highrate_decimate_hz"].forEach((k) => { body[k] = Number($(`#set-${k}`).value); });
  S.settings = await post("/api/settings", body);
  $("#pad").value = S.settings.pad_minutes;
  $("#f-elev_mask").value = S.settings.elevation_mask_deg;
  buildTabs();
  S.figCache.clear();
  toast("Settings saved");
  await loadSummary();
  await loadQueue(S.event && S.event.event_id);
}

/* ------------------------------------------------------------------ keys */

function bind() {
  $("#filters").addEventListener("submit", async (e) => { e.preventDefault(); S.index = 0; await loadSummary(); await loadQueue(); });
  $("#prev").addEventListener("click", () => move(-1));
  $("#next").addEventListener("click", () => move(1));
  $("#skip").addEventListener("click", async () => { const id = S.event.event_id; if (await save({ action: "skip" }, "Skipped")) await afterSave(id); });
  $("#clear").addEventListener("click", async () => { if (await save({ action: "clear" }, "Label cleared")) { await loadSummary(); await loadEvent(); } });
  $$("[data-conf]").forEach((b) => b.addEventListener("click", () => setConfidence(b.dataset.conf)));
  $("#seg-add").addEventListener("click", addSegment);
  $("#toggle-queue").addEventListener("click", () => document.body.classList.toggle("no-queue"));
  $("#open-settings").addEventListener("click", openSettings);
  $("#open-help").addEventListener("click", () => $("#help").showModal());
  $("#settings").addEventListener("close", () => { if ($("#settings").returnValue === "save") saveSettings(); });
  $("#pad").addEventListener("change", () => showTab(S.tab));
  $("#sky-hours").addEventListener("change", () => showTab(S.tab));
  document.addEventListener("keydown", (e) => {
    const tag = document.activeElement && document.activeElement.tagName;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag) || e.metaKey || e.ctrlKey || e.altKey) {
      if (e.key === "Escape") document.activeElement.blur();
      return;
    }
    if ($("dialog[open]")) return;
    const digit = e.code && e.code.startsWith("Digit") ? Number(e.code.slice(5)) : NaN;
    if (e.shiftKey && digit >= 1) {
      const t = $$(".tab")[digit - 1];
      if (t) { e.preventDefault(); showTab(t.dataset.tab); }
      return;
    }
    if (!e.shiftKey && digit >= 1 && digit <= S.boot.classes.length) { e.preventDefault(); decide(S.boot.classes[digit - 1]); return; }
    const k = e.key.toLowerCase();
    if (k === "h") setConfidence("High");
    else if (k === "l") setConfidence("Low");
    else if (k === "r") $("#for-review").checked = !$("#for-review").checked;
    else if (k === "s") $("#skip").click();
    else if (k === "q") document.body.classList.toggle("no-queue");
    else if (k === "?") $("#help").showModal();
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
    fill("#f-date", S.boot.dates);
    fill("#f-constellation", S.boot.constellations);
    $("#pad").value = S.settings.pad_minutes;
    $("#f-elev_mask").value = S.settings.elevation_mask_deg;
    buildLabelButtons();
    buildTabs();
    bind();
    await loadSummary();
    await loadQueue();
  } catch (e) {
    toast(e.message, true);
    $("#ev-title").textContent = "Could not start: " + e.message;
  }
}
init();
