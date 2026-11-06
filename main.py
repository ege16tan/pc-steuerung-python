"""PC-Agent: schaltet einen Windows-PC im Heimnetz (FastAPI).

Start:
    python -m uvicorn main:app --host 0.0.0.0 --port 8000

Zugriff vom Handy: Benutzername + Passwort (Basic Auth). Beim ersten Start
werden die Zugangsdaten in der Web-Oberflaeche unter http://localhost:8000/
eingerichtet (nur am PC moeglich). Lokale Zugriffe (PC selbst: Tray, Web-UI)
brauchen keinen Login. Der alte PC_AGENT_TOKEN-Schutz bleibt als Fallback.

Sicherheitsschalter: PC_AGENT_SIMULATE=1 (Standard) fuehrt nichts aus, sondern
schreibt nur ins Ereignis-Log. Erst mit PC_AGENT_SIMULATE=0 wird wirklich
heruntergefahren/gewischt - der Schalter existiert, damit ein Test vom Handy
den PC nicht sofort ausschaltet.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

PLANS = {
    "high": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
}
CREATE_NO_WINDOW = 0x08000000

TOKEN = os.environ.get("PC_AGENT_TOKEN", "")
SIMULATE = os.environ.get("PC_AGENT_SIMULATE", "1") != "0"

app = FastAPI(title="PC Agent", version="0.1")
_lock = threading.Lock()
_events = deque(maxlen=50)
_pending = {"at": None, "seconds": 0}

CREDENTIALS_FILE = Path(__file__).with_name("credentials.json")


def _load_credentials() -> dict | None:
    try:
        data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("user") and data.get("salt") and data.get("hash"):
        return data
    return None


_credentials = _load_credentials()


def _log(action: str, detail: str, ok: bool = True) -> None:
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action,
        "detail": detail,
        "ok": ok,
    }
    with _lock:
        _events.appendleft(entry)
    print(f"[{entry['time']}] {action} {detail} ok={ok}", flush=True)


def _run(cmd: list[str], real: bool = False) -> tuple[int, str]:
    if SIMULATE and not real:
        return 0, "simuliert: " + " ".join(cmd)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _wake(mac: str, port: int = 9) -> bool:
    mac_clean = mac.strip().lower().replace("-", ":")
    parts = mac_clean.split(":")
    if len(parts) != 6:
        return False
    mac_bytes = bytes(int(p, 16) for p in parts)
    if SIMULATE:
        return True
    packet = b"\xff" * 6 + mac_bytes * 16
    broadcast = os.environ.get("PC_WOL_BROADCAST", "192.168.1.255")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))
    return True


def _active_plan() -> str:
    # Nur lesen, deshalb auch im Simulationsmodus echt ausgefuehrt.
    code, out = _run(["powercfg", "/getactivescheme"], real=True)
    if code != 0:
        return "unbekannt"
    for name, guid in PLANS.items():
        if guid in out.lower():
            return name
    return "anderer"


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _check_basic(authorization: str) -> bool:
    if not authorization.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(authorization[6:]).decode("utf-8")
        user, sep, password = raw.partition(":")
    except Exception:
        return False
    if not sep or not _credentials or user != _credentials.get("user"):
        return False
    try:
        salt = bytes.fromhex(_credentials["salt"])
        expected = bytes.fromhex(_credentials["hash"])
    except (KeyError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return hmac.compare_digest(actual, expected)


def require_token(
    request: Request,
    x_token: str = Header(default=""),
    authorization: str = Header(default=""),
) -> None:
    # Lokale Zugriffe (PC selbst: Tray, Web-UI) brauchen keinen Login.
    if _is_local(request):
        return
    # Neue Anmeldung per Benutzername/Passwort (Basic Auth).
    if _credentials:
        if not _check_basic(authorization):
            raise HTTPException(
                status_code=401,
                detail="Login fehlt oder falsch",
                headers={"WWW-Authenticate": 'Basic realm="PC Agent"'},
            )
        return
    # Fallback: alter Token-Schutz.
    if not TOKEN:
        return
    if not secrets.compare_digest(x_token, TOKEN):
        raise HTTPException(status_code=403, detail="Token fehlt oder falsch")


class SetupRequest(BaseModel):
    user: str
    password: str


@app.get("/setup-status")
def setup_status() -> dict:
    return {"required": _credentials is None}


@app.post("/setup")
def setup(request: Request, body: SetupRequest) -> dict:
    global _credentials
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Einrichtung nur am PC (localhost) moeglich")
    if _credentials is not None:
        raise HTTPException(status_code=409, detail="Zugangsdaten sind bereits eingerichtet")
    user = body.user.strip()
    password = body.password
    if not user:
        raise HTTPException(status_code=400, detail="Benutzername fehlt")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Passwort muss mindestens 8 Zeichen haben")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    data = {"user": user, "salt": salt.hex(), "hash": digest.hex()}
    try:
        CREDENTIALS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Speichern fehlgeschlagen: {e}")
    _credentials = data
    _log("setup", f"Zugangsdaten fuer {user} eingerichtet")
    return {"ok": True, "user": user}


@app.get("/status")
def status(_: None = Depends(require_token)) -> dict:
    with _lock:
        pending = dict(_pending)
    if pending["at"]:
        pending["seconds"] = max(0, round(pending["at"] - time.time()))
    return {
        "online": True,
        "host": os.environ.get("COMPUTERNAME", "?"),
        "simulate": SIMULATE,
        "power_plan": _active_plan(),
        "pending_shutdown": pending["seconds"] if pending["at"] else 0,
        "setup_required": _credentials is None,
    }


@app.post("/shutdown")
def shutdown(
    delay: int = Query(0, ge=0, le=86400), _: None = Depends(require_token)
) -> dict:
    code, out = _run(["shutdown", "/s", "/t", str(delay)])
    if code == 0:
        with _lock:
            _pending.update(at=time.time() + delay, seconds=delay)
        _log("shutdown", f"in {delay}s" if delay else "sofort")
        return {"ok": True, "detail": out or f"Herunterfahren in {delay}s"}
    _log("shutdown", out or "Fehler", ok=False)
    raise HTTPException(status_code=500, detail=out or "shutdown fehlgeschlagen")


@app.post("/abort")
def abort(_: None = Depends(require_token)) -> dict:
    code, out = _run(["shutdown", "/a"])
    with _lock:
        _pending.update(at=None, seconds=0)
    _log("abort", out or "abgebrochen", ok=code == 0)
    return {"ok": code == 0, "detail": out or "Herunterfahren abgebrochen"}


@app.post("/hibernate")
def hibernate(_: None = Depends(require_token)) -> dict:
    code, out = _run(["shutdown", "/h"])
    _log("hibernate", out or "Ruhezustand", ok=code == 0)
    if code != 0:
        raise HTTPException(status_code=500, detail=out or "Ruhezustand fehlgeschlagen")
    return {"ok": True, "detail": out or "Ruhezustand"}


@app.post("/power-plan")
def power_plan(
    name: str = Query(..., pattern="^(high|balanced|saver)$"),
    _: None = Depends(require_token),
) -> dict:
    code, out = _run(["powercfg", "/setactive", PLANS[name]])
    _log("power-plan", f"{name} {out}".strip(), ok=code == 0)
    if code != 0:
        raise HTTPException(status_code=500, detail=out or "powercfg fehlgeschlagen")
    return {"ok": True, "plan": name, "detail": out}


@app.post("/wake")
def wake(
    mac: str = Query(..., description="MAC-Adresse des Ziel-PCs, z. B. AA:BB:CC:DD:EE:FF"),
    port: int = Query(9, ge=1, le=65535),
    _: None = Depends(require_token),
) -> dict:
    ok = _wake(mac, port)
    detail = f"Magic Packet an {mac} (Port {port})" + (" (simuliert)" if SIMULATE else "")
    _log("wake", detail, ok=ok)
    if not ok:
        raise HTTPException(status_code=500, detail="Wake fehlgeschlagen")
    return {"ok": True, "mac": mac, "port": port}


@app.post("/ping")
def ping(_: None = Depends(require_token)) -> dict:
    _log("ping", "Verbindungstest vom Handy")
    return {"ok": True, "detail": "Verbindung steht"}


@app.get("/events")
def events(_: None = Depends(require_token)) -> dict:
    with _lock:
        return {"events": list(_events)}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PC Agent</title>
<style>
 :root{
  --bg:#0f1115; --card:#171a21; --card2:#1e222b; --line:#232a38;
  --text:#e6e8ee; --muted:#9aa3b2; --green:#2ecc71; --red:#e74c3c;
  --amber:#f1c40f; --accent:#3b82f6; --radius:14px;
 }
 *{box-sizing:border-box}
 body{margin:0;padding:24px;min-height:100vh;color:var(--text);
  font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
  background:radial-gradient(1100px 500px at 15% -10%,#1a2233 0%,var(--bg) 60%)}
 .wrap{max-width:1000px;margin:0 auto}
 header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:20px}
 h1{font-size:24px;margin:0;letter-spacing:.3px}
 .badge{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;
  background:var(--card);border:1px solid var(--line);font-size:13px;color:var(--muted)}
 .dot{width:10px;height:10px;border-radius:50%;background:var(--amber);box-shadow:0 0 10px currentColor}
 .dot.ok{background:var(--green)}
 .dot.err{background:var(--red)}
 .grid{display:grid;gap:16px;grid-template-columns:1fr}
 @media(min-width:820px){.grid{grid-template-columns:1.1fr .9fr}}
 .card{background:linear-gradient(180deg,var(--card),#141821);border:1px solid var(--line);
  border-radius:var(--radius);padding:18px}
 .card h2{margin:0 0 14px;font-size:13px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted)}
 #state{font-size:15px;line-height:1.7}
 #state b{color:#fff}
 .kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin-top:12px;font-size:14px}
 .kv span:first-child{color:var(--muted)}
 .actions{display:flex;flex-wrap:wrap;gap:10px}
 button{font:inherit;font-size:14px;padding:10px 14px;border-radius:10px;border:1px solid #2b3442;
  background:var(--card2);color:var(--text);cursor:pointer;transition:.15s ease}
 button:hover{background:#262d3a;transform:translateY(-1px)}
 button:active{transform:translateY(0)}
 button.danger{background:#7f1d1d;border-color:#b91c1c}
 button.danger:hover{background:#b91c1c}
 button.ghost{background:transparent}
 button.plan.active{border-color:var(--accent);background:#12233f;color:#bfdbfe}
 button:disabled{opacity:.5;cursor:not-allowed;transform:none}
 .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
 input{font:inherit;font-size:14px;padding:10px 12px;border-radius:10px;border:1px solid #2b3442;
  background:#10141b;color:var(--text);width:100%}
 input:focus{outline:2px solid #2563eb66;border-color:var(--accent)}
 .delay{flex:0 0 150px}
 .hint{color:var(--muted);font-size:12.5px}
 .sep{height:1px;background:var(--line);margin:16px 0}
 #log{font:12.5px/1.65 ui-monospace,"Cascadia Mono",Consolas,monospace;max-height:360px;overflow:auto}
 #log .entry{padding:6px 0;border-bottom:1px solid #1f2530;color:#cfd6e4;word-break:break-word}
 #log .entry:last-child{border-bottom:0}
 #log .time{color:var(--muted);margin-right:8px}
 #log .action{color:#93c5fd;font-weight:600}
 #log .err{color:#fca5a5}
</style>
</head>
<body>
<div class="wrap">
 <header>
  <h1>PC Agent</h1>
  <span class="badge"><span id="dot" class="dot"></span><span id="conn">Verbinde ...</span></span>
 </header>

 <div id="setup" class="card" style="display:none;max-width:560px;margin:0 auto 16px">
  <h2>Ersteinrichtung</h2>
  <p class="hint">Lege Benutzername und Passwort fuer den Zugriff vom Handy fest.
    Das Passwort wird nur als Hash gespeichert.</p>
  <div class="row"><input id="setupUser" placeholder="Benutzername" autocomplete="username"></div>
  <div class="row"><input id="setupPass" type="password" placeholder="Passwort (mind. 8 Zeichen)" autocomplete="new-password"></div>
  <div class="row"><input id="setupPass2" type="password" placeholder="Passwort wiederholen" autocomplete="new-password"></div>
  <div class="row"><button class="primary" onclick="doSetup()">Einrichten</button></div>
  <div id="setupMsg" class="hint"></div>
 </div>

 <div id="main">
 <div class="grid">
  <div class="card">
   <h2>Status</h2>
   <div id="state">Token wird geprueft ...</div>
   <div class="kv" id="kv"></div>

   <div class="sep"></div>
   <h2>Steuerung</h2>
   <div class="actions">
    <button class="danger" onclick="doShutdown()">Herunterfahren</button>
    <button class="ghost" onclick="act('/abort')">Abbrechen</button>
    <button class="ghost" onclick="act('/hibernate')">Ruhezustand</button>
    <button class="ghost" onclick="act('/ping')">Verbindung testen</button>
   </div>
   <div class="row">
    <input id="delay" class="delay" type="number" min="0" max="86400" value="60"
      title="Verzoegerung in Sekunden" onchange="saveDelay()">
    <span class="hint">Sekunden Verzoegerung beim Herunterfahren</span>
   </div>

   <div class="sep"></div>
   <h2>Energieplan</h2>
   <div class="actions">
    <button class="ghost plan" data-plan="high" onclick="act('/power-plan?name=high')">Hoechstleistung</button>
    <button class="ghost plan" data-plan="balanced" onclick="act('/power-plan?name=balanced')">Ausbalanciert</button>
    <button class="ghost plan" data-plan="saver" onclick="act('/power-plan?name=saver')">Energiesparen</button>
   </div>
  </div>

  <div class="card">
   <h2>Ereignisse</h2>
   <div id="log"></div>
   <div class="sep"></div>
   <div class="hint" id="authHint">Zugriff vom Handy: Benutzername + Passwort (Basic Auth).</div>
  </div>
 </div>
 </div>
</div>
<script>
document.getElementById('delay').value = localStorage.getItem('pcDelay') || '60';

function esc(s){
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function checkSetup(){
  const start = () => {
    if(window.__started) return;
    window.__started = true;
    refresh();
    setInterval(refresh, 2000);
  };
  try{
    const r = await fetch('/setup-status');
    const d = await r.json();
    document.getElementById('setup').style.display = d.required ? 'block' : 'none';
    document.getElementById('main').style.display = d.required ? 'none' : 'block';
    if(!d.required) start();
  }catch(e){
    document.getElementById('setup').style.display = 'none';
    document.getElementById('main').style.display = 'block';
    start();
  }
}
async function doSetup(){
  const user = document.getElementById('setupUser').value.trim();
  const pass = document.getElementById('setupPass').value;
  const pass2 = document.getElementById('setupPass2').value;
  const msg = document.getElementById('setupMsg');
  if(!user){ msg.textContent = 'Bitte Benutzername angeben.'; return; }
  if(pass.length < 8){ msg.textContent = 'Passwort muss mindestens 8 Zeichen haben.'; return; }
  if(pass !== pass2){ msg.textContent = 'Passwoerter stimmen nicht ueberein.'; return; }
  try{
    const r = await fetch('/setup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user: user, password: pass})
    });
    if(!r.ok){ msg.textContent = 'Fehler: ' + (await r.text()); return; }
    msg.textContent = 'Eingerichtet!';
    setTimeout(checkSetup, 500);
  }catch(e){
    msg.textContent = 'Nicht erreichbar: ' + e.message;
  }
}
function saveDelay(){
  localStorage.setItem('pcDelay', document.getElementById('delay').value);
}
async function call(path, method){
  return fetch(path, {method: method || 'GET'});
}
async function act(path){
  try{
    const r = await call(path, 'POST');
    if(!r.ok) alert('Fehler ' + r.status + ': ' + (await r.text()));
  }catch(e){
    alert('Nicht erreichbar: ' + e.message);
  }
  refresh();
}
function fmt(s){
  s = Math.max(0, Math.round(s));
  if(s < 60) return s + ' s';
  const m = Math.round(s / 60);
  if(m < 60) return m + ' min';
  return Math.floor(m / 60) + ' h ' + (m % 60) + ' min';
}
let busy = false;
async function refresh(){
  if(busy) return;
  busy = true;
  const dot = document.getElementById('dot');
  const conn = document.getElementById('conn');
  try{
    const s = await call('/status');
    if(!s.ok) throw new Error('HTTP ' + s.status);
    const d = await s.json();
    dot.className = 'dot ok';
    conn.textContent = d.host + ' - verbunden';
    let html = '<b>Online</b> - Modus: <b>' + (d.simulate ? 'SIMULATION (nichts wird ausgefuehrt)' : 'ECHT') + '</b>';
    if(d.pending_shutdown) html += '<br>Herunterfahren in <b>' + fmt(d.pending_shutdown) + '</b>';
    if(d.setup_required) html += '<br><span class="err">Handy-Zugriff noch nicht eingerichtet</span>';
    document.getElementById('state').innerHTML = html;
    const plans = {high:'Hoechstleistung', balanced:'Ausbalanciert', saver:'Energiesparen'};
    document.getElementById('kv').innerHTML =
      '<span>Energieplan</span><span><b>' + (plans[d.power_plan] || esc(d.power_plan)) + '</b></span>' +
      '<span>Host</span><span>' + esc(d.host) + '</span>';
    document.querySelectorAll('.plan').forEach(b => {
      b.classList.toggle('active', b.dataset.plan === d.power_plan);
    });
    const e = await (await call('/events')).json();
    document.getElementById('log').innerHTML = e.events.map(x =>
      '<div class="entry' + (x.ok ? '' : ' err') + '">' +
        '<span class="time">' + esc(x.time) + '</span>' +
        '<span class="action">' + esc(x.action) + '</span> - ' + esc(x.detail) +
        (x.ok ? '' : ' - FEHLER') + '</div>'
    ).join('') || '<div class="entry">noch keine Ereignisse</div>';
  }catch(err){
    dot.className = 'dot err';
    conn.textContent = 'Keine Verbindung';
    document.getElementById('state').innerHTML =
      '<span class="err">Keine Verbindung / Login fehlt: ' + esc(err.message) + '</span>';
  }finally{
    busy = false;
  }
}
async function doShutdown(){
  const delay = Math.max(0, parseInt(document.getElementById('delay').value || '0', 10));
  if(!confirm('PC in ' + delay + ' Sekunden herunterfahren?')) return;
  await act('/shutdown?delay=' + delay);
}
checkSetup();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    if _credentials is None and not TOKEN:
        print("Hinweis: Noch keine Zugangsdaten eingerichtet.")
        print("Beim ersten Start die Web-Oberflaeche oeffnen: http://localhost:8000/")
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
