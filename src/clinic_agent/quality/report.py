"""Self-contained local HTML: measured results with inspectable evidence."""

from __future__ import annotations

import html
import json
from pathlib import Path


def write_html_report(report: dict, path: Path) -> None:
    escape = html.escape
    before, after = report["baseline"], report["candidate"]
    rows = []
    for scenario in report["scenarios"]:
        old, new = scenario["baseline"], scenario["candidate"]
        rows.append(
            f'<tr><td>{escape(scenario["name"])}<small>{escape(scenario["id"])} · {escape(scenario["split"])}</small></td><td class="{("pass" if old else "fail")}">{scenario["baseline_passed"]}/{scenario["total"]}</td><td class="{("pass" if new else "fail")}">{scenario["candidate_passed"]}/{scenario["total"]}</td></tr>'
        )
    gates = "".join(
        f'<li><span class="{("pass" if c["passed"] else "fail")}">{("PASS" if c["passed"] else "BLOCK")}</span><div><strong>{escape(c["name"].replace("_", " "))}</strong><p>{escape(c["detail"])}</p></div></li>'
        for c in report["gates"]["checks"]
    )
    details = []
    for version in (before, after):
        for row in version["rows"]:
            transcript = "".join(
                f'<div class="utterance {escape(entry["role"])}"><b>{escape(entry["role"])}</b><p>{escape(entry["content"])}</p></div>'
                for entry in row["transcript"]
            )
            scores = "".join(
                f'<li><span class="{("pass" if value == 1 else "fail")}">{escape(str(value))}</span> {escape(key)}<p>{escape(row["evidence"].get(key) or "No evaluator evidence.")}</p></li>'
                for key, value in row["scores"].items()
            )
            world = {
                key: row["world"].get(key, [])
                for key in ("appointments", "operations", "handoffs", "events", "faults_consumed")
            }
            details.append(
                f'<details><summary>{escape(version["version"])} · {escape(row["scenario_id"])} · repetition {row["repetition"]} · {escape(str(row["final_status"]))}</summary><div class="evidence"><section>{transcript}</section><section><h4>LangSmith feedback</h4><ul>{scores}</ul><h4>Independent world inspection</h4><pre>{escape(json.dumps(world, indent=2))}</pre></section></div></details>'
            )
    artifact = escape(json.dumps(report.get("improvement"), indent=2))
    limits = "".join(f"<li>{escape(item)}</li>" for item in report["limitations"])
    decision = (
        "Candidate accepted" if report["accepted"] else "Candidate rejected; baseline retained"
    )
    document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Clinic Agent — measured improvement</title><style>
