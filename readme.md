# プロジェクト: STD-T86（市町村デジタル同報通信システム）OSS受信デコーダ

## 使い方

フルパイプライン実装済み: I/Q（録音ファイル or TCP ストリーム）→ 復調 → 制御チャネル復号
→ 通報検出 → S-Codec 音声デコード（PLC 補間つき、実波で可聴）→ ライブ Web モニタ。

### セットアップ
```sh
uv sync --all-groups   # 依存導入
uv run pytest          # 全テスト
uv run ruff check      # lint（--fix で自動修正）
```

### 実録音の処理（RTL-SDR）
60MHz帯はR820T2の通常同調範囲（direct samplingモッド不要）。
```sh
rtl_sdr -f <チャネル中心Hz> -s 1024000 -g <gain> data/capture.cu8
uv run stdt86 spectrum data/capture.cu8 --fs 1024000      # チャネルオフセット f0 を確認
uv run stdt86 demod    data/capture.cu8 --fs 1024000 --f0 <Hz> --symbol-rate 11250
```

### リアルタイム受信 + Webモニタ（`stdt86 live`）
ストリーミングデコーダ（`dsp/stream_*.py`）+ FastAPI/WebSocket のライブモニタ。
制御チャネル状態（市区町村・拡声中・スロット使用・製造者）、通報タイムライン
（0x22 開始 / 0x30 切断）、信号品質（ウォーターフォール/コンスタレーション/
CFO/EVM/CRC率）、デコードログをブラウザでリアルタイム表示し、通報音声の
S-Codec デコードをストリーミング再生（🔊）/ WAV ダウンロードできる。

**チャネルオフセットは固定**（`--offset` 省略時 0 kHz = SDR# 等で同調済みの前提。
ベースバンドにオフセットが残る録音は明示指定）。受信中の搬送波ドリフトは前段の
CFO 追尾が吸収する。`--municipal-code`/`--seed` 省略時はスクランブル値を自動判定
（シンドローム重みで候補絞り込み → ビタビ復号の CRC16/既知種別/製造者スコアで確定。
確信が持てるまでスライディング窓で再試行）。

> 実運用波の S-Codec 伝送路 FEC は §5.4 の印字と 3 点だけ異なる（インターリーブの 2bit 回転、
> CVin の CRC 配置、CRC7 の計算法）。いずれも印字の不自然さ（純ビット置換にならないインター
> リーブ式、次数 7 に対し非標準な x⁹ CRC 等）を素直に正す補正で、**自己検査の CRC7 一致率を
> 適応度関数**に実キャプチャから同定した。同定値は式5.4-7/§5.4 から数式で
> 再構成でき（自治体不変）、`s_codec.transmission_decode_ota` で実波を可聴デコード
> できる。導出の全段は `docs/analysis-notes.md`。§5.4 印字どおりの実装も規格往復テスト用に併存する。

```sh
# rtl_tcp 直結（同調は rtl_tcp 起動時の -f で行う）
rtl_tcp -a 0.0.0.0 -f 58588000 -s 1024000 &
uv run stdt86 live rtltcp://127.0.0.1:1234 --fs 1024000 --freq 58588000
# → http://127.0.0.1:8000/

# SDR# 等の生 I/Q TCP ストリーム（cf32/cu8）。詳細は contrib/sdrsharp-iq-tcp/
uv run stdt86 live tcp://127.0.0.1:5555 --fs 1024000 --fmt cf32

```

実運用波の音声は伝送路 FEC の実波同定（上記 OTA 版）により正常にデコードされる。
CRC7 落ちフレームは PLC（直近正常フレーム反復・長区間ミュート）で補間して
パイプラインは止めず、CRC7 統計を報告し続ける。

> 解析の経緯（同期ワード同定、CAC のパイロット分断、§5.4 OTA 同定など）は
> `docs/analysis-notes.md` を参照。

### 音声コーデック（G.722.1 16kbit/s + S-Codec）
```sh
bash scripts/build_g7221.sh    # 同梱 ITU 配布物をパッチ・ビルド → build/g7221/
pwsh scripts/build_g7221.ps1   # 同上の Windows 版（MinGW-w64 の gcc/clang が必要）
uv run pytest tests/test_g7221.py
```
`codec/g7221.py` が C バイナリをサブプロセス起動する（`STDT86_G7221_DIR` で場所を上書き可。
未ビルドだと音声デコードのみ失敗する）。S-Codec は `codec/s_codec.py`: 規格 §5.2→§5.7 の
全章往復が PCM ビット一致（`tests/test_g7221.py::test_scodec_full_chain_roundtrip`）、
実波は OTA 同定版 `transmission_decode_ota` で復号する。


### 対応I/Q形式
拡張子で自動判別: `.cu8`（rtl_sdr 生 uint8）/ `.cf32`,`.iq`（float32）/ `.wav`（I=L・Q=R、
fs はヘッダから取得）。生形式はサンプルレート `--fs` の指定が必要。