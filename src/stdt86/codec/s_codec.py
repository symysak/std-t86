from __future__ import annotations

import numpy as np

G7221_FRAME_BITS = 320
TCH_BITS = 512
N_PROTECTED = 190
N_UNPROTECTED = 130
CONSTRAINT_LENGTH = 8

CONV_POLYS = (0o247, 0o371)
CRC7_GEN = (1, 0, 0, 1, 0, 1, 0, 1)
CRC7_SHIFT = 9
_PUNCTURE_POS = frozenset(5 + 16 * k for k in range(26))
_IL_SRC = np.array(
    [8 * i - (i // 64) * 510 - (i // 256) * 7 for i in range(TCH_BITS)], dtype=np.int64
)
assert sorted(_IL_SRC.tolist()) == list(range(TCH_BITS))


def conv_encode(bits: np.ndarray, polys: tuple[int, ...], K: int) -> np.ndarray:
    bits = np.asarray(bits, dtype=np.uint8)
    reg = 0
    out = []
    padded = np.concatenate([bits, np.zeros(K - 1, dtype=np.uint8)])
    for b in padded:
        reg = ((reg << 1) | int(b)) & ((1 << K) - 1)
        for p in polys:
            out.append(bin(reg & p).count("1") & 1)
    return np.array(out, dtype=np.uint8)


_VITERBI_TABLES: dict[tuple[tuple[int, ...], int], tuple] = {}


def _viterbi_tables(polys: tuple[int, ...], K: int) -> tuple:
    key = (tuple(polys), K)
    if key not in _VITERBI_TABLES:
        n = len(polys)
        S = 1 << (K - 1)
        out_tab = np.zeros((S, 2, n), dtype=np.uint8)
        for s in range(S):
            for b in (0, 1):
                reg = ((s << 1) | b) & ((1 << K) - 1)
                for j, p in enumerate(polys):
                    out_tab[s, b, j] = bin(reg & p).count("1") & 1
        ns = np.arange(S)
        P0 = ns >> 1
        P1 = P0 | (S >> 1)
        B = (ns & 1).astype(np.int64)
        _VITERBI_TABLES[key] = (out_tab, P0, P1, B)
    return _VITERBI_TABLES[key]


def viterbi_decode(
    coded: np.ndarray, polys: tuple[int, ...], K: int, terminated: bool = True
) -> np.ndarray:
    n = len(polys)
    coded = np.asarray(coded, dtype=np.uint8)
    n_steps = len(coded) // n
    out_tab, P0, P1, B = _viterbi_tables(tuple(polys), K)
    S = out_tab.shape[0]

    INF = 1 << 30
    pm = np.full(S, INF, dtype=np.int64)
    pm[0] = 0
    prev = np.zeros((n_steps, S), dtype=np.int64)
    sym_all = coded[: n_steps * n].reshape(n_steps, n)
    for t in range(n_steps):
        sym = sym_all[t]
        valid = sym != 2
        bm = ((out_tab ^ sym[None, None, :]) & valid[None, None, :]).sum(axis=2)
        cand0 = pm[P0] + bm[P0, B]
        cand1 = pm[P1] + bm[P1, B]
        take1 = cand1 < cand0
        pm = np.where(take1, cand1, cand0)
        prev[t] = np.where(take1, P1, P0)
    state = 0 if terminated else int(np.argmin(pm))
    dec = np.zeros(n_steps, dtype=np.uint8)
    for t in range(n_steps - 1, -1, -1):
        dec[t] = state & 1
        state = int(prev[t, state])
    if terminated:
        dec = dec[: n_steps - (K - 1)]
    return dec


def crc_check(bits: np.ndarray, poly: int, width: int) -> int:
    reg = 0
    topbit = 1 << width
    full = (topbit | poly)
    for b in np.asarray(bits, dtype=np.uint8):
        reg = (reg << 1) | int(b)
        if reg & topbit:
            reg ^= full
    return reg & (topbit - 1)


def crc7(payload: np.ndarray, shift: int = CRC7_SHIFT) -> np.ndarray:
    d = [0] * shift + [int(b) for b in np.asarray(payload, dtype=np.uint8)]
    for i in range(len(d) - 1, 6, -1):
        if d[i]:
            for j, g in enumerate(CRC7_GEN):
                if g:
                    d[i - 7 + j] ^= 1
    return np.array(d[:7], dtype=np.uint8)


def _cvin_from_payload(payload: np.ndarray, crc: np.ndarray) -> np.ndarray:
    cv = np.zeros(197, dtype=np.uint8)
    cv[0:4] = crc[0:4]
    for x in range(4, 99):
        cv[x] = payload[2 * x - 8]
    for x in range(99, 194):
        cv[x] = payload[2 * x - 197]
    cv[194:197] = crc[4:7]
    return cv


def _cvin_to_payload(cv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    payload = np.zeros(N_PROTECTED, dtype=np.uint8)
    crc = np.zeros(7, dtype=np.uint8)
    crc[0:4] = cv[0:4]
    for x in range(4, 99):
        payload[2 * x - 8] = cv[x]
    for x in range(99, 194):
        payload[2 * x - 197] = cv[x]
    crc[4:7] = cv[194:197]
    return payload, crc


def puncture(coded408: np.ndarray) -> np.ndarray:
    v = np.asarray(coded408, dtype=np.uint8)
    return np.array([v[i + (i + 10) // 15] for i in range(382)], dtype=np.uint8)


def depuncture(u382: np.ndarray) -> np.ndarray:
    v = np.full(408, 2, dtype=np.uint8)
    ui = 0
    for j in range(408):
        if j not in _PUNCTURE_POS:
            v[j] = u382[ui]
            ui += 1
    return v


def transmission_encode(mi: np.ndarray) -> np.ndarray:
    mi = np.asarray(mi, dtype=np.uint8)
    if mi.size != G7221_FRAME_BITS:
        raise ValueError(f"mi は {G7221_FRAME_BITS} bit 必要（{mi.size} 受領）。")
    payload, unprotected = mi[:N_PROTECTED], mi[N_PROTECTED:]
    cv = _cvin_from_payload(payload, crc7(payload))
    coded = conv_encode(cv, CONV_POLYS, CONSTRAINT_LENGTH)
    ilin = np.concatenate([unprotected, puncture(coded)])
    tx = np.zeros(TCH_BITS, dtype=np.uint8)
    tx[np.arange(TCH_BITS)] = ilin[_IL_SRC]
    return tx


def transmission_decode(tch: np.ndarray) -> tuple[np.ndarray, int]:
    tch = np.asarray(tch, dtype=np.uint8)
    if tch.size != TCH_BITS:
        raise ValueError(f"TCH は {TCH_BITS} bit 必要（{tch.size} 受領）。")
    ilin = np.zeros(TCH_BITS, dtype=np.uint8)
    ilin[_IL_SRC] = tch
    unprotected, u = ilin[:N_UNPROTECTED], ilin[N_UNPROTECTED:]
    info = viterbi_decode(depuncture(u), CONV_POLYS, CONSTRAINT_LENGTH)
    payload, crc_rx = _cvin_to_payload(info)
    fer = 0 if np.array_equal(crc7(payload), crc_rx) else 1
    return np.concatenate([payload, unprotected]), fer


def _rotl9(x: int, r: int) -> int:
    return ((x << r) | (x >> (9 - r))) & 0x1FF


def _deinterleave_table() -> np.ndarray:
    spec_deint = np.argsort(_IL_SRC)
    return np.array([_rotl9(int(v), 2) ^ 3 for v in spec_deint], dtype=np.int64)


DEINTERLEAVE = _deinterleave_table()
assert DEINTERLEAVE.shape == (TCH_BITS,)
assert sorted(DEINTERLEAVE.tolist()) == list(range(TCH_BITS))
_CRC7_GEN_BITSERIAL = 0xA9


def crc7_ota(region: np.ndarray) -> int:
    bits = [int(b) for b in np.asarray(region, dtype=np.uint8)]
    reg = 0
    for b in bits[:7]:
        reg = (reg << 1) | b
    for b in bits[7:]:
        reg = (reg << 1) | b
        if reg & 0x80:
            reg ^= _CRC7_GEN_BITSERIAL
    for _ in range(7):
        reg <<= 1
        if reg & 0x80:
            reg ^= _CRC7_GEN_BITSERIAL
    return reg & 0x7F


def transmission_decode_ota(tch: np.ndarray) -> tuple[np.ndarray, int]:
    tch = np.asarray(tch, dtype=np.uint8)
    if tch.size != TCH_BITS:
        raise ValueError(f"TCH は {TCH_BITS} bit 必要（{tch.size} 受領）。")
    ilin = tch[DEINTERLEAVE]
    unprotected, u = ilin[:N_UNPROTECTED], ilin[N_UNPROTECTED:]
    info = viterbi_decode(depuncture(u), CONV_POLYS, CONSTRAINT_LENGTH)
    payload = np.zeros(N_PROTECTED, dtype=np.uint8)
    payload[0::2] = info[3:98]
    payload[1::2] = info[98:193]
    rx = 0
    for b in (info[196], info[195], info[194], info[193], info[2], info[1], info[0]):
        rx = (rx << 1) | int(b)
    fer = 0 if crc7_ota(info[3:193]) == rx else 1
    return np.concatenate([payload, unprotected]), fer


def transmission_encode_ota(mr: np.ndarray) -> np.ndarray:
    mr = np.asarray(mr, dtype=np.uint8)
    if mr.size != G7221_FRAME_BITS:
        raise ValueError(f"mr は {G7221_FRAME_BITS} bit 必要（{mr.size} 受領）。")
    payload, unprotected = mr[:N_PROTECTED], mr[N_PROTECTED:]
    info = np.zeros(N_PROTECTED + 7, dtype=np.uint8)
    info[3:98] = payload[0::2]
    info[98:193] = payload[1::2]
    crc = crc7_ota(info[3:193])
    info[196] = (crc >> 6) & 1
    info[195] = (crc >> 5) & 1
    info[194] = (crc >> 4) & 1
    info[193] = (crc >> 3) & 1
    info[2] = (crc >> 2) & 1
    info[1] = (crc >> 1) & 1
    info[0] = crc & 1
    u = puncture(conv_encode(info, CONV_POLYS, CONSTRAINT_LENGTH))
    ilin = np.concatenate([unprotected, u])
    tch = np.zeros(TCH_BITS, dtype=np.uint8)
    tch[DEINTERLEAVE] = ilin
    return tch


def decode_tch_frames(
    tch_slots: np.ndarray, seed: int, ota: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    from stdt86.fec.scrambler import lfsr_pn

    decode = transmission_decode_ota if ota else transmission_decode
    pn = lfsr_pn(seed, TCH_BITS)
    frames = []
    fers = []
    for tch in tch_slots:
        mr, fer = decode(np.asarray(tch, dtype=np.uint8) ^ pn)
        frames.append(mr)
        fers.append(fer)
    return np.array(frames, dtype=np.uint8), np.array(fers, dtype=np.uint8)


def adaptive_placement(
    lengths: list[int], total_bits: int = G7221_FRAME_BITS, protected_bits: int = N_PROTECTED
) -> list[tuple[int, int, str]]:
    fwd = 0
    rev = total_bits
    toggle = 0
    out: list[tuple[int, int, str]] = []
    for length in lengths:
        if fwd < protected_bits:
            out.append((fwd, length, "F"))
            fwd += length
        else:
            if toggle % 2 == 0:
                rev -= length
                out.append((rev, length, "R"))
            else:
                out.append((fwd, length, "F"))
                fwd += length
            toggle += 1
        if fwd > rev:
            raise ValueError("符号長の合計がフレーム長を超えました。")
    return out


def adaptive_multiplex(
    codes: list[np.ndarray], total_bits: int = G7221_FRAME_BITS, protected_bits: int = N_PROTECTED
) -> np.ndarray:
    lengths = [len(c) for c in codes]
    frame = np.zeros(total_bits, dtype=np.uint8)
    for (start, length, _), code in zip(adaptive_placement(lengths, total_bits, protected_bits),
                                        codes, strict=True):
        frame[start : start + length] = code
    return frame


def adaptive_separate(
    frame: np.ndarray,
    lengths: list[int],
    total_bits: int = G7221_FRAME_BITS,
    protected_bits: int = N_PROTECTED,
) -> list[np.ndarray]:
    return [
        frame[start : start + length]
        for start, length, _ in adaptive_placement(lengths, total_bits, protected_bits)
    ]


def to_standard_order(
    frame: np.ndarray,
    lengths: list[int],
    total_bits: int = G7221_FRAME_BITS,
    protected_bits: int = N_PROTECTED,
) -> np.ndarray:
    return np.concatenate(adaptive_separate(frame, lengths, total_bits, protected_bits))


def voice_span(fers: np.ndarray, win: int = 16, need: int = 6) -> tuple[int, int]:
    good = (np.asarray(fers) == 0).astype(np.int64)
    n = good.size
    if good.sum() == 0:
        return 0, 0
    csum = np.concatenate([[0], np.cumsum(good)])
    start = next((i for i in range(n) if good[i] and csum[min(n, i + win)] - csum[i] >= need), None)
    if start is None:
        return 0, 0
    end = next((i + 1 for i in range(n - 1, -1, -1)
                if good[i] and csum[i + 1] - csum[max(0, i - win + 1)] >= need), start)
    return start, end


def conceal_frame_errors(
    frames: np.ndarray, fers: np.ndarray, max_repeat: int = 2, *,
    last_good: np.ndarray | None = None, run: int = 0
) -> np.ndarray:
    frames = np.asarray(frames, dtype=np.uint8)
    fers = np.asarray(fers)
    out = frames.copy()
    for i, fer in enumerate(fers):
        if fer == 0:
            last_good = frames[i]
            run = 0
        else:
            run += 1
            out[i] = last_good if (last_good is not None and run <= max_repeat) \
                else np.zeros(G7221_FRAME_BITS, dtype=np.uint8)
    return out


class SlotGapTracker:

    def __init__(self, slot_samples: float = 1200.0, max_fill: int = 1500) -> None:
        self.slot_samples = float(slot_samples)
        self.max_fill = max_fill
        self._last_pos: int | None = None
        self._ring = 0
        self._voice_rings: set[int] = {0}

    def step(self, pos: int) -> int | None:
        if self._last_pos is None:
            self._last_pos = pos
            return 0
        dq = round((pos - self._last_pos) / self.slot_samples)
        if dq <= 0:
            return None
        full, rem = divmod(dq - 1, 6)
        missing = full * len(self._voice_rings) + sum(
            1 for k in range(1, rem + 1) if (self._ring + k) % 6 in self._voice_rings)
        self._ring = (self._ring + dq) % 6
        self._voice_rings.add(self._ring)
        self._last_pos = pos
        return min(missing, self.max_fill)


def decode_tch_frames_gapped(
    entries: list, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    real = [e for e in entries if e is not None]
    frames = np.zeros((len(entries), G7221_FRAME_BITS), dtype=np.uint8)
    fers = np.ones(len(entries), dtype=np.uint8)
    if real:
        dec_frames, dec_fers = decode_tch_frames(np.array(real), seed)
        idx = [i for i, e in enumerate(entries) if e is not None]
        frames[idx] = dec_frames
        fers[idx] = dec_fers
    return frames, fers


def decode_audio(
    tch_slots: np.ndarray, seed: int, conceal: bool = True, gate: bool = True
) -> np.ndarray:
    from stdt86.codec import g7221

    frames, fers = decode_tch_frames(tch_slots, seed)
    if gate:
        s, e = voice_span(fers)
        frames, fers = frames[s:e], fers[s:e]
    if conceal:
        frames = conceal_frame_errors(frames, fers)
    return g7221.decode(frames, scodec=True)
