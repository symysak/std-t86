from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from stdt86.codec.s_codec import viterbi_decode
from stdt86.dsp.burst import BITS_PER_SLOT
from stdt86.fec.convolutional import CONTROL_K, CONTROL_POLYS, crc16_ccitt
from stdt86.fec.scrambler import city_name, descramble, municipal_code_to_seed

CAC_OFFSET = 320
CAC_LEN = 256
CAC_PILOT_AT = 232
CAC_SPAN = CAC_LEN + 4
PAYLOAD_LEN = 104

MSG_BCCH_INFO = 0x10
MSG_IDLE_PCH = 0x20
MSG_BCCH_CHANGED = 0x21
MSG_BROADCAST_START = 0x22
MSG_DELAYED_START = 0x23
MSG_FORCED_RELEASE = 0x30
MSG_IDLE_SCCH = 0x40
MSG_NUMBER_NOTIFY = 0x63
MSG_RADIO_CONTROL = 0x78

_MESSAGES = {
    MSG_BCCH_INFO: ("報知情報", "BCCH"),
    MSG_IDLE_PCH: ("アイドル信号(PCH)", "PCH"),
    MSG_BCCH_CHANGED: ("BCCH変更通知", "PCH"),
    MSG_BROADCAST_START: ("通報開始指示", "PCH"),
    MSG_DELAYED_START: ("時差通報開始指示", "PCH"),
    MSG_FORCED_RELEASE: ("強制切断指示", "PCH"),
    MSG_IDLE_SCCH: ("アイドル信号(SCCH)", "SCCH"),
    MSG_NUMBER_NOTIFY: ("番号通知", "FACCH"),
    MSG_RADIO_CONTROL: ("無線制御要求", "SB"),
}
MESSAGE_TYPES = {t: name for t, (name, _) in _MESSAGES.items()}
_CHANNEL_OF_TYPE = {t: chan for t, (_, chan) in _MESSAGES.items()}
_BCCH_FORMAT_TYPES = frozenset({MSG_BCCH_INFO, MSG_IDLE_PCH, MSG_IDLE_SCCH})

MANUFACTURERS = {
    2: "沖電気工業", 3: "東芝", 5: "日本電気(NEC)", 6: "日本無線(JRC)",
    7: "日立国際電気", 8: "富士通", 10: "松下電器産業(パナソニック)", 11: "三菱電機",
    13: "富士通ゼネラル", 171: "日立国際電気",
}
_PARENT_MODE = {0b01: "間欠送信モード", 0b10: "連続送信モード", 0b00: "予約", 0b11: "予約"}
_MEDIA = {0: "予約/音声なし", 1: "音声", 2: "FAX", 3: "文字", 4: "画像", 5: "テレメータ"}
_RELEASE_REASONS = {
    0b0000: "正常切断/正常解放", 0b0001: "親局からの強制切断", 0b0010: "ビジー",
    0b0011: "相手無応答", 0b0100: "通信時限満了", 0b0110: "チャネル使用不可",
    0b0111: "サービス利用不可", 0b1000: "通信不可", 0b1001: "緊急通話不可",
    0b1010: "無効メッセージ", 0b1011: "同期はずれ", 0b1100: "通信時限以外のタイマ満了",
    0b1110: "番号通知送受失敗/市区町村コード不一致", 0b1111: "その他異常時",
}

FACCH_PAYLOAD_LEN = 232


@dataclass
class ControlMessage:

    raw_hex: str
    msg_type: int
    msg_type_name: str
    channel: str
    crc_ok: bool
    busy: bool
    fields: dict = field(default_factory=dict)


def extract_cac(slot_bits: np.ndarray, offset: int = CAC_OFFSET) -> np.ndarray:
    slot_bits = np.asarray(slot_bits, dtype=np.uint8)
    head = slot_bits[offset : offset + CAC_PILOT_AT]
    tail = slot_bits[offset + CAC_PILOT_AT + 4 : offset + CAC_SPAN]
    return np.concatenate([head, tail])


