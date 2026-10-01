param(
    [string]$InputApk = 'client-local/linkz-local-cn-unsigned.apk',
    [string]$AlignedApk = 'client-local/linkz-local-cn-aligned.apk',
    [string]$OutputApk = 'client-local/linkz-local-cn.apk',
    [string]$ReportFile = 'research/bundled-chinese-patch.json'
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $taskRoot
if (-not $env:LINKZ_KEYSTORE_PASSWORD) { throw 'Set LINKZ_KEYSTORE_PASSWORD to the existing keystore password. Do not create a new key.' }
$java = 'D:\Java\jdk-13.0.2\bin\java.exe'
$build = Join-Path $taskRoot 'android-sdk\build-tools\android-11'
$signer = Join-Path $build 'lib\apksigner.jar'
& (Join-Path $build 'zipalign.exe') -f -p 4 $InputApk $AlignedApk
if ($LASTEXITCODE -ne 0) { throw 'APK alignment failed' }
& $java -jar $signer sign --ks client-local/local-development.jks --ks-key-alias linkz-local --ks-pass env:LINKZ_KEYSTORE_PASSWORD --key-pass env:LINKZ_KEYSTORE_PASSWORD --out $OutputApk $AlignedApk
if ($LASTEXITCODE -ne 0) { throw 'APK signing failed' }
$certificates = @()
foreach ($apk in @('client-local/linkz-local.apk', $OutputApk)) {
    $result = & $java -jar $signer verify --print-certs $apk
    if ($LASTEXITCODE -ne 0) { throw "Signature verification failed: $apk" }
    $certificate = @($result | Select-String -Pattern '^Signer #1 certificate SHA-256 digest:')
    if ($certificate.Count -ne 1) { throw 'Expected one signing certificate' }
    $certificates += $certificate[0].ToString()
}
if ($certificates[0] -ne $certificates[1]) { throw 'Candidate signing identity differs; do not install it' }
$reportPath = Join-Path $taskRoot $ReportFile
$report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
$report | Add-Member -Force NoteProperty signed $true
$report | Add-Member -Force NoteProperty same_certificate $true
$hashAlgorithm = [System.Security.Cryptography.SHA256]::Create()
$apkStream = [System.IO.File]::OpenRead((Join-Path $taskRoot $OutputApk))
try { $digest = [BitConverter]::ToString($hashAlgorithm.ComputeHash($apkStream)).Replace('-','').ToLowerInvariant() }
finally { $apkStream.Dispose(); $hashAlgorithm.Dispose() }
$report | Add-Member -Force NoteProperty candidate_sha256 $digest
$report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $reportPath -Encoding UTF8
Write-Host 'Candidate signed and verified with the original certificate. Install with adb install -r; preserve application data.'
