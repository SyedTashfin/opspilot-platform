"""Server-rendered views: the surface a reviewer clicks in a browser.

Design choices worth stating, because they are choices:

* **No build step, no JavaScript framework.** The API serves HTML so that `uvicorn ... --port 8000` plus a
  browser is the entire setup. Next.js remains the intended UI (ADR-001) and consumes the same JSON
  contract, so nothing here is wasted when it lands.
* **Every value is escaped.** Evidence includes log lines, deployment notes and runbook text — and one
  shipped scenario intentionally embeds instructions in retrieved data. An unescaped template would make
  the demo itself the injection vector.
* **Provenance is visible on the page**, not just in the API: each headline number is shown with its
  ``source`` and a ``basis`` tooltip, and the demo banner says the telemetry came from the incident lab.
"""

from __future__ import annotations

import html
from typing import Any

STYLE = """
:root { color-scheme: dark; --fg: #e8e8ea; --muted: #9a9aa5; --line: #2a2a33; --card: #16161b;
        --accent: #7dd3a0; --warn: #e8c07d; --bad: #e08a8a; }
* { box-sizing: border-box; }
body { margin: 0; background: #0d0d10; color: var(--fg);
       font: 15px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }
main { max-width: 980px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted);
     margin: 32px 0 10px; font-weight: 600; }
h3 { font-size: 15px; margin: 18px 0 6px; }
a { color: var(--accent); }
p, li { color: var(--fg); }
.muted { color: var(--muted); }
.banner { border: 1px solid var(--line); border-left: 3px solid var(--warn); background: var(--card);
          padding: 10px 14px; margin: 18px 0; font-size: 13px; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }
.card { border: 1px solid var(--line); background: var(--card); padding: 12px 14px; }
.card .v { font-size: 20px; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
form { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 0; }
select, button { font: inherit; background: #1c1c22; color: var(--fg); border: 1px solid var(--line);
                 padding: 8px 10px; }
button { cursor: pointer; }
button:hover { border-color: var(--accent); }
.ok { color: var(--accent); } .warn { color: var(--warn); } .bad { color: var(--bad); }
pre { background: var(--card); border: 1px solid var(--line); padding: 12px; overflow-x: auto;
      font-size: 12px; margin: 6px 0 0; }
.step { border-left: 2px solid var(--line); padding: 4px 0 4px 12px; margin: 6px 0; }
.step.succeeded { border-color: var(--accent); }
.step.waiting_approval { border-color: var(--warn); }
.step.failed { border-color: var(--bad); }
"""

STATUS_CLASS = {
    "succeeded": "ok",
    "waiting_approval": "warn",
    "failed": "bad",
    "timeout": "bad",
    "budget_exceeded": "bad",
    "running": "warn",
}


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_e(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>"
    )


def _status(value: str) -> str:
    return f"<span class='{STATUS_CLASS.get(value, 'muted')}'>{_e(value.replace('_', ' '))}</span>"


def _metric_cards(overview: dict[str, Any]) -> str:
    wanted = (
        "runs_total",
        "success_rate",
        "spend_total_eur",
        "model_calls_total",
        "tokens_total",
        "mean_step_latency_ms",
        "p95_run_duration_ms",
        "spans_total",
        "approvals_pending",
    )
    cards = []
    for name in wanted:
        metric = overview.get(name) or {}
        if not metric:
            continue
        basis = _e(metric.get("basis", ""))
        cards.append(
            f"<div class='card' title='{basis}'><div class='muted'>{_e(name.replace('_', ' '))}"
            f"<span class='muted'> · {_e(metric.get('source'))}</span></div>"
            f"<div class='v'>{_e(metric.get('value'))}"
            f"<span class='muted'> {_e(metric.get('unit'))}</span></div>"
            f"<div class='muted' style='font-size:11px'>{basis[:70]}</div></div>"
        )
    return "<div class='grid'>" + "".join(cards) + "</div>"


def render_index(
    *,
    overview: dict[str, Any],
    scenarios: list[dict[str, Any]],
    recent: list[dict[str, Any]],
    lab_url: str,
    provider_label: str,
) -> str:
    options = "".join(
        f"<option value='{_e(scenario['scenario_id'])}'>{_e(scenario['title'])}</option>"
        for scenario in scenarios
    )
    scenario_rows = "".join(
        f"<tr><td>{_e(s['title'])}</td><td class='muted'>{_e(s['symptom'])}</td>"
        f"<td class='muted'>{_e(s['window_minutes'])}m</td></tr>"
        for s in scenarios
    )
    run_rows = (
        "".join(
            f"<tr><td><a href='/runs/{_e(run['run_id'])}'>{_e(run['run_id'][:8])}</a></td>"
            f"<td>{_status(run['status'])}</td><td class='muted'>{_e(run['model'] or '-')}</td>"
            f"<td class='muted'>{_e(run['duration_ms'] or '-')}"
            f" ms</td>"
            f"<td class='muted'>EUR {_e(round(float(run['cost_eur'] or 0), 6))}</td>"
            f"<td class='muted'>{_e((run['started_at'] or '')[:19])}</td></tr>"
            for run in recent
        )
        or "<tr><td colspan='6' class='muted'>No runs recorded yet.</td></tr>"
    )

    body = f"""
<h1>OpsPilot Platform</h1>
<p class='muted'>An AI agent that investigates infrastructure incidents, with the guardrails, traces and
evaluations that make its answers checkable.</p>

<div class='banner'>
Telemetry for every run below comes from the <strong>Incident Lab</strong> ({_e(lab_url)}): a real service
that is really slowed down or really fails when a fault is injected. It is labelled <code>demo</code>
everywhere it appears, so nothing here passes for live production data. Runs use the
<strong>{_e(provider_label)}</strong> provider.
</div>

<h2>Run an investigation</h2>
<form method='post' action='/runs'>
  <select name='scenario_id'>{options}</select>
  <select name='provider'>
    <option value='configured'>configured model</option>
    <option value='fake'>deterministic (free)</option>
  </select>
  <button type='submit'>Investigate</button>
</form>
<p class='muted' style='font-size:12px'>A restricted action stops at the human approval gate: the agent
proposes, the platform refuses to execute, and the run suspends with the proposal recorded.</p>

<h2>Incidents the agent can be pointed at</h2>
<table><tr><th>Incident</th><th>Symptom given to the agent</th><th>Window</th></tr>{scenario_rows}</table>

<h2>Platform (measured)</h2>
{_metric_cards(overview)}

<h2>Recent runs</h2>
<table><tr><th>Run</th><th>Status</th><th>Model</th><th>Duration</th><th>Cost</th><th>Started</th></tr>
{run_rows}</table>

<h2>API</h2>
<p class='muted' style='font-size:13px'>GET <a href='/api/v1/scenarios'>/api/v1/scenarios</a> ·
GET <a href='/api/v1/platform/overview'>/api/v1/platform/overview</a> ·
POST /api/v1/runs · GET /api/v1/runs/{{run_id}} · GET /api/v1/runs/{{run_id}}/trace ·
<a href='/docs'>/docs</a></p>
"""
    return _page("OpsPilot Platform", body)


