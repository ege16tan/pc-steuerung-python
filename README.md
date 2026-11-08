# PC Agent

Kleiner FastAPI-Dienst, der einen Windows-PC im Heimnetz steuert. Gegenstück zur Android-App.

## Einrichten

```
python -m pip install -r requirements.txt
```

Beim ersten Start die Web-Oberfläche am PC öffnen (`http://localhost:8000/`)
und dort **Benutzername + Passwort** festlegen. Der Zugriff vom Handy läuft
danach über HTTP Basic Auth; das Passwort wird nur als Hash gespeichert
(`credentials.json`, wird nicht committet). Lokale Zugriffe (PC selbst, Tray,
Web-UI) brauchen keinen Login.

Der alte `PC_AGENT_TOKEN`-Schutz bleibt als Fallback erhalten, ist aber nicht
mehr nötig. `setx` wirkt erst in einem neuen Fenster.

## Starten

```
start.bat
```

Startet gezielt die Python-Version und speichert sie als bevorzugt. Für den
Standardstart mit Rust die Projektdatei `..\start-agent.bat` verwenden.
Beide Versionen können in der Web-UI oder im gemeinsamen Tray gewechselt werden.

## Tray-Steuerung (Taskleiste)

`start-tray.bat` startet das gemeinsame Symbol in der Windows-Taskleiste.
Doppelklick öffnet die Web-UI; das Kontextmenü bietet:

- **Agent starten** / **Agent stoppen** – startet bzw. beendet die bevorzugte Version
- **Version auswählen** – Rust (empfohlen) oder Python starten
- **Web-UI im Browser öffnen** – öffnet `http://localhost:8000/`
- **Herunterfahren** / **Abbrechen** – direkte PC-Steuerung im Tray
- **Status aktualisieren** – zeigt aktive und bevorzugte Version
- **Tray beenden** – schließt nur das Tray, nicht den Agenten

Das Symbol ist grün für Rust, blau für Python und grau, wenn der Agent gestoppt
ist. Das Tray aktualisiert den Status laufend und funktioniert mit beiden
Agenten.

## Im Browser prüfen

`http://localhost:8000/` — Token nur nötig, wenn `PC_AGENT_TOKEN` gesetzt ist,
dann `http://localhost:8000/?token=<dein-token>`.

Die Seite zeigt den Verbindungsstatus, die aktive und bevorzugte Agent-Version,
den Energieplan, ein laufendes Herunterfahren (mit Restzeit) und jedes
eingegangene Kommando mit Uhrzeit.
Die Verzögerung beim Herunterfahren ist einstellbar, der aktive Energieplan ist
markiert. Die Buttons dort lösen dieselben Endpunkte aus wie die Android-App —
damit lässt sich der Agent testen, ohne dass das Handy das erste Mal drückt.

## Endpunkte

| Methode | Pfad | Wirkung |
|---|---|---|
| GET | `/setup-status` | ob noch Zugangsdaten eingerichtet werden müssen |
| POST | `/setup` | Zugangsdaten anlegen (nur von localhost) |
| GET | `/agent-version` | aktive und bevorzugte Agent-Version |
| POST | `/agent-version?version=rust\|python` | Version speichern und Agent wechseln |
| POST | `/mode?simulate=true\|false` | Simulation/Echtzeit umschalten |
| GET | `/status` | online, Energieplan, laufendes Herunterfahren |
| POST | `/shutdown?delay=60` | Herunterfahren in `delay` Sekunden |
| POST | `/abort` | laufendes Herunterfahren abbrechen |
| POST | `/hibernate` | Ruhezustand |
| POST | `/power-plan?name=high\|balanced\|saver` | Energieplan wechseln |
| POST | `/wake?mac=AA:BB:CC:DD:EE:FF&port=9` | Magic Packet senden (Wake on LAN) |
| GET | `/events` | die letzten 50 Kommandos |
| GET | `/log?lines=200` | die letzten Zeilen der Logdatei (Klartext) |

Vom Handy (LAN) verlangen alle Endpunkte außer `/` und `/setup-status` eine
Anmeldung per HTTP Basic Auth (Benutzername/Passwort). Lokal am PC (localhost)
ist kein Login nötig. Ist noch kein Passwort eingerichtet, greift der alte
`PC_AGENT_TOKEN`-Schutz als Fallback.

## Wake on LAN

