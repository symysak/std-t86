using System;
using System.Windows.Forms;
using SDRSharp.Common;
using SDRSharp.Radio;

namespace SDRSharp.IqTcpServer
{
    /// <summary>
    /// SDR# の生 I/Q（RawIQ, デバイスサンプルレート）を TCP サーバとして配信するプラグイン。
    /// 流すフォーマットは float32 I,Q インターリーブ = std-t86 デコーダの cf32 そのもの。
    ///
    ///   uv run stdt86 live tcp://127.0.0.1:5555 --fs &lt;SDR# のサンプルレート&gt; --fmt cf32
    ///
    /// SDR# の <c>Complex</c> は {float Real; float Imag;} の 8 バイトで、メモリ配置が
    /// そのまま cf32（I,Q,I,Q…）なので変換は不要。ブロックをバイトコピーして送るだけ。
    /// </summary>
    public unsafe class IqTcpServerPlugin : ISharpPlugin, IIQProcessor
    {
        private ISharpControl _control;
        private IqTcpServerPanel _panel;
        private readonly IqTcpServer _server = new IqTcpServer();
        private double _sampleRate;

        public string DisplayName => "IQ TCP Server (cf32)";
        public bool HasGui => true;
        public UserControl Gui => _panel;

        internal IqTcpServer Server => _server;

        public void Initialize(ISharpControl control)
        {
            _control = control;
            _panel = new IqTcpServerPanel(this);
            // RawIQ = フロントエンド前の生 I/Q（デバイスレート）。チャネルオフセットは
            // デコーダ側の f0 自動探索が吸収するので、SDR# 側で中心を合わせ込む必要はない。
            control.RegisterStreamHook(this, ProcessorType.RawIQ);
        }

        public void Close()
        {
            _server.Stop();
        }

        // --- IIQProcessor ---------------------------------------------------

        // SDR# は Enabled が true のときだけ Process を呼ぶ。サーバ稼働と連動させる。
        public bool Enabled { get; set; }

        public double SampleRate
        {
            get { return _sampleRate; }
            set
            {
                _sampleRate = value;
                if (_panel != null) _panel.SetSampleRate(value);
            }
        }

        /// <summary>
        /// DSP スレッドから呼ばれる。<paramref name="buffer"/> はこの呼び出し中しか有効で
        /// ないので、即座にマネージド配列へコピーしてサーバのキューへ渡す（送信は別スレッド）。
        /// </summary>
        public void Process(Complex* buffer, int length)
        {
            if (!Enabled || length <= 0) return;

            int nbytes = length * 8; // sizeof(Complex) == 8: float Real + float Imag = cf32
            var block = new byte[nbytes];
            fixed (byte* dst = block)
            {
                Buffer.MemoryCopy(buffer, dst, nbytes, nbytes);
            }
            _server.Broadcast(block);
        }
    }
}
