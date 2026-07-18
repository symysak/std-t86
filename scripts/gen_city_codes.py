from __future__ import annotations

import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "stdt86" / "data" / "city_codes.py"
ESTAT_BASE = "https://www.e-stat.go.jp/municipalities/cities/areacode"


def _fetch(page: int) -> str:
    url = ESTAT_BASE if page == 1 else f"{ESTAT_BASE}?page={page}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def _parse_page(html: str) -> tuple[list[str], list[list[str]]]:
    tables = re.findall(
        r'<table class="stat-inspect-table[^"]*"[^>]*>(.*?)</table>', html, re.S)
    codes: list[str] = []
    rows: list[list[str]] = []
    for t in tables:
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S):
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if not cells:
                continue
            if len(cells) == 1 and re.fullmatch(r"\d{5}", cells[0]):
                codes.append(cells[0])
            elif len(cells) >= 4:
                rows.append(cells)
    return codes, rows


def fetch_estat() -> dict[int, tuple[str, str, str]]:
    out: dict[int, tuple[str, str, str]] = {}
    page, stale = 1, 0
    while page <= 200 and stale < 2:
        codes, rows = _parse_page(_fetch(page))
        if not codes:
            break
        if len(codes) != len(rows):
            raise RuntimeError(f"page {page}: コード{len(codes)}件と名称{len(rows)}件が不一致")
        before = len(out)
        for code, r in zip(codes, rows, strict=True):
            out[int(code)] = (r[0], r[1], r[3])
        stale = stale + 1 if len(out) == before else 0
        print(f"  page {page}: {len(out)} 件", file=sys.stderr)
        page += 1
        time.sleep(0.2)
    return out


def build_names(sac: dict[int, tuple[str, str, str]]) -> dict[int, str]:
    out: dict[int, str] = {}
    for code, (pref, parent, mun) in sac.items():
        parts = [pref]
        if parent and parent != mun and not parent.endswith(("支庁", "振興局")):
            parts.append(parent)
        if mun:
            parts.append(mun)
        out[code] = " ".join(parts)
    return out


_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    out = []
    for si in root.findall(_NS + "si"):
        parts = []
        for child in si:
            if child.tag == _NS + "t":
                parts.append(child.text or "")
            elif child.tag == _NS + "r":
                for t in child.findall(_NS + "t"):
                    parts.append(t.text or "")
        out.append("".join(parts))
    return out


def read_mic_codes(xlsx: Path) -> set[int]:
    z = zipfile.ZipFile(xlsx)
    shared = _shared_strings(z)
    codes: set[int] = set()
    for sheet in sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet")):
        root = ET.fromstring(z.read(sheet))
        for row in root.iter(_NS + "row"):
            cells: dict[int, str] = {}
            for c in row.findall(_NS + "c"):
                v = c.find(_NS + "v")
                if v is None:
                    continue
                val = shared[int(v.text)] if c.get("t") == "s" else (v.text or "")
                cells[_col(c.get("r"))] = val
            code6 = (cells.get(0) or "").strip()
            mun = (cells.get(2) or "").replace("\n", "").strip()
            if re.fullmatch(r"\d{6}", code6) and mun:
                codes.add(int(code6[:5]))
    return codes


def write_module(mapping: dict[int, str]) -> None:
    lines = [
        '"""全国地方公共団体コード（標準地域コード）→ 市区町村名。',
        "",
        "STD-T86 のスクランブル値はこのコードの下位 9bit（1..511）。",
        "コード体系・名称は総務省統計局「統計に用いる標準地域コード」の公開データ。",
        "scripts/gen_city_codes.py で生成する。",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "CITY_CODES: dict[int, str] = {",
    ]
    for code in sorted(mapping):
        name = mapping[code].replace('"', '\\"')
        lines.append(f'    {code}: "{name}",')
    lines.append("}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    print("e-Stat 標準地域コードを取得中…", file=sys.stderr)
    sac = fetch_estat()
    mapping = build_names(sac)
    if len(argv) > 1:
        mic = read_mic_codes(Path(argv[1]))
        only_estat = sorted(set(mapping) - mic)
        only_mic = sorted(mic - set(mapping))
        if only_estat or only_mic:
            print(f"警告: コード集合が不一致 e-Statのみ={only_estat[:10]} "
                  f"総務省のみ={only_mic[:10]}", file=sys.stderr)
        else:
            print(f"クロスチェック OK: {len(mic)} 件一致", file=sys.stderr)
    write_module(mapping)
    print(f"{OUT}: {len(mapping)} 件を書き出しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
