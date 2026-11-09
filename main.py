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
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel
import qrcode
import qrcode.image.svg

PLANS = {
    "high": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
}
CREATE_NO_WINDOW = 0x08000000

try:
    SERVER_PORT = int(os.environ.get("PC_AGENT_PORT", "8000"))
    if not 1 <= SERVER_PORT <= 65535:
        SERVER_PORT = 8000
except ValueError:
    SERVER_PORT = 8000

TOKEN = os.environ.get("PC_AGENT_TOKEN", "")
SIMULATE = os.environ.get("PC_AGENT_SIMULATE", "1") != "0"

app = FastAPI(title="PC Agent", version="0.1")
_lock = threading.Lock()
_events = deque(maxlen=50)
_pending = {"at": None, "seconds": 0}
_sim_plan: str | None = None

CREDENTIALS_FILE = Path(__file__).with_name("credentials.json")
TOKEN_FILE = Path(__file__).with_name("token.json")
DEVICE_FILE = Path(__file__).with_name("device.json")
LOG_FILE = Path(__file__).with_name("agent.log")
AGENT_VERSION_FILE = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    / "PC Steuerung"
    / "agent-version.txt"
)


def _load_credentials() -> dict | None:
    try:
        data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("user") and data.get("salt") and data.get("hash"):
        return data
    return None


_credentials = _load_credentials()


def _load_token_credentials() -> dict | None:
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("salt") and data.get("hash") else None


