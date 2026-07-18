#requires -Version 5.1
<#
.SYNOPSIS
  SDR# プラグイン SDK を自動ダウンロードして参照 DLL を用意し、プラグインをビルドする。

.DESCRIPTION
  https://airspy.com/?ddownload=5944 は SDR# プラグイン SDK の zip で、
  lib/ に SDRSharp.Common.dll / SDRSharp.Radio.dll が入っている。これを refs/ へ
  展開してから dotnet build する。参照 DLL が既に refs/ にあれば再ダウンロードしない
  （-Force で強制再取得）。

.EXAMPLE
  pwsh ./build.ps1
  pwsh ./build.ps1 -Configuration Debug -Force
#>
[CmdletBinding()]
param(
    [string]$SdkUrl = 'https://airspy.com/?ddownload=5944',
    [string]$Configuration = 'Release',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root  = $PSScriptRoot
$refs  = Join-Path $root 'refs'
$cache = Join-Path $root '.sdk-cache'
$need  = @('SDRSharp.Common.dll', 'SDRSharp.Radio.dll')

function Test-Refs { -not ($need | Where-Object { -not (Test-Path (Join-Path $refs $_)) }) }

if ($Force -or -not (Test-Refs)) {
    New-Item -ItemType Directory -Force -Path $refs, $cache | Out-Null
    $zip = Join-Path $cache 'sdrsharp-sdk.zip'
    if ($Force -or -not (Test-Path $zip)) {
        Write-Host "SDR# プラグイン SDK をダウンロード: $SdkUrl"
        Invoke-WebRequest -Uri $SdkUrl -OutFile $zip -UserAgent 'Mozilla/5.0'
    }
    $ext = Join-Path $cache 'extracted'
    Remove-Item -Recurse -Force $ext -ErrorAction SilentlyContinue
    Expand-Archive -Path $zip -DestinationPath $ext -Force
    foreach ($n in $need) {
        $src = Get-ChildItem $ext -Recurse -Filter $n -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $src) { throw "SDK zip 内に $n が見つかりません（URL が変わった可能性）。" }
        Copy-Item $src.FullName (Join-Path $refs $n) -Force
        Write-Host "  refs/$n <- $($src.FullName)"
    }
}
else {
    Write-Host "refs/ に参照 DLL が既にあります（再取得は -Force）。"
}

$proj = Join-Path $root 'SDRSharp.IqTcpServer.csproj'
Write-Host "ビルド: $proj ($Configuration)"
& dotnet build $proj -c $Configuration
if ($LASTEXITCODE -ne 0) { throw "dotnet build に失敗しました（終了コード $LASTEXITCODE）。" }

$dll = Join-Path $root "bin\$Configuration\net9.0-windows\SDRSharp.IqTcpServer.dll"
Write-Host ""
Write-Host "完成: $dll"
Write-Host "→ SDR# の Plugins\ フォルダへコピーして SDRSharp.dotnet9.exe を起動してください。"
