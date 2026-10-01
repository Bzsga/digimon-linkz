param([switch]$Headless)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$env:ANDROID_SDK_ROOT = Join-Path $taskRoot 'android-sdk'
$env:ANDROID_AVD_HOME = Join-Path $taskRoot 'android-avd'
$emulatorArgs = @('-avd','linkz-local','-no-snapshot','-no-boot-anim','-no-metrics','-gpu','swiftshader_indirect','-port','5580')
if ($Headless) { $emulatorArgs += @('-no-window','-no-audio') }
& (Join-Path $env:ANDROID_SDK_ROOT 'emulator\emulator.exe') @emulatorArgs