def decode_cac(cac_bits: np.ndarray, seed: int) -> tuple[np.ndarray, bool]:
    cac_bits = np.asarray(cac_bits, dtype=np.uint8)
    if cac_bits.size != CAC_LEN:
        raise ValueError(f"CAC は {CAC_LEN} bit 必要（{cac_bits.size} 受領）。")
    info = viterbi_decode(descramble(cac_bits, seed), CONTROL_POLYS, CONTROL_K)
    payload = info[:PAYLOAD_LEN].astype(np.uint8)
    rx_crc = _bits_to_int(info[PAYLOAD_LEN:PAYLOAD_LEN + 16])
    crc_ok = crc16_ccitt(payload) == rx_crc
    return payload, crc_ok


def _bits_to_int(bits: np.ndarray) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def _hex(bits: np.ndarray) -> str:
    v = _bits_to_int(bits)
    width = (len(bits) + 3) // 4
    return format(v, f"0{width}x")


def parse_message(payload: np.ndarray, seed: int | None = None) -> ControlMessage:
    payload = np.asarray(payload, dtype=np.uint8)[:PAYLOAD_LEN]
    b = payload
    busy = bool(b[8])
    msg_type = _bits_to_int(b[9:16])
    name = MESSAGE_TYPES.get(msg_type, f"不明(0x{msg_type:02x})")
    channel = _CHANNEL_OF_TYPE.get(msg_type, "CAC")
    fields: dict = {}

    if msg_type in _BCCH_FORMAT_TYPES:
        fields["状況フラグ2"] = _bits_to_int(b[24:27])
        fields["スーパーフレームのフレーム数"] = _bits_to_int(b[27:32])
        fields["状況フラグ1"] = _bits_to_int(b[32:34])
        slot_usage = _bits_to_int(b[34:40])
        fields["スロット使用状況"] = slot_usage
        fields["使用スロット"] = [i for i in range(6) if (slot_usage >> (5 - i)) & 1]
        fields["拡声中放送中"] = bool(b[40])
        fields["メディア種別"] = _MEDIA.get(_bits_to_int(b[41:44]), "予約")
        fields["報知情報更新番号"] = _bits_to_int(b[52:56])
        fields["親局送信モード"] = _PARENT_MODE.get(_bits_to_int(b[56:58]), "?")
        fields["スーパーフレーム長S"] = _bits_to_int(b[59:64])
        fields["PCH数"] = _bits_to_int(b[68:72])
        fields["PCH前のSCCH数"] = _bits_to_int(b[76:80])
        fields["子局識別番号有効ビット数"] = _bits_to_int(b[80:84])
        mfr = _bits_to_int(b[88:104])
        fields["製造者コード"] = mfr
        fields["製造者名"] = MANUFACTURERS.get(mfr, f"不明({mfr})")

    elif msg_type in (MSG_BROADCAST_START, MSG_DELAYED_START):
        fields["呼番号"] = _bits_to_int(b[52:56])
        if msg_type == MSG_DELAYED_START:
            fields["分割番号"] = _bits_to_int(b[48:52])
        fields["子局識別番号1"] = _bits_to_int(b[56:72])
        fields["子局識別番号2"] = _bits_to_int(b[72:88])
        fields["戸別受信機強制音量"] = bool(b[93])
        vol = _bits_to_int(b[94:96])
        fields["音量設定値"] = {0: "通常", 1: "最小", 2: "最大", 3: "予約"}[vol]
        fields["N2"] = bool(b[96])
        fields["N1"] = bool(b[97])
        fields["通報開始指示位置"] = _bits_to_int(b[98:104])

    elif msg_type == MSG_FORCED_RELEASE:
        fields["呼番号"] = _bits_to_int(b[52:56])
        reason = _bits_to_int(b[60:64])
        fields["切断理由"] = _RELEASE_REASONS.get(reason, f"予約({reason})")

    if seed is not None:
        fields["市区町村コード"] = seed

    return ControlMessage(
        raw_hex=_hex(payload), msg_type=msg_type, msg_type_name=name,
        channel=channel, crc_ok=False, busy=busy, fields=fields,
    )


