"""Local dashboard: one web page, on this PC only, showing what Bambu
Companion is doing — the printer and AMS live, slices and multi-colour
builds as they run, finished jobs with time and filament per colour,
the recommendation history, and a picture of each model.

It is started by the Claude connector (so it can show the connector's
running tasks) at http://127.0.0.1:8421/, or on its own with

    python -m bambu_companion.dashboard

which shows the same page from the activity log and job history (tasks
the connector is running at that moment appear once they finish).

Safety, the same rules as the rest of the project:
- it listens on 127.0.0.1 only, and refuses requests whose Host header
  isn't localhost (so a web page elsewhere can't reach it by DNS tricks);
- it only answers GET: nothing on the page can slice, change a setting,
  or send anything to the printer;
- pictures are served only for files that already appear in the
  activity log or job history — never an arbitrary path from the URL;
- printer details stay out of it: the printer section shows the same
  status the connector reports, without address, serial or access code.

Standard library only (plus numpy, already required, for pictures).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .bridge.studio_config import load_studio_config
from .preview import preview_png

DEFAULT_PORT = 8421
ENV_PORT = "BAMBU_COMPANION_DASHBOARD_PORT"
ENV_DISABLE = "BAMBU_COMPANION_DASHBOARD"   # set to 0 to turn the dashboard off


def dashboard_port() -> int:
    try:
        return int(os.environ.get(ENV_PORT, DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def build_state(service: Any) -> dict[str, Any]:
    """Everything the page shows, in one JSON document."""
    cfg = load_studio_config()
    try:
        jobs = service.job_history(25)
    except Exception:  # noqa: BLE001 - a broken history file must not blank the page
        jobs = []
    return {
        "activity": service.activity(40),
        "jobs": jobs,
        "printer": service.printer_snapshot(),
        "studio": {
            "working_strategy": cfg.get("working_strategy"),
            "default_machine": cfg.get("default_machine"),
            "default_process": cfg.get("default_process"),
            "default_filament": cfg.get("default_filament"),
            "default_bed_type": cfg.get("default_bed_type"),
        },
    }


def known_files(service: Any) -> set[str]:
    """Files the page may show a picture of: the ones jobs were about."""
    out = set()
    for rec in service.activity(200):
        for k in ("model_path", "output_path"):
            if rec.get(k):
                out.add(os.path.normcase(str(rec[k])))
    try:
        jobs = service.job_history(100)
    except Exception:  # noqa: BLE001
        jobs = []
    for job in jobs:
        for k in ("model_file", "output_file"):
            if job.get(k):
                out.add(os.path.normcase(str(job[k])))
    return out


def make_handler(service: Any, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "BambuCompanionDashboard"

        def log_message(self, fmt, *args):  # keep the connector's log quiet
            return

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data: Any, code: int = 200) -> None:
            self._send(code, json.dumps(data, default=str, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if (self.headers.get("Host") or "").lower() not in allowed_hosts:
                return self._send(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if url.path == "/api/state":
                try:
                    return self._json(build_state(service))
                except Exception as exc:  # noqa: BLE001
                    return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            if url.path == "/api/preview":
                wanted = (parse_qs(url.query).get("file") or [""])[0]
                try:
                    allowed = known_files(service)
                except Exception:  # noqa: BLE001
                    allowed = set()
                if not wanted or os.path.normcase(wanted) not in allowed:
                    return self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")
                path = Path(wanted)
                if not path.is_file():
                    return self._send(HTTPStatus.NOT_FOUND, b"File no longer exists", "text/plain")
                try:
                    return self._send(200, preview_png(path), "image/png", "private, max-age=300")
                except Exception:  # noqa: BLE001 - a model we can't draw just has no picture
                    return self._send(HTTPStatus.UNPROCESSABLE_ENTITY, b"No preview", "text/plain")
            return self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")

        def do_POST(self):
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"Read-only", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


class Dashboard:
    def __init__(self, service: Any, port: int | None = None):
        self.service = service
        self.port = dashboard_port() if port is None else port
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        port = self.httpd.server_address[1] if self.httpd else self.port
        return f"http://127.0.0.1:{port}/"

    def start(self) -> bool:
        """Starts serving in a background thread. False (and nothing
        else happens) if the port is taken — e.g. a second connector."""
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), make_handler(self.service, self.port))
        except OSError:
            self.httpd = None
            return False
        if self.port == 0:  # tests: rebuild the handler with the real port
            real = self.httpd.server_address[1]
            self.httpd.RequestHandlerClass = make_handler(self.service, real)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="companion-dashboard", daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


def maybe_start(service: Any) -> Dashboard | None:
    """Used by the connector: start unless turned off; never raise."""
    if os.environ.get(ENV_DISABLE, "1").strip() in ("0", "false", "off", "no"):
        return None
    dash = Dashboard(service)
    try:
        if dash.start():
            service.dashboard_url = dash.url
            print(f"Bambu Companion dashboard: {dash.url}", file=sys.stderr)
            return dash
        print(f"Dashboard port {dash.port} is in use; dashboard not started here.", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - the connector must keep working without it
        print(f"Dashboard failed to start: {exc}", file=sys.stderr)
    return None


def main() -> int:
    from .service import CompanionService

    service = CompanionService()
    dash = Dashboard(service)
    if not dash.start():
        url = f"http://127.0.0.1:{dash.port}/"
        print(f"Something is already serving {url} — probably the dashboard the Claude connector "
              "started. Opening it.")
        webbrowser.open(url)
        return 0
    print(f"Bambu Companion dashboard running at {dash.url}  (Ctrl+C to stop)")
    webbrowser.open(dash.url)
    try:
        dash.thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        dash.stop()
        service.close()
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bambu Companion</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#1d2330;--mute:#6b7383;--line:#e3e6ec;--accent:#00ae42;
--warn:#c77700;--bad:#c62f2f;--chip:#eef0f4;--shadow:0 1px 2px rgba(20,30,50,.06),0 4px 14px rgba(20,30,50,.05)}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--ink:#e8ebf1;--mute:#9099aa;
--line:#2a313d;--chip:#252c38;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:16px 24px;border-bottom:1px solid var(--line);background:var(--card)}
header h1{font-size:17px;margin:0;font-weight:650}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mute);display:inline-block}
.dot.on{background:var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 20%,transparent)}
.sub{color:var(--mute);font-size:12.5px}
main{display:grid;grid-template-columns:minmax(300px,380px) 1fr;gap:18px;padding:18px 24px;max-width:1500px;margin:0 auto}
@media (max-width:900px){main{grid-template-columns:1fr;padding:14px}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);padding:16px;margin-bottom:18px}
.card h2{font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--mute);margin:0 0 12px;font-weight:600}
.big{font-size:22px;font-weight:650}
.bar{height:8px;border-radius:5px;background:var(--chip);overflow:hidden;margin:10px 0 6px}
.bar>i{display:block;height:100%;background:var(--accent);border-radius:5px;transition:width .6s}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin-top:10px}
.kv span:nth-child(odd){color:var(--mute)}
.slots{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:6px}
.slot{border:1px solid var(--line);border-radius:10px;padding:8px;text-align:center;font-size:12px}
.sw{width:28px;height:28px;border-radius:50%;margin:2px auto 6px;border:1px solid rgba(0,0,0,.15)}
.msg{color:var(--mute);font-size:13px}
.job{display:grid;grid-template-columns:96px 1fr;gap:14px;padding:12px 0;border-top:1px solid var(--line)}
.job:first-of-type{border-top:0;padding-top:0}
.thumb{width:96px;height:96px;border-radius:10px;background:var(--chip);object-fit:contain;border:1px solid var(--line)}
.title{font-weight:600;word-break:break-word}
.pill{display:inline-block;font-size:11.5px;padding:1px 8px;border-radius:999px;background:var(--chip);color:var(--mute);margin-right:6px;vertical-align:1px}
.pill.run{background:color-mix(in srgb,#2f7cf6 18%,transparent);color:#2f7cf6}
.pill.done{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent)}
.pill.failed{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad)}
.stats{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:6px}
.stats b{font-weight:650}
.fil{display:inline-flex;align-items:center;gap:6px;margin-right:12px}
.fil i{width:12px;height:12px;border-radius:50%;display:inline-block;border:1px solid rgba(0,0,0,.2)}
.path{color:var(--mute);font-size:12px;word-break:break-all}
.err{color:var(--bad);font-size:13px;margin-top:4px}
.warnline{color:var(--warn);font-size:13px;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600;border-top:0}
.spin{width:12px;height:12px;border:2px solid #2f7cf6;border-right-color:transparent;border-radius:50%;display:inline-block;animation:r .8s linear infinite;vertical-align:-1px;margin-right:6px}
@keyframes r{to{transform:rotate(360deg)}}
.empty{color:var(--mute);padding:8px 0}
</style>
</head>
<body>
<header><span class="dot" id="live"></span><h1>Bambu Companion</h1><span class="sub" id="studio"></span>
<span class="sub" style="margin-left:auto" id="updated"></span></header>
<main>
<section>
  <div class="card"><h2>Printer</h2><div id="printer"><div class="empty">Loading…</div></div></div>
  <div class="card"><h2>AMS</h2><div id="ams"><div class="empty">—</div></div></div>
</section>
<section>
  <div class="card"><h2>Activity</h2><div id="activity"><div class="empty">Loading…</div></div></div>
  <div class="card"><h2>Recommendation &amp; slice history</h2><div id="jobs"></div></div>
</section>
</main>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const base=p=>String(p||"").split(/[\\/]/).pop();
const colour=c=>{c=String(c||"");if(/^#?[0-9a-f]{6}([0-9a-f]{2})?$/i.test(c)){c=c.replace("#","");return "#"+c.slice(0,6)}return "transparent"};
const when=t=>{if(!t)return"";const d=new Date(t);return isNaN(d)?esc(t):d.toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"})};
const t1=v=>v==null?"—":(Math.round(v*10)/10).toString();
const KIND={slice:"Slice",build_multicolour:"Multi-colour build",slice_check:"Slice check"};
function thumb(rec){const f=rec.output_path||rec.model_path;if(!f)return '<div class="thumb"></div>';
  return `<img class="thumb" loading="lazy" alt="" src="/api/preview?file=${encodeURIComponent(f)}" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb'}))">`}
function renderPrinter(p){
  if(!p||p.configured===false){$("#printer").innerHTML='<div class="msg">No printer set up on this PC. Run <code>python -m bambu_companion.bridge.printer_setup</code> once to see live status here.</div>';$("#ams").innerHTML='<div class="empty">—</div>';return}
  if(p.message&&p.state==null&&p.progress_percent==null){$("#printer").innerHTML=`<div class="msg">${esc(p.message)}</div>`;$("#ams").innerHTML='<div class="empty">—</div>';return}
  const st=(p.state||"unknown").toString(), pct=p.progress_percent??0;
  const m=p.remaining_time_min, left=m!=null?(m>=60?`${Math.floor(m/60)}h ${m%60}m left`:`${m}m left`):"";
  $("#printer").innerHTML=`<div class="big">${esc(st.charAt(0)+st.slice(1).toLowerCase())}</div>
   <div class="sub">${esc(p.print_name||"")}</div>
   <div class="bar"><i style="width:${Math.max(0,Math.min(100,pct))}%"></i></div>
   <div class="sub">${pct}% ${p.current_layer!=null?`· layer ${p.current_layer}/${p.total_layers??"?"}`:""} ${left?"· "+left:""}</div>
   <div class="kv"><span>Nozzle</span><span>${t1(p.nozzle_temp_c)} °C</span><span>Bed</span><span>${t1(p.bed_temp_c)} °C</span>
   <span>Connection</span><span>${p.connected?"live":"not live"}${p.seconds_since_last_update!=null?` · ${p.seconds_since_last_update}s ago`:""}</span></div>
   ${(p.warnings||[]).map(w=>`<div class="warnline">${esc(w)}</div>`).join("")}
   ${p.message?`<div class="msg" style="margin-top:8px">${esc(p.message)}</div>`:""}`;
  const slots=p.ams_slots||[];
  $("#ams").innerHTML=slots.length?`<div class="slots">${slots.map(s=>`<div class="slot"><div class="sw" style="background:${colour(s.color)}"></div>
     <div><b>${s.slot_index!=null?"Slot "+(s.slot_index+1):""}</b></div><div>${esc(s.filament_type||"empty")}</div>
     ${s.humidity!=null?`<div class="sub">humidity ${esc(s.humidity)}</div>`:""}</div>`).join("")}</div>`:'<div class="empty">No AMS data yet.</div>';
}
function renderActivity(list){
  if(!list.length){$("#activity").innerHTML='<div class="empty">Nothing yet. Slices and multi-colour builds Claude runs will show up here as they happen.</div>';return}
  $("#activity").innerHTML=list.map(r=>{
    const st=r.state==="running"?'<span class="pill run"><span class="spin"></span>running</span>':`<span class="pill ${esc(r.state)}">${esc(r.state)}</span>`;
    let stats="";
    for(const p of (r.plates||[])){
      stats+=`<div class="stats"><span>⏱ <b>${esc(p.print_time||"—")}</b></span><span><b>${p.filament_g??"—"}</b> g</span>
        ${p.layers?`<span>${p.layers} layers · ${p.max_z_mm} mm</span>`:""}${p.objects?`<span>${p.objects} object${p.objects>1?"s":""}</span>`:""}
        ${p.support_used?'<span>supports</span>':''}</div>
        <div class="stats">${(p.filaments||[]).map(f=>`<span class="fil"><i style="background:${colour(f.color)}"></i>slot ${f.slot} ${esc(f.type||"")} · ${f.used_g} g</span>`).join("")}</div>`}
    if(r.objects)stats+=`<div class="stats">${r.objects.map(o=>`<span>${esc(o.name)} × ${o.copies}</span>`).join("")}</div>`;
    const plate=r.bed_type?`<span class="pill">${esc(r.bed_type)}</span>`:"";
    return `<div class="job">${thumb(r)}<div><div>${st}<span class="pill">${esc(KIND[r.kind]||r.kind)}</span>${plate}</div>
      <div class="title" style="margin-top:4px">${esc(r.label)}</div>${stats}
      ${r.error?`<div class="err">${esc(r.error)}</div>`:""}${r.warning?`<div class="warnline">${esc(r.warning)}</div>`:""}
      ${r.orientation_warning?`<div class="warnline">${esc(r.orientation_warning)}</div>`:""}
      <div class="path">${r.output_path?"→ "+esc(r.output_path):esc(r.model_path||"")}</div>
      <div class="sub">${when(r.started_at)}${r.elapsed_s!=null?` · ${r.elapsed_s}s`:""}</div></div></div>`}).join("");
}
function renderJobs(jobs){
  if(!jobs.length){$("#jobs").innerHTML='<div class="empty">No recommendations yet.</div>';return}
  $("#jobs").innerHTML=`<table><tr><th>When</th><th>Model</th><th>Goal</th><th>Approved</th><th>Result</th><th>Outcome</th></tr>
   ${jobs.map(j=>`<tr><td>${when(j.started_at)}</td><td>${esc(base(j.model_file))}</td><td>${esc(j.goal||"")} ${esc(j.material||"")}</td>
   <td>${esc((j.approved_keys||[]).join(", ")||"—")}</td><td>${esc(j.slice_result||"—")}</td><td>${esc(j.outcome_note||"")}</td></tr>`).join("")}</table>`;
}
let lastJson="";
async function tick(){
  try{const r=await fetch("/api/state",{cache:"no-store"});const s=await r.json();
    $("#live").className="dot on";$("#updated").textContent="updated "+new Date().toLocaleTimeString();
    const j=JSON.stringify(s);if(j!==lastJson){lastJson=j;
      const st=s.studio||{};$("#studio").textContent=[st.default_machine,st.default_bed_type].filter(Boolean).join(" · ");
      renderPrinter(s.printer);renderActivity(s.activity||[]);renderJobs(s.jobs||[]);}
  }catch(e){$("#live").className="dot";$("#updated").textContent="dashboard not reachable — is the connector running?"}
}
tick();setInterval(tick,2000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