:root{{color-scheme:dark;--bg:#0b1320;--panel:#131f31;--line:#2b3b52;--ink:#edf2fa;--muted:#a8b9d0;--mint:#7ee2c3;--rose:#ff9ba8}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,sans-serif}}main{{max-width:1180px;margin:auto;padding:48px 24px}}.eyebrow{{color:var(--mint);letter-spacing:.18em;text-transform:uppercase;font-size:12px}}h1{{font-size:clamp(30px,5vw,56px);line-height:1.12;letter-spacing:-.04em;margin:14px 0}}h2{{font-size:25px;margin:38px 0 16px}}p{{color:var(--muted)}}.lede{{max-width:760px}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:32px 0}}.card{{border:1px solid var(--line);border-radius:18px;padding:22px;background:linear-gradient(145deg,var(--panel),#102334)}}.number{{font-size:42px;font-weight:700;letter-spacing:-.04em;color:var(--mint)}}.card p{{margin:0}}.pill{{display:inline-block;border:1px solid var(--line);border-radius:30px;padding:7px 14px;margin:5px 5px 0 0;color:var(--muted);font-size:13px}}table{{width:100%;border-collapse:collapse;background:var(--panel);border-radius:16px;overflow:hidden}}th,td{{padding:14px 18px;border-bottom:1px solid var(--line);text-align:left}}th{{color:var(--muted);font-size:13px}}small{{display:block;color:var(--muted);font-size:12px}}.pass{{color:var(--mint)}}.fail{{color:var(--rose)}}.gates{{list-style:none;padding:0}}.gates li{{display:flex;gap:20px;padding:15px 0;border-bottom:1px solid var(--line)}}.gates p{{margin:4px 0;font-size:14px}}pre{{background:#07101b;border:1px solid var(--line);border-radius:12px;padding:18px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:500px;overflow:auto}}details{{border:1px solid var(--line);border-radius:12px;margin:10px 0;background:var(--panel)}}summary{{cursor:pointer;padding:15px;font-size:14px}}.evidence{{padding:16px;display:grid;grid-template-columns:1fr 1fr;gap:24px}}.utterance{{padding:12px 15px;border-radius:12px;background:#1b2c42;margin:8px 0}}.utterance.user{{background:#14332f}}.utterance b{{font-size:12px;text-transform:uppercase;color:var(--mint)}}.utterance p{{margin:4px 0;color:var(--ink);font-size:14px}}.evidence ul{{padding-left:20px;font-size:13px}}.evidence li p{{font-size:12px}}footer{{margin-top:40px;border-top:1px solid var(--line);padding-top:20px;color:var(--muted);font-size:13px}}@media(max-width:700px){{.cards,.evidence{{grid-template-columns:1fr}}main{{padding:25px 14px}}th,td{{padding:10px}}}}@media print{{body{{background:white;color:black}}.card,table,details{{background:white}}pre{{background:#eee;color:black}}p,small{{color:#444}}}}
</style></head><body><main><div class="eyebrow">Clinic agent · evaluation evidence</div><h1>A safer conversation.<br>A measured improvement.</h1><p class="lede">The same synthetic patient stories ran through both behavior versions. LangSmith scored the conversation, persisted appointments, staff tickets, and booking-operation history. Every failure remains visible.</p><span class="pill">{escape(report["engine"]["platform"])}</span><span class="pill">{escape(report["engine"].get("actor_label", report["engine"]["actor"]))}</span><span class="pill">{escape(report["dataset"]["version"])}</span><span class="pill">{report["dataset"]["count"]} scenarios × {report["dataset"]["repetitions"]} repetitions</span><div class="cards"><div class="card"><small>BASELINE · {escape(before["version"])}</small><div class="number">{before["resolution_rate"]:.1%}</div><p>{before["resolution_passed"]}/{before["total"]} verified resolutions</p></div><div class="card"><small>CANDIDATE · {escape(after["version"])}</small><div class="number">{after["resolution_rate"]:.1%}</div><p>{after["resolution_passed"]}/{after["total"]} verified resolutions</p></div><div class="card"><small>CANDIDATE SAFETY</small><div class="number">{after["critical_violations"]}</div><p>critical check violations</p></div></div><h2>{escape(decision)}</h2><p>{escape(report["note"])} Acceptance writes an artifact; activating new patient conversations is a separate explicit action.</p><ul class="gates">{gates}</ul><h2>Same scenarios, before and after</h2><table><thead><tr><th>Patient story</th><th>Baseline</th><th>Candidate</th></tr></thead><tbody>{"".join(rows)}</tbody></table><h2>The structured change</h2><p>A definite slot conflict may refresh availability once. The patient must confirm the replacement. An unknown booking outcome still requires checking the original operation.</p><pre>{artifact}</pre><h2>Inspect the actual runs</h2><p>Open any run to review its conversation, separate feedback keys, and persisted world. Scores describe the configured demonstration, not clinical readiness.</p>{"".join(details)}<h2>What this evidence cannot prove</h2><ul>{limits}</ul><footer>Generated {escape(report["generated_at"])} · Self-contained local file; no external images, scripts, fonts, or publishing.</footer></main></body></html>"""
    path.write_text(document)
