# SDR# IQ TCP Server プラグイン

SDR# の生 I/Q（RawIQ）を **TCP サーバ**として配信し、std-t86 デコーダから
`tcp://` ソースで受けるためのプラグイン。デコーダは TCP クライアントとして接続しにくる側なので、
SDR# が待ち受けサーバになる。

配信フォーマットは **cf32（float32 の I,Q インターリーブ）**。SDR# の `Complex` は
`{float Real; float Imag;}` の 8 バイトで、メモリ配置がそのまま cf32 なので変換ゼロ・
バイトコピーのみで送出する。

対象は Airspy の **.NET 版 SDR#**。公式プラグイン SDK に合わせて `net9.0-windows` で
ビルドするので、**`SDRSharp.dotnet9.exe`（.NET 9 ホスト）で起動**すること。実 API
（`ISharpPlugin.Gui`、`ISharpControl.RegisterStreamHook(object, ProcessorType)`、
`IIQProcessor.Process(Complex*, int)`）に対しビルド検証済み（0 警告・0 エラー）。

## ビルド（ワンステップ）

`.NET 9 SDK` が必要（未導入なら `winget install Microsoft.DotNet.SDK.9`）。あとは:

```powershell
pwsh ./build.ps1
```

`build.ps1` が自動で:
1. SDR# プラグイン SDK zip（`https://airspy.com/?ddownload=5944`）をダウンロード（`.sdk-cache/` にキャッシュ）
2. 中の `SDRSharp.Common.dll` / `SDRSharp.Radio.dll` を `refs/` へ展開
3. `dotnet build -c Release` でプラグインをビルド

出力: `bin/Release/net9.0-windows/SDRSharp.IqTcpServer.dll`

オプション:
```powershell
pwsh ./build.ps1 -Force                 # SDK を再ダウンロードして refs を更新
pwsh ./build.ps1 -Configuration Debug
# 手元に SDK の lib フォルダがあるなら DL を省略:
dotnet build -c Release -p:RefsDir="C:\path\to\sdrplugins\lib"
```

## インストール

この SDR# は `Plugins\` フォルダを自動スキャンする（`SDRSharp.config` の
`core.pluginsDirectory=Plugins`）。**`Plugins.xml` への登録は不要。**

1. `SDRSharp.IqTcpServer.dll` を SDR# の `Plugins\` フォルダへコピー。
2. **`SDRSharp.dotnet9.exe`** を起動すると右ペインに **"IQ TCP Server (cf32)"** パネルが出る。

## 使い方

1. SDR# でデバイスを選び、対象チャネルへ同調して再生（▶）。
   - サンプルレートはデバイスのレート（例 1.024 MS/s）。パネルに `--fs` 用の値が出る。
   - チャネルの微妙なオフセットはデコーダ側の CFO 追尾が吸収する。
2. パネルで **Port**（既定 5555）を決めて **Start server**。
3. デコーダを接続:

   ```sh
   uv run stdt86 live tcp://127.0.0.1:5555 --fs 1024000 --fmt cf32
   # 別ホストの SDR# なら 127.0.0.1 をそのマシンの IP に
   ```

パネルには待ち受けアドレスと接続クライアント数が出る。処理が追いつかない時は
最古ブロックから捨てる（実時間ソースは取りこぼし前提）。

## 構成

| ファイル | 役割 |
|---|---|
| `build.ps1` | SDK 自動 DL → `refs/` 展開 → ビルド |
| `IqTcpServerPlugin.cs` | `ISharpPlugin` + `IIQProcessor`。RawIQ フックを登録し、ブロックをサーバへ渡す |
| `IqTcpServer.cs` | マルチクライアント TCP サーバ。クライアント毎に有界キュー + 送信スレッド（あふれたら最古破棄） |
| `IqTcpServerPanel.cs` | 最小 WinForms UI（ポート・開始/停止・状態） |
| `refs/` | SDK から展開した参照 DLL（gitignore 済み） |

## 注意

- `System.Threading.Channels`（`BoundedChannelFullMode.DropOldest`）を使う。
- ネットワーク越しに生 I/Q を流すため帯域を食う: cf32 は 8 byte/sample、1.024 MS/s で ≈8 MB/s。
  ローカルループバックなら問題ないが、無線 LAN 越しは注意。
- 旧 .NET Framework 版 SDR# 用の TcpServer プラグイン（`.NETFramework v4.6`）は
  .NET 9 版ホストに load できない。これはその置き換え。
- SDR# を `SDRSharp.dotnet8.exe`（.NET 8 ホスト）で使いたい場合は、csproj の
  `<TargetFramework>` を `net8.0-windows` に変えて `-p:RefsDir` に .NET 8 版の
  参照 DLL を渡す（net9 ビルドは .NET 8 ホストに load できない）。