`POST /wake` sendet ein Magic Packet per UDP-Broadcast an die angegebene
MAC-Adresse. `mac` ist Pflicht (`AA:BB:CC:DD:EE:FF`), `port` ist optional
(Standard 9). Das Broadcast-Ziel wird über `PC_WOL_BROADCAST` gesetzt, Standard
ist `192.168.1.255`:

```
setx PC_WOL_BROADCAST "192.168.1.255"
```

Beispiel:

```
curl -X POST "http://localhost:8000/wake?mac=AA:BB:CC:DD:EE:FF&port=9" -H "X-Token: <dein-token>"
```

Der `X-Token`-Header ist nur nötig, wenn `PC_AGENT_TOKEN` gesetzt ist.

## Logdatei

Jede Aktion wird mit **Datum und Uhrzeit** in `agent.log` geschrieben. Die Datei
wird bei ca. 1 MB nach `agent.log.1` rotiert und ist gitignored.

Letzte Zeilen abrufen:

```
curl "http://localhost:8000/log?lines=200"
```

Dieselben Einträge zeigt die Web-Oberfläche unter „Ereignisse" (Live) und
„Verlauf (Logdatei)". Die Android-App hat dafür den Button „Verlauf anzeigen".

## Simulationsmodus & Debug-Umschalter

`PC_AGENT_SIMULATE=1` (Standard) führt **nichts** aus, sondern schreibt nur ins
Ereignis-Log. Erst mit `PC_AGENT_SIMULATE=0` wird wirklich heruntergefahren.
Auch `/wake` sendet im Simulationsmodus kein Packet, sondern loggt nur.

Zum Testen lässt sich der Modus zur Laufzeit umschalten:

- **Web-Oberfläche:** Debug-Modus → „Simulation" / „Echt"
- **Android-App:** Einstellungen → Echtzeitmodus
- **API:** `curl -X POST "http://localhost:8000/mode?simulate=false"`

**Achtung:** Im Echtmodus werden Befehle wirklich ausgeführt (Herunterfahren,
Ruhezustand, Energieplan). Der Modus gilt bis zum Neustart; der Startwert kommt
weiterhin aus `PC_AGENT_SIMULATE`.

## Firewall (einmalig, als Administrator)

```
netsh advfirewall firewall add rule name="PC Agent 8000" dir=in action=allow protocol=TCP localport=8000 remoteip=localsubnet profile=private
```

## Autostart

Die Skripte in diesem Ordner richten die gemeinsame Aufgabe **"PC Steuerung Agent"**
in der Windows-Aufgabenplanung ein. Sie läuft bei der Anmeldung des aktuellen
Benutzers und startet die bevorzugte Version über `start-agent.bat` im Projektstamm.
Admin-Rechte sind nicht nötig, weil die Aufgabe nur interaktiv läuft ("Nur interaktiv").

Einrichten (im Ordner `PC App`):

```
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
```

Das Skript ist idempotent: Eine vorhandene Aufgabe wird zuerst entfernt und
dann neu registriert. Es startet `start-agent.bat` aus dem Projektstamm.
Die Aufgabe hat kein Zeitlimit und wird nicht bei Batteriebetrieb beendet.

Soll zusätzlich die Tray-Steuerung automatisch starten:

```
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1 -WithTray
```

Dann wird eine zweite Aufgabe **"PC Steuerung Tray"** registriert. Entfernen
entsprechend mit `.\uninstall-autostart.ps1 -WithTray`.

Prüfen — oder sofort starten, ohne Ab- und Anmelden:

```
Get-ScheduledTask -TaskName "PC Steuerung Agent"
Start-ScheduledTask -TaskName "PC Steuerung Agent"
```

Entfernen:

```
powershell -ExecutionPolicy Bypass -File .\uninstall-autostart.ps1
```

Das Entfernen stoppt keinen bereits laufenden Agenten, sondern löscht nur den
Autostart-Eintrag.

Hinweis: `PC_AGENT_TOKEN` und `PC_AGENT_SIMULATE` liest `start.bat` aus den
Umgebungsvariablen des angemeldeten Benutzers. Nach `setx` daher einmal neu
anmelden (oder die Aufgabe neu starten).

## Was noch fehlt

- Herunterfahren/Energieplan echt testen: `PC_AGENT_SIMULATE=0` setzen und die
  Endpunkte ausprobieren — bisher nur im Simulationsmodus geprüft.
- Timer: Es gibt nur `delay` pro `/shutdown`-Aufruf, noch keinen wiederkehrenden
  Zeitplan (z. B. täglich 23:00).
- Autostart einmal nach einer echten Neuanmeldung prüfen (mit eingerichteten Zugangsdaten).
