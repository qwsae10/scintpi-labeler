const state = {
  events: [],
  index: 0,
  summary: null,
  loading: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function requestJSON(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.classList.add("visible");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => toast.classList.remove("visible"), 3200);
}

async function loadSummary() {
  state.summary = await requestJSON("/api/summary");
  const { total, labeled, unlabeled, stations, dates } = state.summary;
  const percent = total ? Math.round((labeled / total) * 100) : 0;
  $("#progress-percent").textContent = `${percent}%`;
  $("#progress-fill").style.width = `${percent}%`;
  $("#labeled-count").textContent = labeled.toLocaleString();
  $("#remaining-count").textContent = unlabeled.toLocaleString();
  fillSelect("#station-filter", stations);
  fillSelect("#date-filter", dates);
}

function fillSelect(selector, values) {
  const select = $(selector);
  const current = select.value;
  const existing = new Set(Array.from(select.options).map((option) => option.value));
  values.forEach((value) => {
    if (!existing.has(value)) select.add(new Option(value, value));
  });
  select.value = current;
}

function filterQuery() {
  const params = new URLSearchParams();
  const fields = {
    status: $("#status-filter").value,
    station: $("#station-filter").value,
    date: $("#date-filter").value,
    prn: $("#prn-filter").value.trim(),
    min_s4: $("#min-s4-filter").value,
    max_s4: $("#max-s4-filter").value,
  };
  Object.entries(fields).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  return params.toString();
}

async function loadQueue({ preserveEventId = null } = {}) {
  const payload = await requestJSON(`/api/events?${filterQuery()}`);
  state.events = payload.events;
  $("#queue-count").textContent = payload.count.toLocaleString();
  if (!state.events.length) {
    showEmptyState();
    return;
  }
  const preserved = preserveEventId
    ? state.events.findIndex((event) => event.event_id === preserveEventId)
    : -1;
  state.index = preserved >= 0 ? preserved : Math.min(state.index, state.events.length - 1);
  await loadCurrentEvent();
}

function showEmptyState() {
  $("#empty-state").classList.remove("hidden");
  $(".plot-card").classList.add("hidden");
  $(".review-card").classList.add("hidden");
  $("#event-title").textContent = "No events";
  $("#event-time").textContent = "";
  $("#queue-position").textContent = "";
  $("#event-values").classList.add("hidden");
}

function showWorkspace() {
  $("#empty-state").classList.add("hidden");
  $(".plot-card").classList.remove("hidden");
  $(".review-card").classList.remove("hidden");
  $("#event-values").classList.remove("hidden");
}

async function loadCurrentEvent() {
  if (!state.events.length || state.loading) return;
  state.loading = true;
  showWorkspace();
  $("#plot-loading").classList.remove("hidden");
  setControlsDisabled(true);
  const queued = state.events[state.index];
  updateEventMetadata(queued);
  try {
    const payload = await requestJSON(`/api/events/${encodeURIComponent(queued.event_id)}`);
    state.events[state.index] = payload.event;
    updateEventMetadata(payload.event);
    await Plotly.react("event-plot", payload.figure.data, payload.figure.layout, {
      responsive: true,
      displaylogo: false,
      scrollZoom: true,
      modeBarButtonsToRemove: ["lasso2d", "select2d"],
    });
  } catch (error) {
    showToast(error.message, true);
  } finally {
    $("#plot-loading").classList.add("hidden");
    setControlsDisabled(false);
    state.loading = false;
  }
}

function updateEventMetadata(event) {
  const start = new Date(event.start_utc);
  const end = new Date(event.end_utc);
  $("#queue-position").textContent = `${state.index + 1} of ${state.events.length}`;
  $("#event-title").textContent = `${event.station} ${event.prn}`;
  $("#event-time").textContent = `${formatUTC(start)} to ${formatUTC(end)}`;
  $("#peak-s4-l1").textContent = formatS4(event.peak_s4);
  $("#peak-s4-l2").textContent = formatS4(event.peak_s4_2);
  $("#event-elevation").textContent = formatElevation(
    event.min_elevation_deg,
    event.max_elevation_deg,
  );
  $("#previous-button").disabled = state.index === 0;
  $("#next-button").disabled = state.index >= state.events.length - 1;
  $("#notes").value = event.notes || "";
  const confidence = event.confidence || "Medium";
  const radio = document.querySelector(`input[name="confidence"][value="${confidence}"]`);
  if (radio) radio.checked = true;
  $$(".label-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.label === event.label);
  });
}

