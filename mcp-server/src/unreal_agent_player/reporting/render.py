from __future__ import annotations

import html as _html
import json
from typing import Any

_CSS = """
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,Segoe UI,sans-serif;background:#0f1115;color:#e6e6e6}
.header{padding:20px 28px}.header.status-pass{background:#13351f;border-bottom:3px solid #2ecc71}
.header.status-fail{background:#3a1717;border-bottom:3px solid #e74c3c}
.header.status-incomplete{background:#33301a;border-bottom:3px solid #d4af37}
.verdict{font-size:26px;font-weight:700}.meta{opacity:.8;margin-top:4px}
.quote{margin-top:10px;font-style:italic;opacity:.85}
.tabs{display:flex;gap:2px;background:#171a21;padding:0 16px;border-bottom:1px solid #262b35}
.tab{padding:12px 18px;cursor:pointer;color:#9aa4b2;border-bottom:2px solid transparent}
.tab.active{color:#fff;border-bottom-color:#5b9dff}
.panel{display:none;padding:24px 28px}.panel.active{display:block}
.check{padding:6px 0}.pass::before{content:"\\2713 ";color:#2ecc71}.fail::before{content:"\\2717 ";color:#e74c3c}
.evi{opacity:.6;margin-left:8px}
.grid{display:flex;flex-wrap:wrap;gap:12px}.thumb{width:240px;cursor:pointer}
.thumb img{width:100%;border:1px solid #2a2f3a;border-radius:6px;display:block}
.cap{font-size:12px;opacity:.8;margin-top:4px}
table{border-collapse:collapse;width:100%}td,th{padding:6px 10px;border-bottom:1px solid #232833;text-align:left;font-size:13px}
tr.err td{background:#2a1414}.badge{font-size:11px;padding:1px 6px;border-radius:4px}
.badge.ok{background:#173d24;color:#7fe0a0}.badge.no{background:#3d1717;color:#e08f8f}
.lb{position:fixed;inset:0;background:rgba(0,0,0,.9);display:none;align-items:center;justify-content:center}
.lb.show{display:flex}.lb img{max-width:92%;max-height:92%}
.sub{margin:18px 0 6px;font-weight:600;color:#cfd6df}.mono{font-family:Consolas,monospace}
"""

_JS = """
function showTab(n){document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.tab===n));
document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p.dataset.panel===n));}
function lb(src){var b=document.getElementById('lb');document.getElementById('lbimg').src=src;b.classList.add('show');}
document.addEventListener('DOMContentLoaded',()=>{showTab('Overview');
document.getElementById('lb').addEventListener('click',e=>e.currentTarget.classList.remove('show'));});
"""


def _e(s: Any) -> str:
    return _html.escape(str("" if s is None else s))


def _overview(d: dict) -> str:
    parts = [f"<p>{_e(d.get('summary'))}</p>"]
    for n in d.get("notes", []):
        parts.append(f"<p class='evi'>{_e(n.get('text'))}</p>")
    parts.append("<div class='sub'>Checks</div>")
    for a in d.get("assertions", []):
        cls = "pass" if a.get("passed") else "fail"
        parts.append(f"<div class='check {cls}'>{_e(a.get('label'))}"
                     f"<span class='evi'>{a.get('evidence', '')}</span></div>")
    return "".join(parts)


def _shots(d: dict) -> str:
    cells = []
    for sh in d.get("screenshots", []):
        if sh.get("missing") or not sh.get("file"):
            cells.append(f"<div class='thumb'><div class='cap'>[missing] {_e(sh.get('caption'))}</div></div>")
            continue
        f = _e(sh["file"])
        cells.append(f"<div class='thumb' onclick=\"lb('{f}')\"><img src='{f}'>"
                     f"<div class='cap'>{_e(sh.get('caption'))} <span class='evi'>{_e(sh.get('t'))}</span></div></div>")
    return f"<div class='grid'>{''.join(cells) or 'No screenshots.'}</div>"


def _timeline(d: dict) -> str:
    rows = []
    for c in d.get("timeline", []):
        badge = "<span class='badge ok'>ok</span>" if c.get("ok") else "<span class='badge no'>err</span>"
        cls = "" if c.get("ok") else "err"
        rows.append(f"<tr class='{cls}'><td class='mono'>{_e(c.get('t'))}</td><td>{_e(c.get('tool'))}</td>"
                    f"<td class='mono'>{_e(json.dumps(c.get('args', {})))}</td><td>{badge}</td>"
                    f"<td>{_e(c.get('ms'))}ms</td></tr>")
    return ("<table><tr><th>time</th><th>tool</th><th>args</th><th></th><th>ms</th></tr>"
            f"{''.join(rows)}</table>")


def _diag(d: dict) -> str:
    parts = []
    env = d.get("env") or {}
    if env:
        b = env.get("bridge") or {}
        parts.append("<div class='sub'>Environment</div>"
                     f"<div class='mono'>plugin {_e(env.get('plugin_version'))} &middot; "
                     f"RC {_e(b.get('rc_reachable'))} &middot; exec {_e(b.get('remote_exec_reachable'))}</div>")
    perf = d.get("perf")
    if perf:
        parts.append("<div class='sub'>Perf</div><div class='mono'>"
                     + " &middot; ".join(f"{_e(k)}={_e(v)}" for k, v in perf.items()) + "</div>")
    logs = d.get("logs") or []
    if logs:
        parts.append(f"<div class='sub'>Log warnings/errors ({len(logs)})</div>")
        for l in logs:
            parts.append(f"<div class='mono'>[{_e(l.get('verbosity'))}] {_e(l.get('category'))}: {_e(l.get('line'))}</div>")
    return "".join(parts) or "No diagnostics captured."


def render(data: dict) -> str:
    status = data.get("status", "running")
    verdict = {"pass": "PASS", "fail": "FAIL"}.get(status, status.upper())
    meta = f"{_e(data.get('started'))} &middot; {_e(data.get('duration_s'))}s &middot; {_e(data.get('project'))}"
    panels = {
        "Overview": _overview(data), "Screenshots": _shots(data),
        "Timeline": _timeline(data), "Diagnostics": _diag(data),
    }
    tabs = "".join(f'<div class="tab" data-tab="{n}" onclick="showTab(\'{n}\')">{n}</div>' for n in panels)
    bodies = "".join(f'<div class="panel" data-panel="{n}">{html}</div>' for n, html in panels.items())
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{_e(data.get('task'))}</title>
<style>{_CSS}</style></head><body>
<div class="header status-{_e(status)}">
  <div class="verdict">{verdict}&nbsp;&nbsp;{_e(data.get('task'))}</div>
  <div class="meta">{meta}</div>
  <div class="quote">"{_e(data.get('quote'))}"</div>
</div>
<div class="tabs">{tabs}</div>
{bodies}
<div class="lb" id="lb"><img id="lbimg" src=""></div>
<script>{_JS}</script>
</body></html>"""
