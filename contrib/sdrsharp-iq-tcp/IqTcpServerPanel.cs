using System;
using System.Drawing;
using System.Windows.Forms;

namespace SDRSharp.IqTcpServer
{
    /// <summary>
    /// SDR# の右ペインに出る最小 UI。ポート指定・開始/停止・状態（接続数・レート）表示のみ。
    /// </summary>
    public class IqTcpServerPanel : UserControl
    {
        private readonly IqTcpServerPlugin _plugin;
        private readonly IqTcpServer _server;

        private readonly NumericUpDown _portBox;
        private readonly Button _toggleButton;
        private readonly Label _statusLabel;
        private readonly Label _rateLabel;

        public IqTcpServerPanel(IqTcpServerPlugin plugin)
        {
            _plugin = plugin;
            _server = plugin.Server;

            var layout = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 2,
                RowCount = 4,
                AutoSize = true,
                Padding = new Padding(4),
            };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));

            layout.Controls.Add(new Label { Text = "Port", AutoSize = true, Anchor = AnchorStyles.Left, TextAlign = ContentAlignment.MiddleLeft }, 0, 0);
            _portBox = new NumericUpDown
            {
                Minimum = 1,
                Maximum = 65535,
                Value = 5555,
                Dock = DockStyle.Fill,
            };
            layout.Controls.Add(_portBox, 1, 0);

            _toggleButton = new Button { Text = "Start server", Dock = DockStyle.Fill };
            _toggleButton.Click += OnToggle;
            layout.Controls.Add(_toggleButton, 0, 1);
            layout.SetColumnSpan(_toggleButton, 2);

            _statusLabel = new Label { Text = "Stopped", AutoSize = true, Anchor = AnchorStyles.Left };
            layout.Controls.Add(_statusLabel, 0, 2);
            layout.SetColumnSpan(_statusLabel, 2);

            _rateLabel = new Label { Text = "Sample rate: (start the radio)", AutoSize = true, Anchor = AnchorStyles.Left };
            layout.Controls.Add(_rateLabel, 0, 3);
            layout.SetColumnSpan(_rateLabel, 2);

            Controls.Add(layout);
            AutoSize = true;

            _server.Changed += OnServerChanged;
            UpdateStatus();
        }

        private void OnToggle(object sender, EventArgs e)
        {
            if (_server.Running)
            {
                _plugin.Enabled = false;   // Process 呼び出しを止める
                _server.Stop();
            }
            else
            {
                _server.Start((int)_portBox.Value);
                _plugin.Enabled = true;    // Process 呼び出しを開始
            }
            UpdateStatus();
        }

        /// <summary>SDR# が SampleRate を設定してきたとき（別スレッドの可能性）に呼ばれる。</summary>
        public void SetSampleRate(double sampleRate)
        {
            RunOnUi(() =>
            {
                _rateLabel.Text = sampleRate > 0
                    ? string.Format("Sample rate: {0:#,0} Hz  →  --fs {0:0}", sampleRate)
                    : "Sample rate: (start the radio)";
            });
        }

        private void OnServerChanged()
        {
            RunOnUi(UpdateStatus);
        }

        private void UpdateStatus()
        {
            bool running = _server.Running;
            _toggleButton.Text = running ? "Stop server" : "Start server";
            _portBox.Enabled = !running;
            _statusLabel.Text = running
                ? string.Format("Listening on 0.0.0.0:{0} — {1} client(s)", _server.Port, _server.ClientCount)
                : "Stopped";
        }

        private void RunOnUi(Action action)
        {
            if (IsDisposed || Disposing) return;
            try
            {
                if (InvokeRequired) BeginInvoke(action);
                else action();
            }
            catch (ObjectDisposedException) { /* ハンドル破棄後: 無視 */ }
            catch (InvalidOperationException) { /* ハンドル未作成: 無視 */ }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                _server.Changed -= OnServerChanged;
                _server.Stop();
            }
            base.Dispose(disposing);
        }
    }
}