function formatS4(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(3) : "n/a";
}

function formatElevation(minimum, maximum) {
  const low = Number(minimum);
  const high = Number(maximum);
  if (!Number.isFinite(low) || !Number.isFinite(high)) return "n/a";
  const roundedLow = Math.round(low);
  const roundedHigh = Math.round(high);
  return roundedLow === roundedHigh ? `${roundedLow}°` : `${roundedLow} to ${roundedHigh}°`;
}

function setQueueOpen(open) {
  $("#queue-drawer").classList.toggle("closed", !open);
  $("#queue-drawer").setAttribute("aria-hidden", String(!open));
  $("#drawer-backdrop").classList.toggle("hidden", !open);
  $("#open-queue").setAttribute("aria-expanded", String(open));
  if (open) $("#close-queue").focus();
}

function formatUTC(date) {
  const formatter = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  });
  return `${formatter.format(date)} UTC`;
}

function setControlsDisabled(disabled) {
  $$(".label-button, #skip-button").forEach((button) => { button.disabled = disabled; });
}

async function saveDecision({ label = null, skipped = false }) {
  if (!state.events.length || state.loading) return;
  const current = state.events[state.index];
  const confidence = document.querySelector('input[name="confidence"]:checked')?.value || "Medium";
  setControlsDisabled(true);
  try {
    const payload = await requestJSON(`/api/events/${encodeURIComponent(current.event_id)}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        state: skipped ? "skipped" : "labeled",
        label,
        confidence,
        notes: $("#notes").value,
      }),
    });
    state.summary = payload.summary;
    const decision = skipped ? "Skipped for later" : `Saved as ${label}`;
    showToast(decision);
    await loadSummary();
    const status = $("#status-filter").value;
    if (status === "unlabeled" || status === "skipped" || status === "labeled" || ["Scintillation", "Multipath", "RFI", "Other"].includes(status)) {
      state.index = Math.min(state.index, Math.max(0, state.events.length - 2));
      await loadQueue();
    } else {
      state.events[state.index] = { ...current, ...payload.review, review_state: payload.review.state };
      if (state.index < state.events.length - 1) state.index += 1;
      await loadCurrentEvent();
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setControlsDisabled(false);
  }
}

async function move(delta) {
  const next = state.index + delta;
  if (next < 0 || next >= state.events.length || state.loading) return;
  state.index = next;
  await loadCurrentEvent();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function bindEvents() {
  $("#filter-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    state.index = 0;
    await loadQueue();
    setQueueOpen(false);
  });
  $("#reset-filters").addEventListener("click", async () => {
    $("#filter-form").reset();
    $("#status-filter").value = "unlabeled";
    state.index = 0;
    await loadQueue();
  });
  $("#open-queue").addEventListener("click", () => setQueueOpen(true));
  $("#close-queue").addEventListener("click", () => setQueueOpen(false));
  $("#drawer-backdrop").addEventListener("click", () => setQueueOpen(false));
  $("#previous-button").addEventListener("click", () => move(-1));
  $("#next-button").addEventListener("click", () => move(1));
  $$(".label-button").forEach((button) => {
    button.addEventListener("click", () => saveDecision({ label: button.dataset.label }));
  });
  $("#skip-button").addEventListener("click", () => saveDecision({ skipped: true }));
  document.addEventListener("keydown", (event) => {
    const tag = document.activeElement?.tagName;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag) || event.metaKey || event.ctrlKey || event.altKey) return;
    const labels = { "1": "Scintillation", "2": "Multipath", "3": "RFI", "4": "Other" };
    if (labels[event.key]) {
      event.preventDefault();
      saveDecision({ label: labels[event.key] });
    } else if (event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveDecision({ skipped: true });
    } else if (event.key === "Escape") {
      setQueueOpen(false);
    }
  });
}

async function initialize() {
  bindEvents();
  try {
    await loadSummary();
    await loadQueue();
  } catch (error) {
    showToast(error.message, true);
    $("#event-title").textContent = "Could not load the event index";
  }
}

initialize();
