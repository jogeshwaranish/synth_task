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
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"
        integrity="sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi"
        crossorigin="anonymous"></script>
<!-- marked does NOT sanitize HTML; DOMPurify scrubs its output before we ever
     touch innerHTML. See renderMarkdown(). Both scripts are SRI-pinned. -->
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"
        integrity="sha384-+VfUPEb0PdtChMwmBcBmykRMDd+v6D/oFmB3rZM/puCMDYcIvF968OimRh4KQY9a"
        crossorigin="anonymous"></script>
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
  .demo-step {
    margin-bottom: 1.65rem;
    padding-bottom: 1.65rem;
    border-bottom: 1px solid var(--soft-gray);
  }
  .demo-step:last-child { margin-bottom: 0; padding-bottom: 0; border-bottom: 0; }
  .step-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1rem;
  }
  .step-head h2 {
    font-size: 1.05rem;
    margin: 0;
  }
  .step-index {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.75rem;
    height: 1.75rem;
    margin-right: 0.55rem;
    border-radius: 999px;
    background: var(--near-black);
    color: var(--off-white);
    font-size: 0.78rem;
  }
  .dataset-line {
    display: grid;
    grid-template-columns: minmax(150px, 0.36fr) minmax(0, 1fr);
    gap: 0.9rem;
    align-items: end;
    margin-bottom: 0.95rem;
  }
  .sync-actions {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    margin-top: 0.9rem;
  }
  .sync-actions button {
    min-height: 50px;
    white-space: nowrap;
  }
  .overview-list {
    display: grid;
    gap: 0.5rem;
    font-size: 0.88rem;
  }
  .overview-row {
    display: flex; justify-content: space-between; gap: 1rem;
    padding: 0.55rem 0.7rem; background: var(--off-white);
    border: 1px solid var(--soft-gray); border-radius: 9px;
  }
  .sync-summary {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.55rem;
    margin-top: 0.9rem;
  }
  .summary-tile {
    background: var(--green-soft);
    border: 1px solid color-mix(in srgb, var(--green) 35%, var(--soft-gray));
    border-radius: 9px;
    padding: 0.65rem 0.75rem;
  }
  .summary-tile .k {
    color: #0a7355;
    font-size: 0.66rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 700;
  }
  .summary-tile .v {
    margin-top: 0.15rem;
    font-size: 1.05rem;
    font-weight: 700;
  }
  .field { margin-bottom: 1.25rem; }
  .field:last-of-type { margin-bottom: 1.75rem; }
  input[type=text], input[type=date], select {
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
  .button-secondary {
    background: var(--near-black);
  }
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
  .report-progress {
    margin: 1rem 0 0;
    padding: 0;
    list-style: none;
    display: grid;
    gap: 0.45rem;
    color: var(--gray);
    font-size: 0.88rem;
  }
  .report-progress li {
    display: flex;
    align-items: center;
    gap: 0.55rem;
  }
  .report-progress li::before {
    content: "";
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 999px;
    background: var(--soft-gray);
  }
  .report-progress li.active {
    color: var(--near-black);
    font-weight: 600;
  }
  .report-progress li.active::before {
    background: var(--green);
  }
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
  /* --- Scannable report summary (built from the report JSON) --- */
  .athlete-header {
    display: flex; justify-content: space-between; align-items: flex-start;
    gap: 1rem; margin-bottom: 0.9rem;
  }
  .athlete-header h1 { font-size: 1.55rem; margin: 0 0 0.25rem; }
  .athlete-header .meta { color: var(--gray); font-size: 0.85rem; }
  .athlete-header .gen {
    color: var(--gray); font-size: 0.78rem; text-align: right; white-space: nowrap;
  }
  .status-badge {
    display: inline-flex; align-items: center; gap: 0.6rem;
    font-weight: 600; font-size: 1rem;
    padding: 0.5rem 1rem; border-radius: 999px;
  }
  .status-badge .sys {
    font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.07em;
    font-weight: 600; opacity: 0.75;
  }
  .status-green { background: var(--green-soft); color: #0a7355; }
  .status-golden { background: #FCF5E3; color: #7a5a12; border: 1px solid var(--golden-dark); }
  .status-red { background: #FBE9E7; color: #B23A2E; border: 1px solid #E0A99F; }
  .status-note { font-size: 0.78rem; color: var(--gray); margin: 0.4rem 0 1.5rem; }
  .coach-panel {
    display: grid;
    grid-template-columns: minmax(0, 1.15fr) minmax(230px, 0.85fr);
    gap: 0.9rem;
    margin: 1rem 0 1.2rem;
  }
  .coach-read, .action-plan {
    background: var(--off-white);
    border: 1px solid var(--soft-gray);
    border-radius: 12px;
    padding: 1rem 1.1rem;
  }
  .coach-label {
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--gray);
    font-weight: 700;
    margin-bottom: 0.45rem;
  }
  .coach-read .read {
    font-size: 1.08rem;
    line-height: 1.45;
    font-weight: 600;
  }
  .signal-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-top: 0.85rem;
  }
  .signal-chip {
    display: inline-flex;
    align-items: center;
    min-height: 1.65rem;
    padding: 0.2rem 0.55rem;
    border-radius: 999px;
    background: #fff;
    border: 1px solid var(--soft-gray);
    color: var(--near-black);
    font-size: 0.74rem;
  }
  .action-plan ol {
    margin: 0;
    padding-left: 1.2rem;
    display: grid;
    gap: 0.45rem;
    font-size: 0.9rem;
  }
  .confidence-note {
    margin-top: 0.8rem;
    color: var(--gray);
    font-size: 0.8rem;
  }
  .metric-strip { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 1.75rem; }
  .metric-tile {
    flex: 1; min-width: 120px; background: var(--off-white);
    border: 1px solid var(--soft-gray); border-radius: 11px; padding: 0.8rem 1rem;
  }
  .metric-tile .k {
    font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--gray); margin-bottom: 0.3rem;
  }
  .metric-tile .v { font-size: 1.25rem; font-weight: 600; }
  .metric-tile .v.green { color: var(--green); }
  .metric-tile .v.golden { color: var(--golden-dark); }
  .section-h {
    font-size: 1.15rem; margin: 1.75rem 0 0.8rem;
    padding-bottom: 0.35rem; border-bottom: 1px solid var(--soft-gray);
  }
  .insight-card {
    border: 1px solid var(--soft-gray); border-radius: 12px;
    padding: 1.05rem 1.25rem; margin-bottom: 0.9rem; background: #fff;
  }
  .insight-card .top {
    display: flex; justify-content: space-between; gap: 1rem; align-items: baseline;
  }
  .insight-card .dates { color: var(--gray); font-size: 0.78rem; white-space: nowrap; }
  .kind-pill {
    display: inline-block; background: var(--green-soft); color: #0a7355;
    font-size: 0.66rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; padding: 0.13rem 0.5rem; border-radius: 999px;
    margin-right: 0.4rem;
  }
  .insight-card h3 { margin: 0.55rem 0 0.2rem; font-size: 1.02rem; font-weight: 600; }
  .insight-card details { margin-top: 0.5rem; }
  .insight-card details > summary { color: var(--green); font-weight: 600; font-size: 0.85rem; }
  .insight-card .detail-body { margin-top: 0.5rem; }
  .insight-card .worth {
    margin-top: 0.5rem; padding: 0.5rem 0.8rem; background: var(--green-soft);
    border-left: 3px solid var(--green); border-radius: 0 8px 8px 0; font-size: 0.9rem;
  }
  .chips { margin-top: 0.7rem; display: flex; flex-wrap: wrap; gap: 0.35rem; }
  .chip {
    background: var(--soft-gray); color: var(--near-black); font-size: 0.72rem;
    padding: 0.1rem 0.5rem; border-radius: 6px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .questions-box {
    background: var(--green-soft); border-radius: 12px;
    padding: 1.05rem 1.4rem; margin: 1.5rem 0;
  }
  .questions-box .q-label {
    font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.09em;
    color: #0a7355; font-weight: 700; margin-bottom: 0.5rem;
  }
  .questions-box ol { margin: 0; padding-left: 1.2rem; }
  .questions-box li { margin: 0.25rem 0; }
  .evi-step { padding: 0.4rem 0; border-bottom: 1px solid var(--soft-gray); font-size: 0.85rem; }
  .evi-step:last-child { border-bottom: none; }
  .evi-step .n { color: var(--gray); margin-right: 0.4rem; }
  .evi-step .tool { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .evi-more { display: inline; }
  .evi-more > summary { color: var(--green); font-size: 0.82rem; display: inline; cursor: pointer; }
  .full-briefing { margin-top: 1.75rem; }
  .full-briefing > summary { font-weight: 600; color: var(--gray); }
  .hidden { display: none; }
  @media (max-width: 680px) {
    .wrap { padding: 1.25rem 0.9rem 3rem; }
    .card { padding: 1.15rem; }
    .dataset-line, .row { display: grid; grid-template-columns: 1fr; }
    .sync-actions { display: grid; grid-template-columns: 1fr; }
    .coach-panel { grid-template-columns: 1fr; }
    .sync-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .athlete-header { display: block; }
    .athlete-header .gen { text-align: left; margin-top: 0.35rem; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="logo">synth<span class="dot">.</span></div>

  <!-- FORM STATE -->
  <section id="form-state">
    <div class="card">
      <div class="demo-step">
        <div class="step-head">
          <h2><span class="step-index">1</span>Connect Sheet</h2>
        </div>
        <div class="dataset-line">
          <div class="field" style="margin-bottom:0">
            <label for="dataset">Dataset</label>
            <select id="dataset" name="dataset">
              <option value="triathlon">Triathlon</option>
              <option value="rowing">Rowing squad</option>
            </select>
          </div>
          <div class="field" style="margin-bottom:0">
            <label for="sheet-url">Google Sheet link</label>
            <input type="text" id="sheet-url" autocomplete="off"
                   placeholder="https://docs.google.com/spreadsheets/d/..." />
          </div>
        </div>
        <div class="sync-actions">
          <button type="button" id="sheet-sync-btn">Sync + Analyze</button>
        </div>
        <div class="hint" id="sheet-sync-status">
          Paste a shared Google Sheet link. Sync downloads it, maps it, and refreshes metrics.
        </div>
        <div class="sync-summary hidden" id="sync-summary"></div>
      </div>
      <div class="demo-step">
        <div class="step-head">
          <h2>Data Snapshot</h2>
        </div>
        <div class="overview-list" id="overview-list"></div>
        <div id="load-chart" class="hint"></div>
      </div>
      <form id="report-form" class="demo-step">
        <div class="step-head">
          <h2><span class="step-index">2</span>Generate Insight Report</h2>
        </div>
        <div class="field">
          <label for="athlete">Athlete ID</label>
          <select id="athlete" name="athlete" required></select>
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
        <button type="submit" id="submit-btn" class="button-secondary" style="margin-top:1.25rem">
          Generate Report
        </button>
        <div class="hint">Report generation uses the Anthropic API. Sync + Analyze does not.</div>
      </form>
    </div>
  </section>

  <!-- LOADING STATE -->
  <section id="loading-state" class="hidden">
    <div class="card">
      <div class="loading"><span class="spinner"></span>Generating report…</div>
      <ol class="report-progress" id="report-progress">
        <li>Resolving athlete and date range</li>
        <li>Querying metrics and anomalies</li>
        <li>Running AI synthesis</li>
        <li>Validating report JSON</li>
        <li>Rendering briefing</li>
      </ol>
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
      return "Report generation could not complete. Anthropic may be busy, " +
             "or the model output failed validation. Try again in a moment.";
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

  // ---- Report rendering ------------------------------------------------
  // A scannable visual summary built from the validated report JSON, with the
  // prose briefing tucked below. EVERY dynamic value goes through escapeHtml;
  // only the briefing markdown is HTML, and it is DOMPurify-sanitized.

  const KIND_LABEL = {
    trend: "trend", anomaly_explanation: "explanation",
    correlation: "connection", observation: "observation",
  };

  function fmt(x, d) {
    return (x === null || x === undefined || isNaN(x)) ? null : Number(x).toFixed(d);
  }

  // The contract does NOT carry metric values or anomaly severities into the
  // report. The one place real numbers survive is a compare_periods evidence
  // digest ("compare_periods -> {json}"); parse the latest one when present.
  function latestComparePeriods(report) {
    const steps = (report.evidence || []).filter((e) => e.tool === "compare_periods");
    for (let i = steps.length - 1; i >= 0; i--) {
      const d = steps[i].result_digest || "";
      const at = d.indexOf("->");
      if (at === -1) continue;
      try { return JSON.parse(d.slice(at + 2).trim()); } catch (e) { /* truncated */ }
    }
    return null;
  }

  // "System read" — an explicitly-labelled heuristic over ONLY the hard signals
  // the report actually carries. Returns null when nothing is derivable, so the
  // badge (and its disclaimer) are omitted rather than guessing.
  //
  // Overreaching fires only on a real ACWR > 1.3 (parsed from the compare_periods
  // digest) OR a flagged anomaly — NEVER inferred from insight/pattern text. The
  // "severity == flag" branch is wired but inert: anomaly severities are not in
  // the contract (anomalies_reviewed is ids only — "{athlete}:{date}:{metric}",
  // no severity), so it can't be evaluated client-side.
  // TODO(viz): pass anomaly severities through the contract to honour the
  // "severity == flag" rule.
  function deriveStatus(report) {
    const cp = latestComparePeriods(report);
    const acwr = cp && cp.period_a && typeof cp.period_a.mean_acwr === "number"
      ? cp.period_a.mean_acwr : null;
    const flagged = false;  // severities not in the report contract — see above

    if ((acwr !== null && acwr > 1.3) || flagged)
      return { cls: "status-red", icon: "🔴", label: "Overreaching" };

    // Plateau: explicit heuristic on insight confidence (an allowed badge input),
    // reached only after Overreaching has been ruled out on hard signals.
    const plateau = (report.patterns || []).some((p) =>
      p.kind === "anomaly_explanation" && (p.confidence === "medium" || p.confidence === "high"));
    if (plateau)
      return { cls: "status-golden", icon: "⚠", label: "Plateau" };

    if (acwr !== null && acwr >= 0.8 && acwr <= 1.3)
      return { cls: "status-green", icon: "✓", label: "On Track" };

    return null;  // no clear, supported signal -> omit the badge entirely
  }

  function buildHeader(report) {
    const cov = report.data_coverage || {};
    const gen = report.generated_at
      ? String(report.generated_at).replace("T", " ").slice(0, 16) + " UTC" : "";
    const coverage = (cov.n_activities ?? "?") + " activities · " +
      (cov.n_wellness_days ?? "?") + " wellness days · " + (cov.n_days ?? "?") + " days";
    return '<div class="athlete-header"><div>' +
        "<h1>" + escapeHtml(report.athlete_id || "—") + "</h1>" +
        '<div class="meta">' +
          escapeHtml((report.period_start || "?") + " → " + (report.period_end || "?")) +
          " · " + escapeHtml(coverage) +
        "</div></div>" +
        '<div class="gen">' + escapeHtml(gen) + "</div>" +
      "</div>";
  }

  function buildStatus(report) {
    const s = deriveStatus(report);
    if (!s) return "";  // underivable -> no badge, no SYSTEM READ label, no disclaimer
    return '<div class="status-badge ' + s.cls + '">' +
        "<span>" + s.icon + " " + escapeHtml(s.label) + "</span>" +
        '<span class="sys">System read</span>' +
      "</div>" +
      '<p class="status-note">Heuristic read of the report data — a glance, not a diagnosis.</p>';
  }

  function parseCoachSummary(summary) {
    const lines = String(summary || "").split(/\\r?\\n/).map((l) => l.trim()).filter(Boolean);
    const out = { read: "", actions: [], confidence: "" };
    let mode = "";
    for (const line of lines) {
      if (/^READ:/i.test(line)) {
        out.read = line.replace(/^READ:\\s*/i, "");
        mode = "";
      } else if (/^NEXT_7_DAYS:/i.test(line)) {
        mode = "actions";
      } else if (/^DATA_CONFIDENCE:/i.test(line)) {
        out.confidence = line.replace(/^DATA_CONFIDENCE:\\s*/i, "");
        mode = "";
      } else if (mode === "actions" && /^[-*]\\s+/.test(line)) {
        out.actions.push(line.replace(/^[-*]\\s+/, ""));
      } else if (!out.read) {
        out.read = line;
      }
    }
    return out;
  }

  function buildCoachPanel(report) {
    const parsed = parseCoachSummary(report.summary);
    const read = parsed.read || "No coaching read was generated for this window.";
    const actions = parsed.actions.slice(0, 3);
    const cov = report.data_coverage || {};
    const wellness = Number(cov.n_wellness_days || 0);
    const chips = [
      (cov.n_activities ?? "?") + " activities",
      (cov.n_days ?? "?") + " metric days",
      wellness ? wellness + " wellness days" : "no wellness rows",
    ];
    const actionHtml = actions.length
      ? "<ol>" + actions.map((a) => "<li>" + escapeHtml(a) + "</li>").join("") + "</ol>"
      : '<p class="status-note">Open the highlighted patterns below for the next coaching call.</p>';
    const confidence = parsed.confidence ||
      (wellness ? "Training and recovery signals are both present." :
       "Training signal is present; recovery context is limited without wellness rows.");
    return '<div class="coach-panel">' +
      '<div class="coach-read">' +
        '<div class="coach-label">Coach read</div>' +
        '<div class="read">' + escapeHtml(read) + "</div>" +
        '<div class="signal-row">' + chips.map((c) =>
          '<span class="signal-chip">' + escapeHtml(c) + "</span>").join("") + "</div>" +
      "</div>" +
      '<div class="action-plan">' +
        '<div class="coach-label">Next 7 days</div>' +
        actionHtml +
        '<div class="confidence-note">' + escapeHtml(confidence) + "</div>" +
      "</div>" +
    "</div>";
  }

  function buildStrip(report) {
    const cp = latestComparePeriods(report);
    const a = (cp && cp.period_a) || {};
    const dl = (cp && cp.deltas) || {};
    const tiles = [];
    // Only push a tile when the metric has a real value; absent/empty -> no tile.
    const pushTile = (k, v, cls) => {
      if (v === null || v === undefined || v === "") return;
      tiles.push('<div class="metric-tile"><div class="k">' + escapeHtml(k) + "</div>" +
        '<div class="v' + (cls ? " " + cls : "") + '">' + escapeHtml(v) + "</div></div>");
    };

    const load = fmt(a.mean_acute_load_7d, 0);
    if (load !== null) {
      const arrow = dl.mean_acute_load_7d == null ? ""
        : (dl.mean_acute_load_7d > 0 ? " ↑" : (dl.mean_acute_load_7d < 0 ? " ↓" : ""));
      pushTile("Acute load 7d", load + arrow, "green");
    }
    const acwr = fmt(a.mean_acwr, 2);
    if (acwr !== null)
      pushTile("ACWR", acwr, (a.mean_acwr >= 0.8 && a.mean_acwr <= 1.3) ? "green" : "golden");

    // pace_trend_pct_14d / hr_at_pace_trend_pct_14d aren't carried in the report
    // (get_daily_metrics digests are bare row counts) — omitted, never shown empty.
    // TODO(viz): surface these (and an ACWR sparkline) if a metrics endpoint is added.

    if (!tiles.length) return "";  // no real data -> no strip at all
    return '<div class="metric-strip">' + tiles.join("") + "</div>";
  }

  // Patterns end their description with "EVIDENCE: ..."; split so the takeaway
  // reads first (mirrors synthesize/render.py).
  function splitEvidence(desc) {
    const d = String(desc || "");
    for (const m of ["EVIDENCE:", "Evidence:"]) {
      const i = d.indexOf(m);
      if (i !== -1) return [d.slice(0, i).trim(), d.slice(i + m.length).trim()];
    }
    return [d.trim(), null];
  }

  function buildInsights(report) {
    const pats = report.patterns || [];
    if (!pats.length)
      return '<p class="status-note">No patterns surfaced for this period.</p>';
    return pats.map((p) => {
      const conf = (p.confidence || "medium").toLowerCase();
      const confCls = conf === "high" ? "high" : conf;  // .badge high/medium/low
      const kind = KIND_LABEL[p.kind] || p.kind || "observation";
      const ev = splitEvidence(p.description);
      const dates = (p.date_start || "") + " → " + (p.date_end || "");
      let detail = "<p>" + escapeHtml(ev[0]) + "</p>";
      if (ev[1])
        detail += '<div class="worth"><strong>Evidence:</strong> ' + escapeHtml(ev[1]) + "</div>";
      if (p.caveats)
        detail += '<div class="worth"><strong>Worth noting:</strong> ' + escapeHtml(p.caveats) + "</div>";
      const chips = (p.metrics_involved || [])
        .map((m) => '<span class="chip">' + escapeHtml(m) + "</span>").join("");
      return '<div class="insight-card">' +
          '<div class="top"><div>' +
            '<span class="kind-pill">' + escapeHtml(kind) + "</span>" +
            '<span class="badge ' + confCls + '">' + escapeHtml(conf) + " confidence</span>" +
          '</div><span class="dates">' + escapeHtml(dates) + "</span></div>" +
          "<h3>" + escapeHtml(p.title || "") + "</h3>" +
          "<details><summary>↓ Full detail</summary>" +
            '<div class="detail-body">' + detail + "</div></details>" +
          (chips ? '<div class="chips">' + chips + "</div>" : "") +
        "</div>";
    }).join("");
  }

  function buildQuestions(report) {
    const qs = report.open_questions || [];
    if (!qs.length) return "";
    return '<div class="questions-box">' +
      '<div class="q-label">Questions for the athlete</div><ol>' +
      qs.map((q) => "<li>" + escapeHtml(q) + "</li>").join("") +
      "</ol></div>";
  }

  function buildEvidence(report) {
    const ev = report.evidence || [];
    if (!ev.length) return "";
    const steps = ev.map((e) => {
      const args = Object.entries(e.args || {}).map(([k, v]) => k + "=" + v).join(", ");
      const head = escapeHtml(e.tool || "") + (args ? "(" + escapeHtml(args) + ")" : "");
      const digest = String(e.result_digest || "");
      const body = digest.length > 120
        ? '<details class="evi-more"><summary>' + escapeHtml(digest.slice(0, 120)) +
          "…</summary>" + escapeHtml(digest) + "</details>"
        : escapeHtml(digest);
      return '<div class="evi-step"><span class="n">' + escapeHtml(String(e.step)) +
        '.</span><span class="tool">' + head + "</span> → " + body + "</div>";
    }).join("");
    return "<details><summary>↓ How this was checked</summary>" + steps + "</details>";
  }

  function renderReport(report) {
    let html =
      buildHeader(report) +
      buildStatus(report) +
      buildCoachPanel(report) +
      buildStrip(report) +
      '<h2 class="section-h">Training calls</h2>' +
      buildInsights(report) +
      buildQuestions(report) +
      buildEvidence(report);
    // Full prose briefing below the visual summary — LLM output, so sanitize.
    const md = report.briefing_md;
    if (typeof md === "string" && md.length) {
      html += '<details class="full-briefing">' +
        "<summary>↓ Full written briefing</summary>" +
        '<div class="briefing">' + DOMPurify.sanitize(marked.parse(md)) + "</div></details>";
    }
    $("report-body").innerHTML = html;
    show("report");
  }

  let currentAthlete = "";
  // athlete_id -> {start, end} for the selected dataset; drives the date bounds.
  let spans = {};
  let progressTimer = null;

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

  function setReportProgress(index) {
    const items = Array.from($("report-progress").querySelectorAll("li"));
    items.forEach((el, i) => el.classList.toggle("active", i === index));
  }

  function startReportProgress() {
    clearInterval(progressTimer);
    let i = 0;
    setReportProgress(i);
    progressTimer = setInterval(() => {
      i = Math.min(i + 1, 4);
      setReportProgress(i);
      if (i === 4) clearInterval(progressTimer);
    }, 4500);
  }

  function stopReportProgress() {
    clearInterval(progressTimer);
    progressTimer = null;
  }

  function updateDatasetMode() {
    const isRowing = $("dataset").value === "rowing";
    $("sync-summary").classList.add("hidden");
    $("sheet-sync-status").textContent = isRowing
      ? "Paste the rowing sheet link. Sync ingests the roster, then choose an athlete below."
      : "Paste the triathlon sheet link. Sync downloads it, maps it, and refreshes metrics.";
  }

  function riskText(row) {
    const latest = row.latest || {};
    const acwr = latest.acwr == null ? "ACWR ?" : "ACWR " + Number(latest.acwr).toFixed(2);
    return escapeHtml(row.risk_level || "steady") + " · " + escapeHtml(acwr);
  }

  async function loadOverview() {
    const dataset = $("dataset").value;
    const list = $("overview-list");
    const chart = $("load-chart");
    list.innerHTML = "";
    chart.textContent = "Loading overview…";
    try {
      const resp = await fetch("/overview?dataset=" + encodeURIComponent(dataset));
      if (!resp.ok) throw new Error("overview " + resp.status);
      const rows = (await resp.json()).athletes || [];
      list.innerHTML = rows.slice(0, 5).map((r) =>
        '<div class="overview-row"><span>' + escapeHtml(r.athlete_id) + "</span>" +
        "<span>" + riskText(r) + "</span></div>"
      ).join("");
      chart.textContent = rows.length
        ? "Open an athlete report to view detail from /athlete-series."
        : "No overview rows found.";
    } catch (e) {
      chart.textContent = "Couldn't load overview.";
    }
  }

  async function syncGoogleSheet() {
    const sheetUrl = $("sheet-url").value.trim();
    if (!sheetUrl) {
      $("sheet-sync-status").textContent = "Paste a Google Sheet link first.";
      return;
    }
    const btn = $("sheet-sync-btn");
    btn.disabled = true;
    $("sheet-sync-status").textContent = "Syncing sheet and refreshing analysis…";
    try {
      const resp = await fetch("/sync-google-sheet", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          dataset: $("dataset").value,
          sheet_url: sheetUrl,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        $("sheet-sync-status").textContent =
          "Sync failed: " + escapeHtml(data.detail || "check the sheet sharing settings.");
        return;
      }
      renderSyncSummary(data);
      $("sheet-sync-status").textContent =
        "Synced and analyzed. Choose an athlete below, then generate a report.";
      await loadDataset(data.athlete_id);
    } catch (e) {
      $("sheet-sync-status").textContent = "Sync failed. Check that the server is running.";
    } finally {
      btn.disabled = false;
    }
  }

  function renderSyncSummary(data) {
    const a = data.analysis || {};
    const tiles = [
      ["Scope", data.athlete_id || "Roster"],
      ["Activities", data.sheet ?? 0],
      ["Metric days", a.metrics ?? 0],
      ["Anomalies", a.anomalies ?? 0],
    ];
    $("sync-summary").innerHTML = tiles.map(([k, v]) =>
      '<div class="summary-tile"><div class="k">' + escapeHtml(k) + "</div>" +
      '<div class="v">' + escapeHtml(v) + "</div></div>"
    ).join("");
    $("sync-summary").classList.remove("hidden");
  }

  // Load the athlete roster + date spans for the selected dataset and rebuild
  // the picker. Names come from the data, so the UI never lists stale athletes.
  async function loadDataset(preferredAthlete) {
    const dataset = $("dataset").value;
    $("athlete-hint").textContent = "Loading athletes…";
    spans = {};
    try {
      const resp = await fetch("/athletes?dataset=" + encodeURIComponent(dataset));
      if (!resp.ok) throw new Error("athletes " + resp.status);
      const rows = (await resp.json()).athletes || [];
      const select = $("athlete");
      select.innerHTML = "";
      rows.forEach((r) => {
        spans[r.athlete_id] = { start: r.start, end: r.end };
        const opt = document.createElement("option");
        opt.value = r.athlete_id;
        opt.textContent = r.athlete_id;
        select.appendChild(opt);
      });

      const ids = rows.map((r) => r.athlete_id);
      if (preferredAthlete && spans[preferredAthlete]) {
        $("athlete").value = preferredAthlete;
        $("athlete-hint").textContent = "Synced athlete: " + preferredAthlete;
        onAthletePicked();
      } else if (dataset === "triathlon" && ids.length) {
        // One athlete — prefill it and its full window.
        $("athlete").value = ids[0];
        $("athlete-hint").textContent = "Single athlete: " + ids[0];
        onAthletePicked();
      } else {
        const sample = ids.slice(0, 3).join(", ");
        $("athlete-hint").textContent = ids.length
          ? "e.g. " + sample + " (" + ids.length + " athletes) — pick one to set the dates"
          : "No athletes found in this dataset.";
        if (!ids.length) {
          const opt = document.createElement("option");
          opt.value = "";
          opt.textContent = "No athletes found";
          select.appendChild(opt);
        } else {
          $("athlete").value = ids[0];
        }
        // Default the window to the whole dataset until an athlete is chosen.
        const starts = rows.map((r) => r.start).filter(Boolean).sort();
        const ends = rows.map((r) => r.end).filter(Boolean).sort();
        applyWindow(starts[0], ends[ends.length - 1]);
      }
    } catch (e) {
      $("athlete-hint").textContent =
        "Couldn't load athletes. Check that the server is running.";
    }
    await loadOverview();
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
    startReportProgress();
    try {
      const resp = await fetch("/insights?" + params.toString());
      if (!resp.ok) { stopReportProgress(); renderError(messageFor(resp.status)); return; }
      const data = await resp.json();
      stopReportProgress();
      renderReport(data);
    } catch (e) {
      stopReportProgress();
      renderError(messageFor(0));
    }
  }

  $("dataset").addEventListener("change", () => {
    updateDatasetMode();
    loadDataset();
  });
  $("athlete").addEventListener("change", onAthletePicked);
  $("sheet-sync-btn").addEventListener("click", syncGoogleSheet);
  $("report-form").addEventListener("submit", getReport);
  $("new-report").addEventListener("click", (ev) => {
    ev.preventDefault();
    show("form");
  });
  updateDatasetMode();
  loadDataset();  // populate the default dataset roster on first load
</script>
</body>
</html>
"""
