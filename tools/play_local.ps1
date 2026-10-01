$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $taskRoot
$adb = Join-Path $taskRoot 'android-sdk\platform-tools\adb.exe'
$device = 'emulator-5580'
function Read-Health {
    try { return Invoke-RestMethod 'http://127.0.0.1:3000/health' -TimeoutSec 3 } catch { return $null }
}
$health = Read-Health
if (-not $health) {
    if (Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue) {
        throw '端口3000已被其他程序占用。'
    }
    Write-Host '正在启动本地服务……'
    $python = (Get-Command python).Source
    Start-Process $python -ArgumentList @('-X','utf8','server/linkz_server.py','--public-base','http://10.0.2.2:3000','--asset-root','assets-restored-all') -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput 'runtime/server-play.out.log' -RedirectStandardError 'runtime/server-play.err.log' | Out-Null
    $deadline = (Get-Date).AddSeconds(30)
    do { Start-Sleep -Milliseconds 500; $health = Read-Health } until ($health -or (Get-Date) -gt $deadline)
}
if (-not $health -or $health.service -ne 'linkz-local') { throw '本地服务不可用，请查看 runtime/server-play.err.log。' }
if (-not $health.assets_ready) { throw '本地服务尚未配置游戏资源。' }
$emulatorPresent = (& $adb devices) -match '^emulator-5580\s+(device|offline)$'
if (-not $emulatorPresent) {
    Write-Host '正在打开游戏窗口，模拟器启动可能需要两分钟……'
    Start-Process powershell -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File', ('"' + (Join-Path $PSScriptRoot 'start_emulator.ps1') + '"')) -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput 'runtime/emulator-play.out.log' -RedirectStandardError 'runtime/emulator-play.err.log' | Out-Null
}
$deadline = (Get-Date).AddMinutes(4)
do {
    Start-Sleep -Seconds 2
    $connected = (& $adb devices) -match '^emulator-5580\s+device$'
    $booted = if ($connected) { ([string](& $adb -s $device shell getprop sys.boot_completed 2>$null)).Trim() } else { '' }
    $activityReady = if ($booted -eq '1') {
        (& $adb -s $device shell cmd package resolve-activity --brief org.is_saved.digimon_links_ww 2>$null) -match 'org.is_saved.digimon_links_ww/com.unity3d.player.UnityPlayerActivity'
    } else { $false }
} until ($activityReady -or (Get-Date) -gt $deadline)
if (-not $activityReady) { throw '游戏组件未准备就绪，请查看 runtime/emulator-play.err.log。' }
$launchOutput = & $adb -s $device shell am start -W -n org.is_saved.digimon_links_ww/com.unity3d.player.UnityPlayerActivity 2>&1
$launchOutput | Write-Host
if ($LASTEXITCODE -ne 0 -or ($launchOutput -match 'Error:|Error type')) { throw '游戏启动失败。' }
Write-Host '游戏已打开，请点击标题页的 TAP TO START。游戏进度会自动保存。'
