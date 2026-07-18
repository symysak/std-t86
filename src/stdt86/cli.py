from __future__ import annotations

import argparse
from pathlib import Path

from stdt86.dsp.ddc import downconvert, resample_to_sps
from stdt86.dsp.demod_16qam import demodulate_16qam
from stdt86.dsp.spectrum import estimate_channel_offset, plot_spectrum
from stdt86.io import load_iq
from stdt86.viz import plot_constellation


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("iq", help="入力 I/Q ファイル (.cu8/.cf32/.wav)")
    p.add_argument("--fmt", default="auto", help="形式 (auto/cu8/cf32/wav)")
    p.add_argument("--fs", type=float, default=None, help="生 I/Q のサンプルレート [Hz]")


def cmd_spectrum(args: argparse.Namespace) -> int:
    samples, fs = load_iq(args.iq, fmt=args.fmt, fs=args.fs)
    out = args.out or f"figures/{Path(args.iq).stem}_spectrum.png"
    offset = plot_spectrum(samples, fs, out, title=Path(args.iq).name)
    print(f"fs={fs/1e6:.4f} MS/s, samples={len(samples)}")
    print(f"推定チャネルオフセット f0 = {offset/1e3:.2f} kHz")
    print(f"図: {out}")
    return 0


def cmd_demod(args: argparse.Namespace) -> int:
    samples, fs = load_iq(args.iq, fmt=args.fmt, fs=args.fs)
    f0 = args.f0
    if f0 is None:
        f0 = estimate_channel_offset(samples, fs)
        print(f"f0 未指定 → 推定 {f0/1e3:.2f} kHz")
    bb = downconvert(samples, fs, f0)
    bb, fs_bb = resample_to_sps(bb, fs, args.symbol_rate, sps=args.sps)
    print(f"DDC 後: fs={fs_bb:.1f} Hz, {len(bb)} samples ({args.sps} sps)")
    res = demodulate_16qam(bb, sps=args.sps, beta=args.beta, loop_bw=args.loop_bw)
    out = args.out or f"figures/{Path(args.iq).stem}_constellation.png"
    plot_constellation(res.symbols, out, title=Path(args.iq).name, evm=res.evm)
    print(f"EVM = {res.evm:.2f}%  (timing offset {res.timing_offset:.2f} samp)")
    print(f"図: {out}")
    return 0


