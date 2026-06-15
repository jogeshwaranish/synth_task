"""The single-page coach UI served by app.py. Owner: Anish (frontend).

One self-contained HTML document — CSS and JS inlined, marked.js pulled from a
CDN — so FastAPI can return it from GET / with a plain HTMLResponse: no
StaticFiles mount, no second origin, no CORS. The page talks only to this app's
GET /insights endpoint and renders the harness-built `briefing_md` Markdown.

This module holds presentation only; all logic lives behind /insights.
"""

from __future__ import annotations

# Brand tokens are defined once in :root and referenced everywhere below so the
# palette in the task brief lives in exactly one place.
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>synth.</title>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<style>
  :root {
    --green: #10B981;
    --green-soft: #DDF7EE;
    --near-black: #111111;
    --gray: #8B8BBB;
    --soft-gray: #EAEAEA;
    --off-white: #F8F6F3;
    --golden: #EFCB77;
    --golden-dark: #D9B05E;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: system-ui, "SF Pro Display", "Segoe UI", sans-serif;
    font-weight: 400;
    color: var(--near-black);
    background: var(--off-white);
    line-height: 1.55;
  }
  .wrap { max-width: 760px; margin: 0 auto; padding: 2.5rem 1.5rem 5rem; }
  .logo {
    font-size: 1.6rem;
    font-weight: 600;
    letter-spacing: -0.02em;
    margin-bottom: 2.5rem;
  }
  .logo .dot { color: var(--green); }
  h1, h2, h3 { font-weight: 600; }
  label {
    display: block;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--gray);
    margin: 0 0 0.4rem;
  }
  .card {
    background: #fff;
    border: 1px solid var(--soft-gray);
    border-radius: 14px;
    padding: 2rem;
  }
  .field { margin-bottom: 1.25rem; }
  .field:last-of-type { margin-bottom: 1.75rem; }
  input[type=text], input[type=date] {
    width: 100%;
    padding: 0.7rem 0.85rem;
    font: inherit;
    color: var(--near-black);
    border: 1px solid var(--soft-gray);
    border-radius: 9px;
    background: #fff;
  }
  input:focus { outline: 2px solid var(--green); border-color: var(--green); }
  .hint { font-size: 0.8rem; color: var(--gray); margin-top: 0.4rem; }
  .row { display: flex; gap: 1rem; }
  .row .field { flex: 1; }
  button {
    appearance: none;
    border: none;
    background: var(--green);
    color: #fff;
    font: inherit;
    font-weight: 600;
    padding: 0.75rem 1.4rem;
    border-radius: 9px;
    cursor: pointer;
  }
  button:disabled { opacity: 0.6; cursor: default; }
  a { color: var(--green); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .back { display: inline-block; margin-bottom: 1.5rem; font-weight: 600; }
  .spinner {
    display: inline-block;
    width: 1.05rem; height: 1.05rem;
    border: 2px solid var(--green-soft);
    border-top-color: var(--green);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    vertical-align: -2px;
    margin-right: 0.55rem;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading { display: flex; align-items: center; color: var(--gray); }
  .error {
    background: var(--green-soft);
    border: 1px solid var(--golden-dark);
    background: #FCF5E3;
    color: var(--near-black);
    padding: 1rem 1.15rem;
    border-radius: 11px;
    margin-bottom: 1.5rem;
  }
  /* Rendered briefing markdown */
  .briefing h1 { font-size: 1.7rem; margin: 0 0 0.4rem; }
  .briefing h2 {
    font-size: 1.15rem;
    margin: 2rem 0 0.6rem;
    padding-bottom: 0.35rem;
    border-bottom: 1px solid var(--soft-gray);
  }
  .briefing h3 { font-size: 1rem; margin: 1.4rem 0 0.3rem; }
  .briefing blockquote {
    margin: 0.6rem 0;
    padding: 0.5rem 0.9rem;
    background: var(--green-soft);
    border-left: 3px solid var(--green);
    border-radius: 0 8px 8px 0;
    color: var(--near-black);
  }
  .briefing em { color: var(--gray); font-style: normal; }
  .briefing code {
    background: var(--off-white);
    border: 1px solid var(--soft-gray);
    border-radius: 5px;
    padding: 0.05rem 0.35rem;
    font-size: 0.85em;
  }
  /* JSON-fallback rendering */
  .insight {
    border: 1px solid var(--soft-gray);
    border-radius: 11px;
    padding: 1.1rem 1.25rem;
    margin-bottom: 1rem;
  }
  .badge {
    display: inline-block;
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0.15rem 0.55rem;
    border-radius: 999px;
  }
  .badge.high { background: var(--green-soft); color: #0a7355; }
  .badge.low, .badge.medium {
    background: var(--golden); color: var(--near-black);
    border: 1px solid var(--golden-dark);
  }
  details { margin-top: 1.5rem; }
  summary { cursor: pointer; color: var(--gray); font-weight: 600; }
  pre.trail {
    background: #fff; border: 1px solid var(--soft-gray);
    border-radius: 9px; padding: 1rem; overflow-x: auto; font-size: 0.85rem;
  }
  .hidden { display: none; }
</style>
</head>
<body>
<div class="wrap">
  <div class="logo">synth<span class="dot">.</span></div>

  <!-- FORM STATE -->
  <section id="form-state">
    <div class="card">
      <form id="report-form">
        <div class="field">
          <label for="dataset">Dataset</label>
          <select id="dataset" name="dataset">
            <option value="rowing">Rowing squad</option>
            <option value="triathlon">Triathlon</option>
          </select>
        </div>
        <div class="field">
          <label for="athlete">Athlete ID</label>
          <input type="text" id="athlete" name="athlete" required
                 autocomplete="off" list="athlete-list" placeholder="cox-madeline" />
          <datalist id="athlete-list"></datalist>
          <div class="hint" id="athlete-hint">Loading athletes…</div>
        </div>
        <div class="row">
          <div class="field">
            <label for="start">Start date</label>
            <input type="date" id="start" name="start" />
          </div>
          <div class="field">
            <label for="end">End date</label>
            <input type="date" id="end" name="end" />
          </div>
        </div>
        <div class="hint" id="date-hint"></div>
        <button type="submit" id="submit-btn" style="margin-top:1.25rem">Get Report</button>
      </form>
    </div>
  </section>

  <!-- LOADING STATE -->
  <section id="loading-state" class="hidden">
    <div class="card">
      <div class="loading"><span class="spinner"></span>Generating report…</div>
    </div>
  </section>

  <!-- REPORT STATE -->
  <section id="report-state" class="hidden">
    <a href="#" class="back" id="new-report">&larr; New Report</a>
    <div id="report-body"></div>
  </section>
</div>

<script>
  const $ = (id) => document.getElementById(id);
  const states = {
    form: $("form-state"),
    loading: $("loading-state"),
    report: $("report-state"),
  };
  function show(name) {
    for (const [k, el] of Object.entries(states))
      el.classList.toggle("hidden", k !== name);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // Error copy is fixed per the brief — we never surface raw payloads.
  function messageFor(status) {
    if (status === 404)
      return "No data found for athlete '" + escapeHtml(currentAthlete) +
             "'. Check the athlete ID and try again.";
    if (status === 502)
      return "Report generation failed validation. Try again or use a " +
             "stronger model (see README).";
    if (status === 429)
      return "Rate limit reached. Each report costs LLM tokens — please " +
             "wait a moment and try again.";
    return "Something went wrong. Check that the server is running.";
  }

  function renderError(msg) {
    $("report-body").innerHTML =
      '<div class="error">' + msg + "</div>";
    show("report");
  }

  // PRIMARY path: the harness-rendered Markdown briefing.
  function renderMarkdown(md) {
    $("report-body").innerHTML =
      '<div class="briefing">' + marked.parse(md) + "</div>";
    show("report");
  }

  // FALLBACK path: render the validated JSON sections cleanly.
  function renderJson(report) {
    const parts = [];
    parts.push('<div class="briefing"><h1>Training Insights — ' +
      escapeHtml(report.athlete_id) + "</h1></div>");
    if (report.summary)
      parts.push("<p>" + escapeHtml(report.summary) + "</p>");

    (report.patterns || []).forEach((p) => {
      const conf = (p.confidence || "medium").toLowerCase();
      const cls = conf === "high" ? "high" : conf;
      parts.push(
        '<div class="insight"><h3>' + escapeHtml(p.title) + "</h3>" +
        '<span class="badge ' + cls + '">' + escapeHtml(conf) +
        " confidence</span><p>" + escapeHtml(p.description) + "</p></div>");
    });

    if ((report.open_questions || []).length) {
      parts.push("<h2>Questions to follow up</h2><ul>");
      report.open_questions.forEach((q) =>
        parts.push("<li>" + escapeHtml(q) + "</li>"));
      parts.push("</ul>");
    }

    const trail = (report.evidence || [])
      .map((e) => e.step + ". " + e.tool +
        "(" + Object.entries(e.args || {}).map(([k, v]) => k + "=" + v).join(", ") +
        ") -> " + e.result_digest)
      .join("\\n");
    if (trail)
      parts.push("<details><summary>How this was checked</summary>" +
        '<pre class="trail">' + escapeHtml(trail) + "</pre></details>");

    $("report-body").innerHTML = parts.join("");
    show("report");
  }

  let currentAthlete = "";
  // athlete_id -> {start, end} for the selected dataset; drives the date bounds.
  let spans = {};

  // Pre-fill and CLAMP the date pickers to a [start, end] window so the coach
  // can only choose dates the data actually covers.
  function applyWindow(start, end) {
    for (const el of [$("start"), $("end")]) {
      el.min = start || "";
      el.max = end || "";
    }
    $("start").value = start || "";
    $("end").value = end || "";
    $("date-hint").textContent = (start && end)
      ? "Data covers " + start + " to " + end + ". Narrow the window if you like."
      : "";
  }

  // When an athlete is chosen, snap the date window to that athlete's coverage.
  function onAthletePicked() {
    const span = spans[$("athlete").value.trim()];
    if (span) applyWindow(span.start, span.end);
  }

  // Load the athlete roster + date spans for the selected dataset and rebuild
  // the picker. Names come from the data, so the UI never lists stale athletes.
  async function loadDataset() {
    const dataset = $("dataset").value;
    $("athlete-hint").textContent = "Loading athletes…";
    spans = {};
    try {
      const resp = await fetch("/athletes?dataset=" + encodeURIComponent(dataset));
      if (!resp.ok) throw new Error("athletes " + resp.status);
      const rows = (await resp.json()).athletes || [];
      const list = $("athlete-list");
      list.innerHTML = "";
      rows.forEach((r) => {
        spans[r.athlete_id] = { start: r.start, end: r.end };
        const opt = document.createElement("option");
        opt.value = r.athlete_id;
        list.appendChild(opt);
      });

      const ids = rows.map((r) => r.athlete_id);
      if (dataset === "triathlon" && ids.length) {
        // One athlete — prefill it and its full window.
        $("athlete").value = ids[0];
        $("athlete-hint").textContent = "Single athlete: " + ids[0];
        onAthletePicked();
      } else {
        $("athlete").value = "";
        const sample = ids.slice(0, 3).join(", ");
        $("athlete-hint").textContent = ids.length
          ? "e.g. " + sample + " (" + ids.length + " athletes) — pick one to set the dates"
          : "No athletes found in this dataset.";
        // Default the window to the whole dataset until an athlete is chosen.
        const starts = rows.map((r) => r.start).filter(Boolean).sort();
        const ends = rows.map((r) => r.end).filter(Boolean).sort();
        applyWindow(starts[0], ends[ends.length - 1]);
      }
    } catch (e) {
      $("athlete-hint").textContent =
        "Couldn't load athletes. Check that the server is running.";
    }
  }

  async function getReport(ev) {
    ev.preventDefault();
    currentAthlete = $("athlete").value.trim();
    if (!currentAthlete) return;

    const params = new URLSearchParams({
      athlete: currentAthlete, dataset: $("dataset").value,
    });
    if ($("start").value) params.set("start", $("start").value);
    if ($("end").value) params.set("end", $("end").value);

    show("loading");
    try {
      const resp = await fetch("/insights?" + params.toString());
      if (!resp.ok) { renderError(messageFor(resp.status)); return; }
      const data = await resp.json();
      if (typeof data.briefing_md === "string" && data.briefing_md.length)
        renderMarkdown(data.briefing_md);
      else
        renderJson(data);
    } catch (e) {
      renderError(messageFor(0));
    }
  }

  $("dataset").addEventListener("change", loadDataset);
  $("athlete").addEventListener("change", onAthletePicked);
  $("report-form").addEventListener("submit", getReport);
  $("new-report").addEventListener("click", (ev) => {
    ev.preventDefault();
    show("form");
  });
  loadDataset();  // populate the default (rowing) roster on first load
</script>
</body>
</html>
"""
