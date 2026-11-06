# tray.ps1
# System-Tray-Steuerung fuer den PC-Agenten (Start/Stop/Status).
#
# Starten: start-tray.bat (oder: powershell -STA -ExecutionPolicy Bypass -File .\tray.ps1)
#
# Das Tray steuert den Agenten ueber Port 8000. Es funktioniert auch, wenn der
# Agent per Autostart (start.bat) gestartet wurde. "Tray beenden" stoppt den
# Agenten NICHT - dafuer gibt es den Menuepunkt "Agent stoppen".

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 8000

function Test-Agent {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $client.BeginConnect('127.0.0.1', $port, $null, $null)
        $ok = $ar.AsyncWaitHandle.WaitOne(500)
        if ($ok) { $client.EndConnect($ar) }
        return $ok
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-AgentPid {
    try {
        $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop | Select-Object -First 1
        return [int]$conn.OwningProcess
    } catch {
        return $null
    }
}

function Start-Agent {
    if (Test-Agent) { return 'laeuft bereits' }
    try {
        Start-Process -FilePath 'python' `
            -ArgumentList @('-m','uvicorn','main:app','--host','0.0.0.0','--port',"$port") `
            -WorkingDirectory $scriptDir -WindowStyle Hidden | Out-Null
        Start-Sleep -Milliseconds 900
        if (Test-Agent) { return 'gestartet' } else { return 'Start dauert noch' }
    } catch {
        return "Fehler: $($_.Exception.Message)"
    }
}

function Stop-Agent {
    $procId = Get-AgentPid
    if (-not $procId) { return 'war bereits gestoppt' }
    try {
        Stop-Process -Id $procId -Force -ErrorAction Stop
        return 'gestoppt'
    } catch {
        return "Fehler: $($_.Exception.Message)"
    }
}

# Icon erzeugen: farbiger Punkt (gruen = laeuft, grau = gestoppt).
function New-DotIcon([System.Drawing.Color]$color, [ref]$bitmapRef) {
    $bmp = New-Object System.Drawing.Bitmap 16, 16
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)
    $brush = New-Object System.Drawing.SolidBrush $color
    $g.FillEllipse($brush, 1, 1, 14, 14)
    $g.Dispose()
    $brush.Dispose()
    $bitmapRef.Value = $bmp
    return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}

$bmpRun = $null
$bmpStop = $null
$iconRun = New-DotIcon ([System.Drawing.Color]::FromArgb(46, 204, 113)) ([ref]$bmpRun)
$iconStop = New-DotIcon ([System.Drawing.Color]::FromArgb(158, 158, 158)) ([ref]$bmpStop)

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Text = 'PC Agent'
$notify.Icon = $iconStop
$notify.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip

$itemStatus = New-Object System.Windows.Forms.ToolStripMenuItem
$itemStatus.Text = 'Status: ...'
$itemStatus.Enabled = $false
[void]$menu.Items.Add($itemStatus)
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

$itemStart = New-Object System.Windows.Forms.ToolStripMenuItem
$itemStart.Text = 'Agent starten'
$itemStart.Add_Click({
    $result = Start-Agent
    $notify.ShowBalloonTip(3000, 'PC Agent', "Agent $result", [System.Windows.Forms.ToolTipIcon]::Info)
    Update-Ui
})

$itemStop = New-Object System.Windows.Forms.ToolStripMenuItem
$itemStop.Text = 'Agent stoppen'
$itemStop.Add_Click({
    $result = Stop-Agent
    $notify.ShowBalloonTip(3000, 'PC Agent', "Agent $result", [System.Windows.Forms.ToolTipIcon]::Info)
    Update-Ui
})

$itemCheck = New-Object System.Windows.Forms.ToolStripMenuItem
$itemCheck.Text = 'Status pruefen'
$itemCheck.Add_Click({
    try {
        $headers = @{}
        if ($env:PC_AGENT_TOKEN) { $headers['X-Token'] = $env:PC_AGENT_TOKEN }
        $s = Invoke-RestMethod -Uri "http://127.0.0.1:$port/status" -Headers $headers -TimeoutSec 3
        $notify.ShowBalloonTip(5000, 'PC Agent',
            "Host: $($s.host)`nPlan: $($s.power_plan)`nSimulation: $($s.simulate)`nHerunterfahren in: $($s.pending_shutdown) s",
            [System.Windows.Forms.ToolTipIcon]::Info)
    } catch {
        $notify.ShowBalloonTip(5000, 'PC Agent', 'Nicht erreichbar', [System.Windows.Forms.ToolTipIcon]::Warning)
    }
})

$itemOpen = New-Object System.Windows.Forms.ToolStripMenuItem
$itemOpen.Text = 'Web-Oberflaeche oeffnen'
$itemOpen.Add_Click({ Start-Process "http://127.0.0.1:$port/" })

