# uninstall-autostart.ps1
# Entfernt die gemeinsame PC-Steuerung-Aufgabe aus der Windows-Aufgabenplanung.
#
# Ausfuehren (im Ordner "PC App"):
#   powershell -ExecutionPolicy Bypass -File .\uninstall-autostart.ps1
#   powershell -ExecutionPolicy Bypass -File .\uninstall-autostart.ps1 -WithTray
#
# Beendet keinen bereits laufenden Agenten - nur der Autostart wird entfernt.

param(
    [string]$TaskName = 'PC Steuerung Agent',
    [switch]$WithTray
)

$ErrorActionPreference = 'Stop'

$names = @($TaskName)
if ($WithTray) { $names += 'PC Steuerung Tray' }

$removed = 0
foreach ($name in $names) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Aufgabe entfernt: $name"
        $removed++
    } else {
        Write-Host "Aufgabe nicht vorhanden: $name"
    }
}

if ($removed -eq 0) {
    Write-Host "Nichts zu entfernen."
}
