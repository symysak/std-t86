#!/usr/bin/env bash
# ITU-T G.722.1 参考実装をパッチ・ビルドする。
#
# S-Codec の音声エンジンは G.722.1 16kbit/s（320bit/20ms フレーム, 7kHz 帯域）。
# ソースはリポジトリ同梱の ITU 公式配布物
# T-REC-G.722.1-200505-I!!SOFT-ZST-E/Software/Fixed-200505-Rel.2.1/ を用いる
# （basic-op ライブラリは同梱 common/stl-files.zip から展開）。
# 生成物: build/g7221/{g7221_encode,g7221_decode,g7221_sep_decode}
#
# 使い方: bash scripts/build_g7221.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_ROOT/T-REC-G.722.1-200505-I!!SOFT-ZST-E/Software/Fixed-200505-Rel.2.1"
OUT_DIR="$REPO_ROOT/build/g7221"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "[1/5] ITU-T G.722.1 参考実装を展開..."
[ -d "$SRC" ] || { echo "ERROR: G.722.1 ソースが見つかりません: $SRC"; exit 1; }
B="$WORK/build"; mkdir -p "$B"
cp "$SRC"/common/*.c "$SRC"/common/*.h \
   "$SRC"/encode/*.c "$SRC"/encode/*.h \
   "$SRC"/decode/*.c "$SRC"/decode/*.h "$B/"
unzip -oq "$SRC/common/stl-files.zip" -d "$B"   # basop32.c/h count.c/h typedef.h
cd "$B"

echo "[2/5] パッチ適用（modern C 互換）..."
# CRLF → LF（パッチのアンカー照合のため）
LC_ALL=C perl -i -pe 's/\r$//' ./*.c ./*.h
# ITU basic-op の round() が math.h と衝突するため改名
LC_ALL=C perl -i -pe 's/\bround\b/g722_round/g' ./*.c ./*.h
# K&R/implicit-int main を修正
LC_ALL=C perl -i -pe 's/^\s*main\s*\(\s*Word16\s+argc\s*,\s*char\s*\*argv\[\]\s*\)/int main(int argc,char *argv[])/' ./*.c
# STL typedef.h は __unix__ 系でしか型を定義しない（macOS clang は __unix__ 非定義）
LC_ALL=C perl -i -pe 's/defined\(__unix__\)/defined(__unix__) || defined(__APPLE__)/' typedef.h

echo "[3/5] ビルド..."
LIBS="basop32.c common.c dct4_a.c dct4_s.c huff_tab.c tables.c coef2sam.c sam2coef.c decoder.c encoder.c count.c"
mkdir -p "$OUT_DIR"
CC="${CC:-cc}"   # 例: CC=gcc-15 bash scripts/build_g7221.sh
echo "    compiler: $CC"
"$CC" -O2 -w -o "$OUT_DIR/g7221_encode" encode.c $LIBS -lm
"$CC" -O2 -w -o "$OUT_DIR/g7221_decode" decode.c $LIBS -lm

echo "[4/5] S-Codec 適応分離パッチ（ARIB STD-T86 §5.6）→ g7221_sep_decode..."
python3 "$REPO_ROOT/scripts/patch_g7221_scodec.py" "$B"
"$CC" -O2 -w -o "$OUT_DIR/g7221_sep_decode" decode.c $LIBS -lm
# エンコーダも再ビルド（STDT86_MLT_OUT の MLT ダンプは env 未設定なら無効）
"$CC" -O2 -w -o "$OUT_DIR/g7221_encode" encode.c $LIBS -lm

echo "[5/5] 完了: $OUT_DIR"
ls -la "$OUT_DIR"