def render_run(
    *,
    run_id: str,
    status: str,
    steps: list[dict[str, Any]],
    report: dict[str, Any] | None,
    trace: dict[str, Any],
) -> str:
    step_html = "".join(
        f"<div class='step {_e(step['status'])}'><strong>{_e(step['name'])}</strong> "
        f"{_status(step['status'])} <span class='muted'>{_e(step['duration_ms'])} ms</span>"
        f"<div class='muted'>{_e(step['summary'])}</div></div>"
        for step in steps
    )

    if report is None:
        detail = (
            "<p class='muted'>This run produced no report: the investigation did not reach that step.</p>"
        )
    else:
        diagnosis = report.get("diagnosis") or {}
        evidence = report.get("evidence") or []
        remediation = report.get("remediation") or {}
        flags = report.get("injection_flags") or []
        evidence_rows = "".join(
            f"<tr><td><code>{_e(item['evidence_id'])}</code></td><td class='muted'>{_e(item['kind'])}</td>"
            f"<td class='muted'>{_e(item['source'])}</td><td>{_e(item['summary'])}</td></tr>"
            for item in evidence
        )
        cited = ", ".join(_e(c) for c in diagnosis.get("evidence_ids", [])) or "none"
        unsupported = ", ".join(_e(c) for c in diagnosis.get("unsupported_evidence_ids", []))
        remediation_block = (
            f"<p>Proposed action <code>{_e(remediation.get('tool_name'))}</code> — "
            f"status <strong>{_e(remediation.get('status'))}</strong>"
            + (" (waiting for human approval)" if remediation.get("requires_approval") else "")
            + f"</p><p class='muted'>Bound to arguments hash "
            f"<code>{_e((remediation.get('arguments_hash') or 'n/a')[:16])}</code>, so approving this "
            f"cannot be replayed for a different request.</p>"
            if remediation
            else "<p class='muted'>No action proposed.</p>"
        )
        flag_block = (
            f"<h3 class='warn'>Instruction-shaped content in retrieved data ({len(flags)})</h3>"
            + "".join(f"<div class='banner'>{_e(flag)}</div>" for flag in flags)
            if flags
            else "<p class='muted'>No instruction-shaped content found in retrieved data.</p>"
        )
        detail = f"""
<h2>Diagnosis</h2>
<p>{_e(diagnosis.get("root_cause"))}</p>
<p class='muted'>Confidence {_e(diagnosis.get("confidence"))} · grounding
<strong>{_e(diagnosis.get("grounding_ratio"))}</strong> of cited evidence exists · cited: {cited}</p>
{f"<p class='bad'>Citations that do not exist: {unsupported}</p>" if unsupported else ""}
<h3>Remediation</h3>
{remediation_block}
<h3>Injection screen</h3>
{flag_block}
<h2>Evidence</h2>
<table><tr><th>Id</th><th>Kind</th><th>Source</th><th>Observation</th></tr>{evidence_rows}</table>
"""

    body = f"""
<h1>Run <code>{_e(run_id[:8])}</code></h1>
<p class='muted'><a href='/'>&larr; all runs</a> · status {_status(status)} ·
trace <code>{_e((trace.get("trace_id") or "none")[:16])}</code> ·
{_e(trace.get("span_count"))} spans · {_e(trace.get("duration_ms"))} ms</p>

<h2>Steps ({len(steps)})</h2>
{step_html}
{detail}
<h2>API</h2>
<p class='muted' style='font-size:13px'>
<a href='/api/v1/runs/{_e(run_id)}'>/api/v1/runs/{_e(run_id[:8])}</a> ·
<a href='/api/v1/runs/{_e(run_id)}/trace'>/api/v1/runs/{_e(run_id[:8])}/trace</a> ·
<a href='/api/v1/platform/overview'>/api/v1/platform/overview</a> · <a href='/docs'>/docs</a></p>
"""
    return _page(f"Run {run_id[:8]}", body)
