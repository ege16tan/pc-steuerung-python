# install-autostart.ps1
# Registriert den PC-Agenten in der Windows-Aufgabenplanung.
#
# Ausfuehren (im Ordner "PC App"):
#   powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
#
# Es sind keine Admin-Rechte noetig: Die Aufgabe laeuft nur bei der Anmeldung
# des aktuellen Benutzers ("Nur interaktiv").

param(
    [string]$TaskName = 'PC Agent 8000',
    [switch]$WithTray
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startBat  = Join-Path $scriptDir 'start.bat'

if (-not (Test-Path -LiteralPath $startBat -PathType Leaf)) {
    throw "start.bat wurde nicht gefunden: $startBat"
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# Idempotent: eine vorhandene Aufgabe zuerst entfernen.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Vorhandene Aufgabe entfernt: $TaskName"
}

$action = New-ScheduledTaskAction `
    -Execute $startBat `
    -WorkingDirectory $scriptDir

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
    -Description 'Startet den PC-Agenten (FastAPI) auf Port 8000' `
    -Force | Out-Null

Write-Host "Aufgabe registriert: $TaskName"
Write-Host "  Benutzer: $currentUser"
Write-Host "  Aktion:   $startBat"
Write-Host "  StartIn:  $scriptDir"

if ($WithTray) {
    $trayTaskName = 'PC Agent Tray'
    $startTray = Join-Path $scriptDir 'start-tray.bat'
    if (-not (Test-Path -LiteralPath $startTray -PathType Leaf)) {
        throw "start-tray.bat wurde nicht gefunden: $startTray"
    }
    $existingTray = Get-ScheduledTask -TaskName $trayTaskName -ErrorAction SilentlyContinue
    if ($existingTray) {
        Unregister-ScheduledTask -TaskName $trayTaskName -Confirm:$false
        Write-Host "Vorhandene Aufgabe entfernt: $trayTaskName"
    }
    $trayAction = New-ScheduledTaskAction -Execute $startTray -WorkingDirectory $scriptDir
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
