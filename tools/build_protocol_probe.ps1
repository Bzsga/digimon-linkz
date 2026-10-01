$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$sourceRoot = (Get-ChildItem -LiteralPath (Join-Path $taskRoot 'source-client') -Directory | Select-Object -First 1).FullName
$outDir = Join-Path $taskRoot 'runtime'
New-Item -ItemType Directory -Path $outDir -Force | Out-Null
$sources = @((Join-Path $PSScriptRoot 'ClientProtocolProbe.cs'), (Join-Path $sourceRoot 'Assembly-CSharp\WebAPIJsonParse.cs'), (Join-Path $sourceRoot 'Secure\ConstValue.cs'))
$sources += Get-ChildItem -LiteralPath (Join-Path $sourceRoot 'Assembly-CSharp\OAuth') -Filter '*.cs' | ForEach-Object FullName
$sources += Get-ChildItem -LiteralPath (Join-Path $sourceRoot 'Assembly-CSharp-firstpass\LitJson') -Filter '*.cs' | ForEach-Object FullName
$staging = Join-Path $outDir 'probe-source'
New-Item -ItemType Directory -Path $staging -Force | Out-Null
$sources = @($sources | ForEach-Object {
    $text = [IO.File]::ReadAllText($_)
    if ($text.Contains('<>f__mg$cache')) {
        $copy = Join-Path $staging ([IO.Path]::GetFileName($_))
        [IO.File]::WriteAllText($copy, $text.Replace('<>f__mg$cache', 'generated_cache_'))
        $copy
    } else { $_ }
})
& 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe' /nologo /target:exe "/out:$outDir\ClientProtocolProbe.exe" $sources
if ($LASTEXITCODE -ne 0) { throw 'Protocol probe compilation failed' }