def broadcast_target(msg: ControlMessage,
                     valid_bits: int | None = None) -> dict | None:
    if msg.msg_type not in (MSG_BROADCAST_START, MSG_DELAYED_START):
        return None
    ids = []
    if msg.fields.get("N1") and msg.fields.get("子局識別番号1") is not None:
        ids.append(int(msg.fields["子局識別番号1"]))
    if msg.fields.get("N2") and msg.fields.get("子局識別番号2") is not None:
        ids.append(int(msg.fields["子局識別番号2"]))
    mask = (1 << valid_bits) - 1 if valid_bits else 0xFFFF
    eff = [i & mask for i in ids]
    note = ""
    if not ids:
        kind, label = "unknown", "不明（有効な子局識別番号なし）"
    elif all(e == 0 for e in eff):
        kind, label = "all", "一斉（全子局）"
    else:
        kind = "selective"
        label = "子局/群 " + "・".join(str(e) for e in eff)
        if all(e == mask for e in eff):
            note = ("全1: §4.3.7 に「一括番号（システム内の全１）」の記載があり"
                    "一斉の可能性があるが、原文が曖昧なため断定しない")
    return {
        "kind": kind,
        "label": label,
        "ids": ids,
        "effective_ids": eff,
        "valid_bits": valid_bits,
        "call_no": msg.fields.get("呼番号"),
        "note": note,
    }


def decode_slot(slot_bits: np.ndarray, seed: int, offset: int = CAC_OFFSET) -> ControlMessage:
    payload, crc_ok = decode_cac(extract_cac(slot_bits, offset), seed)
    msg = parse_message(payload, seed=seed)
    msg.crc_ok = crc_ok
    return msg


def parse_facch_message(payload: np.ndarray) -> ControlMessage:
    payload = np.asarray(payload, dtype=np.uint8)[:FACCH_PAYLOAD_LEN]
    b = payload
    msg_type = _bits_to_int(b[9:16])
    name = MESSAGE_TYPES.get(msg_type, f"不明(0x{msg_type:02x})")
    fields: dict = {}
    if msg_type == MSG_NUMBER_NOTIFY:
        fields["呼番号"] = _bits_to_int(b[52:56])
        fields["子局識別番号"] = _bits_to_int(b[56:72])
        code = _bits_to_int(b[72:88])
        fields["市区町村コード(完全)"] = code
        city = city_for_municipal_code(code)
        if city:
            fields["市区町村名"] = city
        mfr = _bits_to_int(b[88:104])
        fields["製造者コード"] = mfr
        fields["製造者名"] = MANUFACTURERS.get(mfr, f"不明({mfr})")
        fields["免許人固有情報長"] = _bits_to_int(b[104:112])
    return ControlMessage(
        raw_hex=_hex(payload), msg_type=msg_type, msg_type_name=name,
        channel="FACCH", crc_ok=False, busy=False, fields=fields,
    )


def decode_facch(tch_bits: np.ndarray, seed: int) -> ControlMessage:
    tch_bits = np.asarray(tch_bits, dtype=np.uint8)
    if tch_bits.size != 512:
        raise ValueError(f"FACCH は 512bit 必要（{tch_bits.size} 受領）。")
    info = viterbi_decode(descramble(tch_bits, seed), CONTROL_POLYS, CONTROL_K)
    payload = info[:FACCH_PAYLOAD_LEN].astype(np.uint8)
    rx_crc = _bits_to_int(info[FACCH_PAYLOAD_LEN:FACCH_PAYLOAD_LEN + 16])
    msg = parse_facch_message(payload)
    msg.crc_ok = crc16_ccitt(payload) == rx_crc
    return msg


def candidates_for_seed(seed: int) -> list[tuple[int, str]]:
    from stdt86.data.city_codes import CITY_CODES

    return [(c, n) for c, n in CITY_CODES.items() if (c & 0x1FF) == (seed & 0x1FF)]