def cmd_slots(args: argparse.Namespace) -> int:
    import numpy as np

    from stdt86.dsp.burst import SYMBOL_RATE, demod_bursts
    from stdt86.dsp.spectrum import welch_psd

    samples, fs = load_iq(args.iq, fmt=args.fmt, fs=args.fs)
    f0 = args.f0
    if f0 is None:
        freqs, psd_db = welch_psd(samples, fs, nperseg=65536)
        keep = np.abs(freqs) <= args.search_bw
        p = 10.0 ** (psd_db[keep] / 10.0)
        f0 = float(np.sum(p * freqs[keep]) / np.sum(p))
        print(f"f0 未指定 → PSD重心 {f0/1e3:.2f} kHz（±{args.search_bw/1e3:.0f}kHz内）")
    from stdt86.dsp.ddc import channel_filter

    bb = downconvert(samples, fs, f0)
    bb = channel_filter(bb, fs, 16_000.0)
    bb, fs_bb = resample_to_sps(bb, fs, SYMBOL_RATE, sps=args.sps)
    results = demod_bursts(bb, fs_bb, sps=args.sps, beta=args.beta, max_bursts=args.max_bursts)
    if not results:
        print("バーストが見つかりませんでした。")
        return 1
    evms = [r.evm for r in results]
    print(f"バースト {len(results)} 本, EVM 中央値 {np.median(evms):.1f}% / 最良 {min(evms):.1f}%")
    best = sorted(results, key=lambda r: r.evm)[: args.plot_best]
    syms = np.concatenate([r.symbols[10:-4] for r in best])
    out = args.out or f"figures/{Path(args.iq).stem}_slots.png"
    plot_constellation(
        syms, out, title=f"{Path(args.iq).name} best {len(best)} bursts", evm=best[0].evm
    )
    print(f"図: {out}")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    import uvicorn

    from stdt86.fec.scrambler import municipal_code_to_seed
    from stdt86.io.sources import open_source
    from stdt86.server.app import create_app
    from stdt86.server.pipeline import Pipeline

    if args.seed is not None:
        seed = args.seed
    elif args.municipal_code is not None:
        seed = municipal_code_to_seed(args.municipal_code)
    else:
        seed = None
        print("スクランブル値: 自動判定モード（制御スロット蓄積後に確定します）")
    if args.offset is None:
        print("チャネルオフセット: 0 kHz（未指定。SDR# 等で同調済みの前提。"
              "ベースバンドにオフセットがある録音は --offset で指定）")
    is_network = args.source.startswith(("rtltcp://", "tcp://"))
    if is_network and args.fs is None:
        print("ネットワークソースには --fs（サンプルレート [Hz]）が必須です。")
        return 2

    source = open_source(
        args.source, fs=args.fs, freq_hz=args.freq, fmt=args.fmt,
        realtime=not args.full_speed, speed=args.speed,
    )
    f0 = args.offset * 1e3 if args.offset is not None else 0.0
    pipeline = Pipeline(source, f0=f0, seed=seed,
                        municipal_code=args.municipal_code,
                        sync_thresh=args.sync_thresh,
                        audio_log_dir=args.log_dir)
    off_desc = f"{args.offset:+.1f}kHz" if args.offset is not None else "0(未指定)"
    pipeline.state.source_desc = (
        f"{args.source} (fs={source.fs/1e6:.4g}MS/s, offset={off_desc}, "
        f"seed={'自動' if seed is None else seed})")
    app = create_app(pipeline)
    if pipeline.audio.log_dir is not None:
        print(f"デコード音声の保存先: {pipeline.audio.log_dir} "
              "(通報検出時に WAV を書き出します)")
    print(f"http://{args.host}:{args.port}/ でライブモニタを開けます (Ctrl-C で終了)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                timeout_graceful_shutdown=3)
    return 0


def cmd_decode_audio(args: argparse.Namespace) -> int:
    import numpy as np
    import soundfile as sf

    from stdt86.codec import g7221, s_codec
    from stdt86.control import channel as ch
    from stdt86.dsp import iq_control as iqc
    from stdt86.fec.scrambler import municipal_code_to_seed

    if args.seed is not None:
        seed = args.seed
    elif args.municipal_code is not None:
        seed = municipal_code_to_seed(args.municipal_code)
    else:
        print("--municipal-code か --seed を指定してください（スクランブル値）。")
        return 2

    f0 = args.offset * 1e3 if args.offset is not None else None
    samples, fs = load_iq(args.iq, fmt=args.fmt, fs=args.fs, max_seconds=args.max_seconds)
    print(f"fs={fs/1e6:.4f} MS/s, {len(samples)/fs:.1f}s 読込")

    res = iqc.demod_broadcast(samples, fs, seed, f0=f0)
    fs_bb = res["fs_bb"]
    control, bursts = res["control"], res["tch"]
    if not bursts:
        print("TCH スロット（同期ワード S3）が見つかりませんでした。")
        return 1

    n_start = sum(1 for _, m in control if m.msg_type == ch.MSG_BROADCAST_START)
    n_stop = sum(1 for _, m in control if m.msg_type == ch.MSG_FORCED_RELEASE)
    end_pos = max(b.pos for b in bursts) + 1
    windows = ch.broadcast_windows(control, end_pos)
    print(f"制御 {len(control)} 通（通報開始指示 {n_start} / 強制切断指示 {n_stop}）")
    for i, (a, z) in enumerate(windows):
        print(f"  放送区間 {i+1}: {a/fs_bb:.1f}s → {z/fs_bb:.1f}s")
    if not windows:
        print("通報開始指示が見つからないため、全区間を対象にします（録音が途中からの可能性）。")
        windows = [(0, end_pos)]

    voice_flags = iqc.smooth_voice_flags(bursts)
    selected = [
        b for b, v in zip(bursts, voice_flags, strict=True)
        if v and any(a <= b.pos < z for a, z in windows)
    ]
    from collections import Counter

    ctypes = Counter(b.ctype for b in bursts)
    print(f"TCH バースト {len(bursts)} 本（C 判定: "
          + " ".join(f"{k}={v}" for k, v in ctypes.most_common()) + "）")
    print(f"→ 放送区間内の音声スロット {len(selected)} 本を復号 (Viterbi K=8)...")
    if not selected:
        print("復号対象の音声スロットがありません。")
        return 1

    def _window_of(pos: int) -> int:
        return next((i for i, (a, z) in enumerate(windows) if a <= pos < z), -1)

    entries: list = []
    stale = 0
    gaps = None
    cur_win = None
    for b in selected:
        w = _window_of(b.pos)
        if w != cur_win:
            cur_win, gaps = w, s_codec.SlotGapTracker()
        missing = gaps.step(b.pos)
        if missing is None:
            stale += 1
            continue
        entries.extend([None] * missing)
        entries.append(b.bits)
    n_fill = sum(1 for e in entries if e is None)
    frames, fers = s_codec.decode_tch_frames_gapped(entries, seed)
    ok = int(np.sum(fers == 0))
    print(f"CRC7 一致 {ok}/{len(frames) - n_fill} フレーム"
          f"（欠落スロット補間 {n_fill} / 位置逆行破棄 {stale}。"
          "誤り残留で不一致でも復号継続）")

    pcm = g7221.decode(s_codec.conceal_frame_errors(frames, fers), scodec=True)
    out = args.out or f"figures/{Path(args.iq).stem}_audio.wav"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    sf.write(out, np.clip(pcm, -1.0, 1.0), 16000)
    print(f"音声: {out} ({len(pcm)/16000:.1f}s, 16kHz)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stdt86", description="STD-T86 受信デコーダ")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spectrum", help="スペクトル表示 + チャネル推定")
    _add_common(sp)
    sp.add_argument("--out", default=None, help="出力 PNG")
    sp.set_defaults(func=cmd_spectrum)

    dm = sub.add_parser("demod", help="16QAM 復調 + コンスタレーション")
    _add_common(dm)
    dm.add_argument("--f0", type=float, default=None, help="チャネルオフセット [Hz]（省略で自動）")
    dm.add_argument("--symbol-rate", dest="symbol_rate", type=float, default=9600.0,
                    help="シンボルレート [baud]")
    dm.add_argument("--sps", type=int, default=4, help="復調オーバーサンプル")
    dm.add_argument("--beta", type=float, default=0.35, help="RRC ロールオフ")
    dm.add_argument("--loop-bw", dest="loop_bw", type=float, default=0.01, help="搬送波 PLL 帯域")
    dm.add_argument("--out", default=None, help="出力 PNG")
    dm.set_defaults(func=cmd_demod)

    sl = sub.add_parser("slots", help="TDMAバースト検出 + フィードフォワード復調 (Rs=11250)")
    _add_common(sl)
    sl.add_argument("--f0", type=float, default=None, help="チャネルオフセット [Hz]（省略で自動）")
    sl.add_argument("--search-bw", dest="search_bw", type=float, default=10_000.0,
                    help="f0 自動推定の探索範囲 ±[Hz]（同調済み前提）")
    sl.add_argument("--sps", type=int, default=4, help="復調オーバーサンプル")
    sl.add_argument("--beta", type=float, default=0.5, help="RRC ロールオフ")
    sl.add_argument("--max-bursts", dest="max_bursts", type=int, default=100,
                    help="処理する最大バースト数")
    sl.add_argument("--plot-best", dest="plot_best", type=int, default=5,
                    help="コンスタレーションに使う低EVMバースト数")
    sl.add_argument("--out", default=None, help="出力 PNG")
    sl.set_defaults(func=cmd_slots)

    lv = sub.add_parser(
        "live",
        help="リアルタイム受信 + Webモニタ (rtltcp://host:port / tcp://host:port / ファイル)")
    lv.add_argument("source",
                    help="入力: rtltcp://host:port, tcp://host:port, または録音ファイル"
                         "（ファイルは実時間リプレイ）")
    lv.add_argument("--municipal-code", dest="municipal_code", type=int, default=None,
                    help="市区町村コード（例 40225=うきは市, スクランブル値を決定。"
                         "省略で自動判定）")
    lv.add_argument("--seed", type=int, default=None,
                    help="スクランブル値を直接指定（--municipal-code の代わり。省略で自動判定）")
    lv.add_argument("--offset", type=float, default=None,
                    help="チャネルのベースバンドオフセット [kHz]（例 -83）。"
                         "省略で 0（SDR# 等で同調済みの前提）")
    lv.add_argument("--fs", type=float, default=None,
                    help="サンプルレート [Hz]（ネットワークソースで必須）")
    lv.add_argument("--freq", type=float, default=None,
                    help="rtl_tcp のチューナ中心周波数 [Hz]（例 58588000）")
    lv.add_argument("--fmt", default="auto",
                    help="生TCP/ファイルの形式 (cu8/cs16/cf32/wav)")
    lv.add_argument("--sync-thresh", dest="sync_thresh", type=float, default=0.6,
                    help="同期ワード相関しきい値")
    lv.add_argument("--full-speed", dest="full_speed", action="store_true",
                    help="ファイルソースを実時間ペーシングせず全速で処理")
    lv.add_argument("--speed", type=float, default=1.0,
                    help="ファイルリプレイの倍速（--full-speed 無指定時）")
    lv.add_argument("--log-dir", dest="log_dir", default="logs",
                    help="通報ウィンドウ毎のデコード音声 WAV の保存先"
                         "（既定 logs/。空文字で保存無効）")
    lv.add_argument("--host", default="127.0.0.1", help="Web サーバの bind アドレス")
    lv.add_argument("--port", type=int, default=8000, help="Web サーバのポート")
    lv.set_defaults(func=cmd_live)

    da = sub.add_parser("decode-audio", help="下りTCH→S-Codec→16kHz WAV")
    da.add_argument("--municipal-code", dest="municipal_code", type=int, default=None,
                    help="市区町村コード（例 40225=うきは市, スクランブル値を決定）")
    da.add_argument("--seed", type=int, default=None,
                    help="スクランブル値を直接指定（--municipal-code の代わり）")
    da.add_argument("--offset", type=float, default=None,
                    help="チャネルのベースバンドオフセット [kHz]（例 -83）。省略で自動探索")
    da.add_argument("--max-seconds", dest="max_seconds", type=float, default=None,
                    help="解析する秒数（省略で全体）")
    _add_common(da)
    da.add_argument("--out", default=None, help="出力 WAV（既定 figures/<stem>_audio.wav）")
    da.set_defaults(func=cmd_decode_audio)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
