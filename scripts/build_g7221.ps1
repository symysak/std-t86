#Requires -Version 5.1
# ITU-T G.722.1 参考実装をパッチ・ビルドする（scripts/build_g7221.sh の Windows 版）。
#
# S-Codec の音声エンジンは G.722.1 16kbit/s（320bit/20ms フレーム, 7kHz 帯域）。
# ソースはリポジトリ同梱の ITU 公式配布物
# T-REC-G.722.1-200505-I!!SOFT-ZST-E/Software/Fixed-200505-Rel.2.1/ を用いる
# （basic-op ライブラリは同梱 common/stl-files.zip から展開）。
# 生成物: build/g7221/{g7221_encode,g7221_decode,g7221_sep_decode}.exe
#
# 必要なもの: gcc か clang（MinGW-w64 / MSYS2 / CodeBlocks 同梱等）と Python 3。
# 使い方: pwsh scripts/build_g7221.ps1   （$env:CC でコンパイラを上書き可）
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Src = Join-Path $RepoRoot 'T-REC-G.722.1-200505-I!!SOFT-ZST-E\Software\Fixed-200505-Rel.2.1'
$OutDir = Join-Path $RepoRoot 'build\g7221'
$Work = Join-Path ([IO.Path]::GetTempPath()) ("g7221_build_" + [Guid]::NewGuid().ToString('N'))

# gcc 系コンパイラ（MSVC cl は非対応。フラグ体系が違うため）
$CC = $env:CC
if (-not $CC) {
    foreach ($c in 'gcc', 'clang', 'cc') {
        if (Get-Command $c -ErrorAction SilentlyContinue) { $CC = $c; break }
    }
}
if (-not $CC) {
    throw 'C コンパイラが見つかりません。MinGW-w64 の gcc/clang を PATH に入れるか $env:CC で指定してください。'
}

# パッチ用 Python（標準ライブラリのみ使用。Store スタブの python.exe は除外）
$Python = $null
foreach ($c in 'python', 'python3') {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { $Python = @($cmd.Source); break }
}
if (-not $Python -and (Get-Command py -ErrorAction SilentlyContinue)) { $Python = @('py', '-3') }
if (-not $Python -and (Get-Command uv -ErrorAction SilentlyContinue)) { $Python = @('uv', 'run', 'python') }
if (-not $Python) { throw 'Python 3 が見つかりません。' }

# latin-1（バイト透過）で読み書きし、ITU ソースの非 ASCII 文字を保存する
$Latin1 = [Text.Encoding]::GetEncoding('iso-8859-1')

try {
    Write-Host '[1/5] ITU-T G.722.1 参考実装を展開...'
    if (-not (Test-Path -LiteralPath $Src)) { throw "ERROR: G.722.1 ソースが見つかりません: $Src" }
    $B = Join-Path $Work 'build'
    New-Item -ItemType Directory -Force $B | Out-Null
    foreach ($d in 'common', 'encode', 'decode') {
        Get-ChildItem -LiteralPath (Join-Path $Src $d) -File |
            Where-Object { $_.Extension -in '.c', '.h' } |
            Copy-Item -Destination $B
    }
    # basop32.c/h count.c/h typedef.h
    Expand-Archive -LiteralPath (Join-Path $Src 'common\stl-files.zip') -DestinationPath $B -Force

    Write-Host '[2/5] パッチ適用（modern C 互換）...'
    foreach ($f in Get-ChildItem $B -File | Where-Object { $_.Extension -in '.c', '.h' }) {
        $t = [IO.File]::ReadAllText($f.FullName, $Latin1)
        # CRLF → LF（patch_g7221_scodec.py のアンカー照合のため）
        $t = $t.Replace("`r`n", "`n")
        # ITU basic-op の round() が math.h と衝突するため改名（-creplace: 大文字小文字を区別）
        $t = $t -creplace '\bround\b', 'g722_round'
        if ($f.Extension -eq '.c') {
            # K&R/implicit-int main を修正
            $t = $t -creplace '(?m)^\s*main\s*\(\s*Word16\s+argc\s*,\s*char\s*\*argv\[\]\s*\)', 'int main(int argc,char *argv[])'
        }
        if ($f.Name -eq 'typedef.h') {
            # STL typedef.h は MinGW（__unix__ も _MSC_VER も非定義）で型が未定義になる。
            # Windows は LLP64 で long=32bit なので _MSC_VER 分岐（Word32=long）に相乗りする。
            $t = $t.Replace('defined(_MSC_VER)', 'defined(_MSC_VER) || defined(_WIN32)')
        }
        [IO.File]::WriteAllText($f.FullName, $t, $Latin1)
    }

    Write-Host '[3/5] ビルド...'
    $Libs = 'basop32.c', 'common.c', 'dct4_a.c', 'dct4_s.c', 'huff_tab.c', 'tables.c',
            'coef2sam.c', 'sam2coef.c', 'decoder.c', 'encoder.c', 'count.c'
    New-Item -ItemType Directory -Force $OutDir | Out-Null
    Write-Host "    compiler: $CC"
    Push-Location $B
    try {
        & $CC -O2 -w -o (Join-Path $OutDir 'g7221_encode.exe') encode.c @Libs
        if ($LASTEXITCODE) { throw "g7221_encode のビルドに失敗しました (exit $LASTEXITCODE)" }
        & $CC -O2 -w -o (Join-Path $OutDir 'g7221_decode.exe') decode.c @Libs
        if ($LASTEXITCODE) { throw "g7221_decode のビルドに失敗しました (exit $LASTEXITCODE)" }

        Write-Host '[4/5] S-Codec 適応分離パッチ（ARIB STD-T86 §5.6）→ g7221_sep_decode...'
        $PatchCmd = @($Python) + @((Join-Path $RepoRoot 'scripts\patch_g7221_scodec.py'), $B)
        & $PatchCmd[0] $PatchCmd[1..($PatchCmd.Length - 1)]
        if ($LASTEXITCODE) { throw "patch_g7221_scodec.py が失敗しました (exit $LASTEXITCODE)" }
        & $CC -O2 -w -o (Join-Path $OutDir 'g7221_sep_decode.exe') decode.c @Libs
        if ($LASTEXITCODE) { throw "g7221_sep_decode のビルドに失敗しました (exit $LASTEXITCODE)" }
    }
    finally { Pop-Location }

    Write-Host "[5/5] 完了: $OutDir"
    Get-ChildItem $OutDir
}
finally {
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
