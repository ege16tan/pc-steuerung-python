# install-autostart.ps1
# Registriert den PC-Agenten in der Windows-Aufgabenplanung.
#
# Ausfuehren (im Ordner "PC App"):
#   powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
#
# Es sind keine Admin-Rechte noetig: Die Aufgabe laeuft nur bei der Anmeldung
# des aktuellen Benutzers ("Nur interaktiv").

param(
    [string]$TaskName = 'PC Steuerung Agent',
    [switch]$WithTray
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$startBat  = Join-Path $projectRoot 'start-agent.bat'

if (-not (Test-Path -LiteralPath $startBat -PathType Leaf)) {
    throw "start.bat wurde nicht gefunden: $startBat"
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# Alte getrennte Python-/Rust-Aufgaben entfernen, damit beim Anmelden nicht
# zwei Agenten gleichzeitig um Port 8000 konkurrieren.
foreach ($legacyName in @('PC Agent 8000', 'PC Agent Rust 8000', 'PC Agent Tray', 'PC Agent Rust Tray')) {
    if ($legacyName -ne $TaskName -and (Get-ScheduledTask -TaskName $legacyName -ErrorAction SilentlyContinue)) {
        Unregister-ScheduledTask -TaskName $legacyName -Confirm:$false
    }
}

# Idempotent: eine vorhandene Aufgabe zuerst entfernen.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Vorhandene Aufgabe entfernt: $TaskName"
}

$action = New-ScheduledTaskAction `
    -Execute $startBat `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser

$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

# Kein Zeitlimit (Agent soll dauerhaft laufen), kein Neustart bei doppeltem Start.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Startet die bevorzugte PC-Agent-Version auf Port 8000' `
    -Force | Out-Null

Write-Host "Aufgabe registriert: $TaskName"
Write-Host "  Benutzer: $currentUser"
Write-Host "  Aktion:   $startBat"
Write-Host "  StartIn:  $scriptDir"

if ($WithTray) {
    $trayTaskName = 'PC Steuerung Tray'
    $startTray = Join-Path $projectRoot 'start-tray.bat'
    if (-not (Test-Path -LiteralPath $startTray -PathType Leaf)) {
        throw "start-tray.bat wurde nicht gefunden: $startTray"
    }
    $existingTray = Get-ScheduledTask -TaskName $trayTaskName -ErrorAction SilentlyContinue
    if ($existingTray) {
        Unregister-ScheduledTask -TaskName $trayTaskName -Confirm:$false
        Write-Host "Vorhandene Aufgabe entfernt: $trayTaskName"
    }
    $trayAction = New-ScheduledTaskAction -Execute $startTray -WorkingDirectory $projectRoot
    Register-ScheduledTask `
        -TaskName $trayTaskName `
        -Action $trayAction `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description 'Startet die PC-Agent-Tray-Steuerung (Taskleiste)' `
        -Force | Out-Null
    Write-Host "Aufgabe registriert: $trayTaskName"
    Write-Host "  Aktion:   $startTray"
}

Write-Host ""
Write-Host "Pruefen:  Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Start:    Start-ScheduledTask -TaskName '$TaskName'"
if ($WithTray) {
    Write-Host "Entfernen: .\uninstall-autostart.ps1 -WithTray"
} else {
    Write-Host "Entfernen: .\uninstall-autostart.ps1"
}