$itemExit = New-Object System.Windows.Forms.ToolStripMenuItem
$itemExit.Text = 'Tray beenden'
$itemExit.Add_Click({
    $notify.Visible = $false
    $notify.Dispose()
    [System.Windows.Forms.Application]::Exit()
})

[void]$menu.Items.Add($itemStart)
[void]$menu.Items.Add($itemStop)
[void]$menu.Items.Add($itemCheck)
[void]$menu.Items.Add($itemOpen)
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
[void]$menu.Items.Add($itemExit)

$notify.ContextMenuStrip = $menu

# --- Quick-Action-Popup (Linksklick) -------------------------------------
$panel = New-Object System.Windows.Forms.Form
$panel.Text = 'PC Steuerung'
$panel.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedToolWindow
$panel.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$panel.TopMost = $true
$panel.ShowInTaskbar = $false
$panel.ClientSize = New-Object System.Drawing.Size(230, 300)

$panelStatus = New-Object System.Windows.Forms.Label
$panelStatus.Dock = 'Top'
$panelStatus.Height = 28
$panelStatus.TextAlign = 'MiddleLeft'
$panelStatus.Padding = New-Object System.Windows.Forms.Padding(10, 0, 10, 0)
$panelStatus.Text = 'Status: ...'
$panel.Controls.Add($panelStatus)

$flow = New-Object System.Windows.Forms.FlowLayoutPanel
$flow.Dock = 'Fill'
$flow.FlowDirection = 'TopDown'
$flow.WrapContents = $false
$flow.Padding = New-Object System.Windows.Forms.Padding(10)
$panel.Controls.Add($flow)

function Add-PanelButton([string]$text, [scriptblock]$onClick) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text
    $b.Width = 194
    $b.Height = 34
    $b.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 8)
    $b.Add_Click($onClick)
    [void]$flow.Controls.Add($b)
    return $b
}

Add-PanelButton 'Agent starten' {
    $notify.ShowBalloonTip(3000, 'PC Agent', "Agent $(Start-Agent)", [System.Windows.Forms.ToolTipIcon]::Info)
    Update-Ui
} | Out-Null

Add-PanelButton 'Agent stoppen' {
    $notify.ShowBalloonTip(3000, 'PC Agent', "Agent $(Stop-Agent)", [System.Windows.Forms.ToolTipIcon]::Info)
    Update-Ui
} | Out-Null

Add-PanelButton 'PC herunterfahren (60 s)' {
    try {
        Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/shutdown?delay=60" -TimeoutSec 3 | Out-Null
        $notify.ShowBalloonTip(3000, 'PC Agent', 'Herunterfahren in 60 s geplant', [System.Windows.Forms.ToolTipIcon]::Info)
    } catch {
        $notify.ShowBalloonTip(3000, 'PC Agent', 'Nicht erreichbar', [System.Windows.Forms.ToolTipIcon]::Warning)
    }
} | Out-Null

Add-PanelButton 'Abbrechen' {
    try {
        Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/abort" -TimeoutSec 3 | Out-Null
        $notify.ShowBalloonTip(3000, 'PC Agent', 'Herunterfahren abgebrochen', [System.Windows.Forms.ToolTipIcon]::Info)
    } catch {
        $notify.ShowBalloonTip(3000, 'PC Agent', 'Nicht erreichbar', [System.Windows.Forms.ToolTipIcon]::Warning)
    }
} | Out-Null

Add-PanelButton 'Web-Oberflaeche' { Start-Process "http://127.0.0.1:$port/" } | Out-Null

$panel.Add_Deactivate({ $panel.Hide() })

$notify.Add_MouseClick({
    param($sender, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        if (Test-Agent) { $panelStatus.Text = 'Status: laeuft' } else { $panelStatus.Text = 'Status: gestoppt' }
        $pos = [System.Windows.Forms.Cursor]::Position
        $x = $pos.X - [int]($panel.Width / 2)
        $y = $pos.Y - $panel.Height - 10
        $panel.Location = New-Object System.Drawing.Point($x, $y)
        $panel.Show()
        $panel.Activate()
    }
})

function Update-Ui {
    $running = Test-Agent
    if ($running) {
        $notify.Icon = $iconRun
        $notify.Text = "PC Agent - laeuft (Port $port)"
        $itemStatus.Text = 'Status: laeuft'
        $itemStart.Enabled = $false
        $itemStop.Enabled = $true
    } else {
        $notify.Icon = $iconStop
        $notify.Text = "PC Agent - gestoppt"
        $itemStatus.Text = 'Status: gestoppt'
        $itemStart.Enabled = $true
        $itemStop.Enabled = $false
    }
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 2000
$timer.Add_Tick({ Update-Ui })
$timer.Start()

Update-Ui
[System.Windows.Forms.Application]::Run()