def load_raw_slots(path: str, slots_per_frame: int = 6) -> list[np.ndarray]:
    toks: list[str] = []
    for ln in open(path, encoding="utf-8", errors="ignore"):
        toks += ln.split()
    control = [t for t in toks[::slots_per_frame] if len(t) == BITS_PER_SLOT // 4]
    out = []
    for t in control:
        v = int(t, 16)
        out.append(np.array([(v >> (BITS_PER_SLOT - 1 - i)) & 1
                             for i in range(BITS_PER_SLOT)], dtype=np.uint8))
    return out


def decode_slots(slots: list[np.ndarray], seed: int) -> list[ControlMessage]:
    return [decode_slot(s, seed) for s in slots]


def summarize(messages: list[ControlMessage], seed: int,
              municipal_code: int | None = None) -> dict:
    from collections import Counter

    valid = [m for m in messages if m.crc_ok and m.msg_type in MESSAGE_TYPES]
    pool = valid or [m for m in messages if m.msg_type in MESSAGE_TYPES]
    bcch = [m for m in pool if m.msg_type in _BCCH_FORMAT_TYPES]

    def majority(vals):
        vals = [v for v in vals if v is not None]
        return Counter(vals).most_common(1)[0][0] if vals else None

    mfr_counts = Counter(
        m.fields["製造者名"] for m in bcch
        if m.fields.get("製造者コード") in MANUFACTURERS
    )
    slot_usage = majority(tuple(m.fields["使用スロット"]) for m in bcch
                          if m.fields.get("使用スロット"))
    parent_mode = majority(m.fields.get("親局送信モード") for m in bcch)
    superframe = majority(m.fields.get("スーパーフレーム長S") for m in bcch)
    active = (
        sum(1 for m in bcch if m.fields.get("拡声中放送中")) > len(bcch) / 2
        or any(m.msg_type == MSG_BROADCAST_START for m in pool)
    )

    city = city_name(municipal_code) if municipal_code else None
    return {
        "seed": seed,
        "municipal_code": municipal_code,
        "municipality": city,
        "candidates": candidates_for_seed(seed),
        "type_counts": Counter(m.msg_type_name for m in pool),
        "manufacturers": dict(mfr_counts.most_common()),
        "slot_usage": list(slot_usage) if slot_usage else [],
        "broadcast_active": bool(active),
        "parent_mode": parent_mode,
        "superframe_len": superframe,
        "total": len(messages),
        "valid": len(pool),
        "crc_ok": sum(1 for m in messages if m.crc_ok),
        "recent": [(m.msg_type_name, m.channel, m.raw_hex) for m in pool[-12:]],
    }


def broadcast_windows(
    control: list[tuple[int, ControlMessage]], end_pos: int
) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    open_pos: int | None = None
    for pos, msg in control:
        if (msg.msg_type in (MSG_BROADCAST_START, MSG_DELAYED_START)
                and open_pos is None):
            open_pos = pos
        elif msg.msg_type == MSG_FORCED_RELEASE and open_pos is not None:
            windows.append((open_pos, pos))
            open_pos = None
    if open_pos is not None:
        windows.append((open_pos, end_pos))
    return windows


def score_seed(cacs: list[np.ndarray], seed: int) -> tuple[float, int, int]:
    crc_hits = 0
    known = 0
    mfr = 0
    for cac in cacs:
        payload, crc_ok = decode_cac(cac, seed)
        msg = parse_message(payload)
        crc_hits += int(crc_ok)
        if msg.msg_type in MESSAGE_TYPES:
            known += 1
            if msg.fields.get("製造者コード") in MANUFACTURERS:
                mfr += 1
    return 3.0 * crc_hits + known + 2.0 * mfr, crc_hits, known


def detect_seed(cacs: list[np.ndarray], top: int = 48) -> tuple[int, dict]:
    from stdt86.fec.seed_search import SeedSearcher

    ss = SeedSearcher(top=top)
    for cac in cacs:
        ss.push(cac)
    scored = sorted(
        ((score_seed(cacs, s), s) for s in ss.candidates()), reverse=True)
    (score, crc_hits, known), seed = scored[0]
    second = scored[1][0][0] if len(scored) > 1 else 0.0
    confident = (score >= max(6.0, len(cacs) * 0.5)
                 and score >= 1.5 * max(second, 1.0))
    return seed, {
        "score": score,
        "second_score": second,
        "confident": confident,
        "crc_hits": crc_hits,
        "known": known,
        "n_slots": len(cacs),
        "ranking": ss.ranking(),
        "candidates": candidates_for_seed(seed),
    }


def manufacturer_name(code: int) -> str:
    return MANUFACTURERS.get(code, f"不明({code})")


def city_for_municipal_code(code: int) -> str | None:
    return city_name(code)


def seed_for_municipal_code(code: int) -> int:
    return municipal_code_to_seed(code)