def _load_device_settings() -> dict:
    try:
        data = json.loads(DEVICE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {
        "name": str(data.get("name", "")),
        "wol_enabled": bool(data.get("wol_enabled", False)),
        "mac": str(data.get("mac", "")),
    }


_token_credentials = _load_token_credentials()
_device_settings = _load_device_settings()


def _append_log(line: str) -> None:
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _log(action: str, detail: str, ok: bool = True) -> None:
    now = datetime.now()
    entry = {
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "detail": detail,
        "ok": ok,
    }
    line = f"{entry['time']}  {action}  {detail}  {'ok' if ok else 'FEHLER'}"
    with _lock:
        _events.appendleft(entry)
        _append_log(line)
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
    try:
        mac_bytes = bytes(int(p, 16) for p in parts)
    except ValueError:
        return False
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


def _current_plan() -> str:
    if SIMULATE and _sim_plan:
        return _sim_plan
    return _active_plan()


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _lan_ipv4() -> str | None:
    """Ermittelt die IPv4-Adresse der Standardroute ohne ein Paket zu senden."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
        return address if address and not address.startswith("127.") else None
    except OSError:
        return None


def _pairing_payload() -> dict | None:
    host = _lan_ipv4()
    if not host:
        return None
    name = _device_settings["name"].strip() or os.environ.get("COMPUTERNAME", "PC")
    mac = _device_settings["mac"] if _device_settings["wol_enabled"] else ""
    payload_data = {"app": "pc-steuerung", "version": 1, "host": host, "port": SERVER_PORT, "name": name}
    if mac:
        payload_data["mac"] = mac
    payload = json.dumps(
        payload_data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "ready": True, "host": host, "port": SERVER_PORT, "name": name,
        "wol_enabled": bool(mac), "mac": mac or None, "payload": payload,
    }


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
    if _token_credentials:
        try:
            salt = bytes.fromhex(_token_credentials["salt"])
            expected = bytes.fromhex(_token_credentials["hash"])
        except (KeyError, ValueError):
            raise HTTPException(status_code=403, detail="Token-Zugangsdaten sind ungueltig")
        actual = hashlib.pbkdf2_hmac("sha256", x_token.encode("utf-8"), salt, 200_000)
        if not hmac.compare_digest(actual, expected):
            raise HTTPException(status_code=403, detail="Token fehlt oder falsch")
        return
    # Fallback: alter Token-Schutz.
    if not TOKEN:
        return
    if not secrets.compare_digest(x_token, TOKEN):
        raise HTTPException(status_code=403, detail="Token fehlt oder falsch")


class SetupRequest(BaseModel):
    auth_mode: str = "basic"
    user: str = ""
    password: str = ""


class DeviceSetupRequest(BaseModel):
    name: str
    wol_enabled: bool = False
    mac: str = ""


@app.get("/setup-status")
def setup_status() -> dict:
    return {"required": _credentials is None and _token_credentials is None and not TOKEN}


@app.get("/network-adapters")
def network_adapters(request: Request) -> list[dict]:
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Nur lokal verfuegbar")
    script = r"$items = @(Get-NetIPConfiguration -ErrorAction SilentlyContinue | ForEach-Object { $c = $_; $a = $c.NetAdapter; $ips = @($c.IPv4Address | ForEach-Object { $_.IPAddress } | Where-Object { $_ -and $_ -notlike '127.*' }); if ($a -and $a.Status -eq 'Up' -and $ips.Count -gt 0 -and $a.MacAddress) { [pscustomobject]@{ name = $c.InterfaceAlias; ip = $ips[0]; mac = $a.MacAddress; is_default = [bool]$c.IPv4DefaultGateway } } }); ConvertTo-Json -InputObject $items -Compress"
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        data = json.loads(result.stdout)
        return data if isinstance(data, list) else [data]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


@app.post("/device-setup")
def device_setup(request: Request, body: DeviceSetupRequest) -> dict:
    global _device_settings
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Einrichtung nur lokal moeglich")
    name = body.name.strip()
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="PC-Name muss 1 bis 64 Zeichen haben")
    mac = body.mac.strip().lower().replace("-", ":") if body.wol_enabled else ""
    if body.wol_enabled:
        parts = mac.split(":")
        if len(parts) != 6 or any(len(part) != 2 or not all(c in "0123456789abcdef" for c in part) for part in parts):
            raise HTTPException(status_code=400, detail="MAC-Adresse ungueltig")
        mac = mac.upper()
    settings = {"name": name, "wol_enabled": bool(mac), "mac": mac}
    try:
        DEVICE_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Setup konnte nicht gespeichert werden: {e}")
    _device_settings = settings
    return {"ok": True, **settings}


@app.get("/pairing-info")
def pairing_info() -> dict:
    info = _pairing_payload()
    return info or {"ready": False, "message": "Keine LAN-IPv4-Adresse erkannt."}


@app.get("/pairing-qr.svg", response_class=Response)
def pairing_qr() -> Response:
    info = _pairing_payload()
    if not info:
        return Response(
            content='{"detail":"Keine LAN-IPv4-Adresse erkannt"}',
            status_code=503,
            media_type="application/json",
        )
    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    code.add_data(info["payload"])
    code.make(fit=True)
    image = code.make_image(image_factory=qrcode.image.svg.SvgPathFillImage)
    return Response(
        content=image.to_string(encoding="unicode"),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/setup")
def setup(request: Request, body: SetupRequest) -> dict:
    global _credentials, _token_credentials
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Einrichtung nur am PC (localhost) moeglich")
    if _credentials is not None or _token_credentials is not None or TOKEN:
        raise HTTPException(status_code=409, detail="Zugangsdaten sind bereits eingerichtet")
    if body.auth_mode == "token":
        token = secrets.token_hex(32)
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt, 200_000)
        data = {"salt": salt.hex(), "hash": digest.hex()}
        try:
            TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Token konnte nicht gespeichert werden: {e}")
        _token_credentials = data
        _log("setup", "Token-Zugang eingerichtet")
        return {"ok": True, "auth_mode": "token", "token": token}
    if body.auth_mode != "basic":
        raise HTTPException(status_code=400, detail="Anmeldeart muss basic oder token sein")
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


def _preferred_agent_version() -> str:
    try:
        version = AGENT_VERSION_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "rust"
    return version if version in ("python", "rust") else "rust"


@app.get("/agent-version")
def agent_version(_: None = Depends(require_token)) -> dict:
    return {"active": "python", "selected": _preferred_agent_version()}


@app.post("/agent-version")
def switch_agent_version(
    version: str = Query(..., pattern="^(python|rust)$"),
    _: None = Depends(require_token),
) -> dict:
    manager = Path(__file__).resolve().parent.parent / "agent-manager.ps1"
    if not manager.is_file():
        raise HTTPException(status_code=500, detail="Agent-Verwaltung wurde nicht gefunden")
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(manager),
                "-Action",
                "switch",
                "-Version",
                version,
                "-Port",
                "8000",
            ],
            cwd=manager.parent,
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Versionswechsel fehlgeschlagen: {e}")
    return {"ok": True, "selected": version, "switching": True}


@app.post("/mode")
def set_mode(
    simulate: bool = Query(...),
    _: None = Depends(require_token),
) -> dict:
    global SIMULATE, _sim_plan
    SIMULATE = simulate
    _sim_plan = None
    with _lock:
        _pending.update(at=None, seconds=0)
    _log("mode", "SIMULATION" if simulate else "ECHT")
    return {"ok": True, "simulate": SIMULATE}


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
        "power_plan": _current_plan(),
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
    global _sim_plan
    code, out = _run(["powercfg", "/setactive", PLANS[name]])
    _log("power-plan", f"{name} {out}".strip(), ok=code == 0)
    if code != 0:
        raise HTTPException(status_code=500, detail=out or "powercfg fehlgeschlagen")
    if SIMULATE:
        _sim_plan = name
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


@app.get("/log", response_class=PlainTextResponse)
def log(
    lines: int = Query(200, ge=1, le=1000),
    _: None = Depends(require_token),
) -> str:
    try:
        if not LOG_FILE.exists():
            return ""
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            content = f.readlines()
        return "".join(content[-lines:])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Log konnte nicht gelesen werden: {e}")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


PAGE = """
<!doctype html>
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
 select{font:inherit;font-size:14px;padding:10px 12px;border-radius:10px;border:1px solid #2b3442;
  background:#10141b;color:var(--text);min-width:190px}
 .delay{flex:0 0 150px}
 .hint{color:var(--muted);font-size:12.5px}
 .sep{height:1px;background:var(--line);margin:16px 0}
 #log{font:12.5px/1.65 ui-monospace,"Cascadia Mono",Consolas,monospace;max-height:360px;overflow:auto}
 #log .entry{padding:6px 0;border-bottom:1px solid #1f2530;color:#cfd6e4;word-break:break-word}
 #log .entry:last-child{border-bottom:0}
 #log .time{color:var(--muted);margin-right:8px}
 #log .action{color:#93c5fd;font-weight:600}
 #log .err{color:#fca5a5}
 .filelog{font:12px/1.6 ui-monospace,"Cascadia Mono",Consolas,monospace;max-height:300px;overflow:auto;white-space:pre-wrap;color:#cfd6e4}
 .pairing{display:flex;align-items:center;justify-content:space-between;gap:18px;max-width:700px;margin:0 auto 16px}
 .pairing h2{margin-bottom:8px}
 .pairing img{display:block;width:184px;height:184px;flex:0 0 184px;padding:8px;border-radius:12px;background:#fff}
 .pairing-address{display:inline-block;margin-top:8px;padding:6px 9px;border:1px solid var(--line);border-radius:8px;color:#cbd5e1;word-break:break-all}
 @media(max-width:520px){.pairing{align-items:flex-start;flex-direction:column}.pairing img{align-self:center}}
 button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
 button.primary:hover{background:#2563eb}
 .steps{display:flex;gap:6px;margin-bottom:14px}
 .steps span{flex:1;height:4px;border-radius:2px;background:var(--line)}
 .steps span.on{background:var(--accent)}
 .switch{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0}
 .switch input{width:auto}
 .tokenbox{font:12.5px/1.5 ui-monospace,"Cascadia Mono",Consolas,monospace;background:#10141b;border:1px solid var(--line);border-radius:10px;padding:10px;word-break:break-all;margin-top:8px;color:#bfdbfe}
</style>
</head>
<body>
<div class="wrap">
 <header>
  <h1>PC Agent</h1>
  <span class="badge"><span id="dot" class="dot"></span><span id="conn">Verbinde ...</span></span>
 </header>

 <div id="pairingCard" class="card pairing" style="display:none">
  <div>
   <h2>Mit dem Handy verbinden</h2>
   <p class="hint">In der Android-App „QR scannen“ wählen. Der Code enthält nur PC-Name, IP und Port, keine Zugangsdaten.</p>
   <div id="pairingAddress" class="pairing-address">Netzwerkadresse wird gesucht …</div>
  </div>
  <img id="pairingQr" alt="QR-Code für die PC-Steuerung">
 </div>

 <div id="setup" class="card" style="display:none;max-width:560px;margin:0 auto 16px">
  <h2 id="setupTitle">Ersteinrichtung</h2>
  <div class="steps"><span id="step1" class="on"></span><span id="step2"></span><span id="step3"></span><span id="step4"></span></div>

  <div id="wiz1" class="wiz">
   <p class="hint">Wie soll dieser PC in der Android-App heißen?</p>
   <div class="row"><input id="wizName" placeholder="z. B. Wohnzimmer-PC" maxlength="64" autocomplete="off"></div>
  </div>

  <div id="wiz2" class="wiz" style="display:none">
   <div class="switch">
    <span>Wake-on-LAN einschalten</span>
    <input id="wizWol" type="checkbox" onchange="toggleWol()">
   </div>
   <p class="hint">Ermöglicht das Einschalten per Handy. Zuverlässig nur mit Ethernet-Kabel.</p>
   <div id="wizWolFields" style="display:none">
    <div class="row"><input id="wizMac" placeholder="MAC-Adresse (AA:BB:CC:DD:EE:FF)"></div>
    <div class="row">
     <button class="ghost" onclick="findAdapters()">Netzwerkadapter suchen</button>
     <select id="wizAdapter" style="display:none" onchange="pickAdapter()"></select>
    </div>
   </div>
  </div>

  <div id="wiz3" class="wiz" style="display:none">
   <p class="hint">Wie soll sich das Handy am PC anmelden?</p>
   <div class="row">
    <select id="wizAuth" onchange="toggleAuth()">
     <option value="basic">Benutzername + Passwort (empfohlen)</option>
     <option value="token">Token (nicht empfohlen)</option>
    </select>
   </div>
   <div id="wizBasicFields">
    <div class="row"><input id="wizUser" placeholder="Benutzername" autocomplete="username"></div>
    <div class="row"><input id="wizPass" type="password" placeholder="Passwort (mind. 8 Zeichen)" autocomplete="new-password"></div>
    <div class="row"><input id="wizPass2" type="password" placeholder="Passwort wiederholen" autocomplete="new-password"></div>
   </div>
  </div>

  <div id="wiz4" class="wiz" style="display:none">
   <p class="hint">Fertig vorbereitet. Beim Einrichten werden gespeichert:</p>
   <div id="wizSummary" class="hint" style="line-height:1.8"></div>
   <p class="hint" style="margin-top:8px">Danach oben den QR-Code mit dem Handy scannen.</p>
  </div>

  <div class="row" style="justify-content:space-between">
   <button id="wizBack" class="ghost" onclick="wizBack()" style="display:none">Zurück</button>
   <button id="wizNext" class="primary" onclick="wizNext()">Weiter</button>
  </div>
  <div id="setupMsg" class="hint"></div>
 </div>

 <div id="main">
 <div class="card" style="margin:0 0 16px">
  <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
   <div><h2 style="margin-bottom:4px">Agent-Version</h2>
    <div class="hint">Aktiv: <strong id="activeAgent">wird geprüft …</strong></div></div>
   <div class="row" style="margin-top:0">
    <select id="agentVersion" aria-label="Bevorzugte Agent-Version">
     <option value="rust">Rust (empfohlen)</option><option value="python">Python</option>
    </select>
    <button id="switchVersion" onclick="switchAgentVersion()">Version verwenden</button>
   </div>
  </div>
  <div class="hint" id="agentVersionMessage" style="margin-top:10px">Rust ist die bevorzugte Version. Python bleibt als Alternative verfügbar.</div>
 </div>
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
   <div class="row">
    <span class="hint">Debug-Modus:</span>
    <button class="ghost" onclick="setMode(true)">Simulation</button>
    <button class="ghost" onclick="setMode(false)">Echt</button>
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

 <div class="card" style="margin-top:16px">
  <h2>Verlauf (Logdatei)</h2>
  <div id="filelog" class="filelog">Wird geladen ...</div>
 </div>
 </div>
</div>
<script>
document.getElementById('delay').value = localStorage.getItem('pcDelay') || '60';

function esc(s){
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function checkSetup(){
  const pairingCard = document.getElementById('pairingCard');
  pairingCard.style.display = 'flex';
  loadPairingInfo();
  const start = () => {
    if(window.__started) return;
    window.__started = true;
    refresh();
    loadFileLog();
    loadAgentVersion();
    setInterval(refresh, 2000);
    setInterval(loadFileLog, 10000);
    setInterval(loadAgentVersion, 5000);
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
async function loadPairingInfo(){
  const label = document.getElementById('pairingAddress');
  const image = document.getElementById('pairingQr');
  try{
    const r = await fetch('/pairing-info', {cache:'no-store'});
    const data = await r.json();
    if(!r.ok || !data.ready){
      label.textContent = data.message || 'Keine LAN-Adresse erkannt. PC und Handy müssen im selben Netzwerk sein.';
      image.style.display = 'none';
      return;
    }
    label.textContent = data.name + ' · http://' + data.host + ':' + data.port;
    image.style.display = 'block';
    image.src = '/pairing-qr.svg?ts=' + Date.now();
  }catch(e){
    label.textContent = 'QR-Code konnte nicht geladen werden.';
    image.style.display = 'none';
  }
}
let wizStep = 1;
function showWiz(step){
  wizStep = step;
  for(let i=1;i<=4;i++){
    document.getElementById('wiz' + i).style.display = i === step ? 'block' : 'none';
    const bar = document.getElementById('step' + i);
    if(bar) bar.className = i <= step ? 'on' : '';
  }
  document.getElementById('wizBack').style.display = step > 1 ? 'inline-block' : 'none';
  document.getElementById('wizNext').textContent = step === 4 ? 'Einrichten' : 'Weiter';
  document.getElementById('setupTitle').textContent = 'Ersteinrichtung · Schritt ' + step + ' von 4';
  if(step === 4) buildSummary();
  document.getElementById('setupMsg').textContent = '';
}
function buildSummary(){
  const name = document.getElementById('wizName').value.trim();
  const wol = document.getElementById('wizWol').checked;
  const mac = document.getElementById('wizMac').value.trim();
  const auth = document.getElementById('wizAuth').value;
  const user = document.getElementById('wizUser').value.trim();
  document.getElementById('wizSummary').innerHTML =
    '<b>PC-Name:</b> ' + esc(name || '—') +
    '<br><b>Wake-on-LAN:</b> ' + (wol ? 'an · ' + esc(mac) : 'aus') +
    '<br><b>Anmeldung:</b> ' + (auth === 'token' ? 'Token' : 'Benutzer „' + esc(user || '—') + '“');
}
function wizNext(){
  const msg = document.getElementById('setupMsg');
  if(wizStep === 1){
    if(!document.getElementById('wizName').value.trim()){ msg.textContent = 'Bitte PC-Name angeben.'; return; }
    showWiz(2);
  } else if(wizStep === 2){
    const wol = document.getElementById('wizWol').checked;
    if(wol){
      const mac = document.getElementById('wizMac').value.trim();
      if(!/^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$/.test(mac)){ msg.textContent = 'MAC-Adresse ungueltig (Format AA:BB:CC:DD:EE:FF).'; return; }
    }
    showWiz(3);
  } else if(wizStep === 3){
    const auth = document.getElementById('wizAuth').value;
    if(auth === 'basic'){
      const user = document.getElementById('wizUser').value.trim();
      const pass = document.getElementById('wizPass').value;
      const pass2 = document.getElementById('wizPass2').value;
      if(!user){ msg.textContent = 'Bitte Benutzername angeben.'; return; }
      if(pass.length < 8){ msg.textContent = 'Passwort muss mindestens 8 Zeichen haben.'; return; }
      if(pass !== pass2){ msg.textContent = 'Passwoerter stimmen nicht ueberein.'; return; }
    }
    showWiz(4);
  } else {
    finishSetup();
  }
}
function wizBack(){
  if(wizStep > 1) showWiz(wizStep - 1);
}
function toggleWol(){
  document.getElementById('wizWolFields').style.display =
    document.getElementById('wizWol').checked ? 'block' : 'none';
}
function toggleAuth(){
  document.getElementById('wizBasicFields').style.display =
    document.getElementById('wizAuth').value === 'basic' ? 'block' : 'none';
}
async function findAdapters(){
  const sel = document.getElementById('wizAdapter');
  const msg = document.getElementById('setupMsg');
  msg.textContent = 'Adapter werden gesucht …';
  try{
    const r = await fetch('/network-adapters');
    if(!r.ok){ msg.textContent = 'Adapter-Suche nur lokal moeglich (HTTP ' + r.status + ').'; return; }
    const list = await r.json();
    if(!Array.isArray(list) || list.length === 0){ msg.textContent = 'Keine aktiven Netzwerkadapter gefunden.'; return; }
    sel.innerHTML = '<option value="">Adapter wählen …</option>' + list.map(a =>
      '<option value="' + esc(a.mac) + '">' + esc(a.name) + ' · ' + esc(a.ip) + (a.is_default ? ' (Standard)' : '') + '</option>'
    ).join('');
    sel.style.display = 'inline-block';
    msg.textContent = list.length + ' Adapter gefunden.';
  }catch(e){
    msg.textContent = 'Adapter-Suche fehlgeschlagen: ' + e.message;
  }
}
function pickAdapter(){
  const mac = document.getElementById('wizAdapter').value;
  if(mac) document.getElementById('wizMac').value = mac;
}
async function finishSetup(){
  const msg = document.getElementById('setupMsg');
  const button = document.getElementById('wizNext');
  const name = document.getElementById('wizName').value.trim();
  const wol = document.getElementById('wizWol').checked;
  const mac = wol ? document.getElementById('wizMac').value.trim() : '';
  button.disabled = true;
  try{
    const dr = await fetch('/device-setup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, wol_enabled: wol, mac: mac})
    });
    if(!dr.ok){ msg.textContent = 'Fehler (PC-Name/WoL): ' + (await dr.text()); button.disabled = false; return; }
  }catch(e){
    msg.textContent = 'Nicht erreichbar: ' + e.message; button.disabled = false; return;
  }
  const auth = document.getElementById('wizAuth').value;
  const body = auth === 'token'
    ? {auth_mode: 'token'}
    : {auth_mode: 'basic', user: document.getElementById('wizUser').value.trim(), password: document.getElementById('wizPass').value};
  try{
    const sr = await fetch('/setup', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await sr.json().catch(() => ({}));
    if(!sr.ok){ msg.textContent = 'Fehler (Anmeldung): ' + (data.detail || (await sr.text())); button.disabled = false; return; }
    if(auth === 'token'){
      msg.innerHTML = 'Eingerichtet! Dein Token:<br><div class="tokenbox">' + esc(data.token) + '</div>';
    } else {
      msg.textContent = 'Eingerichtet! Scanne jetzt den QR-Code oben mit dem Handy.';
    }
    loadPairingInfo();
    setTimeout(checkSetup, 800);
  }catch(e){
    msg.textContent = 'Nicht erreichbar: ' + e.message; button.disabled = false;
  }
}
function saveDelay(){
  localStorage.setItem('pcDelay', document.getElementById('delay').value);
}
async function call(path, method){
  return fetch(path, {method: method || 'GET'});
}
async function loadAgentVersion(){
  try{
    const r = await call('/agent-version');
    if(!r.ok) return;
    const d = await r.json();
    const names = {rust:'Rust', python:'Python'};
    document.getElementById('activeAgent').textContent = names[d.active] || d.active || 'gestoppt';
    const choice = document.getElementById('agentVersion');
    if(document.activeElement !== choice) choice.value = d.selected || 'rust';
    document.getElementById('switchVersion').disabled = d.active === choice.value && d.selected === choice.value;
  }catch(e){}
}
async function switchAgentVersion(){
  const version = document.getElementById('agentVersion').value;
  const message = document.getElementById('agentVersionMessage');
  const button = document.getElementById('switchVersion');
  button.disabled = true;
  message.textContent = 'Wechsel läuft. Der Agent wird neu gestartet …';
  try{
    const r = await call('/agent-version?version=' + encodeURIComponent(version), 'POST');
    const data = await r.json().catch(() => ({}));
    if(!r.ok){ message.textContent = data.detail || 'Versionswechsel fehlgeschlagen.'; button.disabled = false; return; }
    message.textContent = 'Auswahl gespeichert. Warte auf ' + (version === 'rust' ? 'Rust' : 'Python') + ' …';
    for(let attempt=0; attempt<150; attempt++){
      await new Promise(resolve => setTimeout(resolve, 2000));
      try{
        const status = await call('/agent-version');
        if(status.ok && (await status.json()).active === version){ location.reload(); return; }
      }catch(e){}
    }
    message.textContent = 'Der Wechsel dauert noch. Aktualisiere die Seite gleich erneut.';
    button.disabled = false;
  }catch(e){ message.textContent = 'Verbindung zum Agenten wurde unterbrochen. Die Seite prüft weiter …'; }
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
async function loadFileLog(){
  try{
    const r = await call('/log?lines=200');
    if(!r.ok) return;
    const text = await r.text();
    document.getElementById('filelog').textContent = text.trim() || 'noch keine Einträge';
  }catch(e){}
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
async function setMode(simulate){
  if(!simulate && !confirm('Echtzeitmodus aktivieren? Befehle werden wirklich ausgefuehrt!')) return;
  try{
    const r = await call('/mode?simulate=' + simulate, 'POST');
    if(!r.ok) alert('Fehler ' + r.status + ': ' + (await r.text()));
  }catch(e){
    alert('Nicht erreichbar: ' + e.message);
  }
  refresh();
}
showWiz(1);
toggleAuth();
toggleWol();
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

    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
