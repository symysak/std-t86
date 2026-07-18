using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Channels;

namespace SDRSharp.IqTcpServer
{
    /// <summary>
    /// 複数クライアントを受け付ける生バイト TCP サーバ。各クライアントは有界キューを持ち、
    /// あふれたら最古ブロックを捨てる（デコーダ側もネットワークソースは lossy=True 前提）。
    /// DSP スレッド（<see cref="Broadcast"/>）はキュー投入のみ。実送信は各クライアントの
    /// 専用スレッドで行い、コールバックをブロックしない。
    /// </summary>
    public sealed class IqTcpServer
    {
        // クライアント毎に貯めるブロック数の上限。160ms 相当のブロックが数十入る想定。
        private const int QueueCapacity = 64;

        private readonly object _sync = new object();
        private readonly List<Client> _clients = new List<Client>();
        private TcpListener _listener;
        private Thread _acceptThread;
        private volatile bool _running;

        public int Port { get; private set; }
        public bool Running => _running;

        public int ClientCount
        {
            get { lock (_sync) return _clients.Count; }
        }

        /// <summary>クライアント数やサーバ状態が変わったとき発火（UI 更新用）。</summary>
        public event Action Changed;

        public void Start(int port)
        {
            Stop();
            _listener = new TcpListener(IPAddress.Any, port);
            _listener.Start();
            Port = port;
            _running = true;
            _acceptThread = new Thread(AcceptLoop) { IsBackground = true, Name = "IqTcp-Accept" };
            _acceptThread.Start();
            RaiseChanged();
        }

        public void Stop()
        {
            if (!_running && _listener == null) return;
            _running = false;
            try { _listener?.Stop(); } catch { /* ignore */ }
            _listener = null;
            lock (_sync)
            {
                foreach (var c in _clients) c.Dispose();
                _clients.Clear();
            }
            RaiseChanged();
        }

        private void AcceptLoop()
        {
            while (_running)
            {
                Socket sock;
                try
                {
                    sock = _listener.AcceptSocket();
                }
                catch
                {
                    break; // listener stopped
                }
                try { sock.NoDelay = true; } catch { /* ignore */ }

                var client = new Client(sock, QueueCapacity, OnClientClosed);
                lock (_sync) _clients.Add(client);
                client.Start();
                RaiseChanged();
            }
        }

        private void OnClientClosed(Client c)
        {
            bool removed;
            lock (_sync) removed = _clients.Remove(c);
            c.Dispose();
            if (removed) RaiseChanged();
        }

        /// <summary>DSP スレッドから 1 ブロック（cf32 バイト列）を全クライアントへ投入。</summary>
        public void Broadcast(byte[] block)
        {
            lock (_sync)
            {
                for (int i = 0; i < _clients.Count; i++)
                    _clients[i].Enqueue(block);
            }
        }

        private void RaiseChanged()
        {
            var h = Changed;
            if (h != null) h();
        }

        // ------------------------------------------------------------------

        private sealed class Client
        {
            private readonly Socket _sock;
            private readonly Channel<byte[]> _channel;
            private readonly Thread _sender;
            private readonly Action<Client> _onClosed;
            private volatile bool _alive = true;

            public Client(Socket sock, int capacity, Action<Client> onClosed)
            {
                _sock = sock;
                _onClosed = onClosed;
                _channel = Channel.CreateBounded<byte[]>(new BoundedChannelOptions(capacity)
                {
                    FullMode = BoundedChannelFullMode.DropOldest, // 実時間: あふれたら最古を捨てる
                    SingleReader = true,
                    SingleWriter = false,
                });
                _sender = new Thread(SendLoop) { IsBackground = true, Name = "IqTcp-Send" };
            }

            public void Start() => _sender.Start();

            public void Enqueue(byte[] block)
            {
                if (_alive) _channel.Writer.TryWrite(block); // 非ブロッキング（満杯なら最古破棄）
            }

            private void SendLoop()
            {
                var reader = _channel.Reader;
                try
                {
                    while (_alive)
                    {
                        byte[] block;
                        if (!reader.TryRead(out block))
                        {
                            // 空: データ到着まで待つ（別スレッドなので同期待ちでよい）
                            if (!reader.WaitToReadAsync().AsTask().GetAwaiter().GetResult())
                                break; // channel completed
                            continue;
                        }
                        int off = 0;
                        while (off < block.Length)
                        {
                            int sent = _sock.Send(block, off, block.Length - off, SocketFlags.None);
                            if (sent <= 0) throw new SocketException((int)SocketError.ConnectionReset);
                            off += sent;
                        }
                    }
                }
                catch
                {
                    // 相手切断・送信エラー: このクライアントを畳む
                }
                finally
                {
                    _onClosed?.Invoke(this);
                }
            }

            public void Dispose()
            {
                if (!_alive) return;
                _alive = false;
                _channel.Writer.TryComplete();
                try { _sock.Shutdown(SocketShutdown.Both); } catch { /* ignore */ }
                try { _sock.Close(); } catch { /* ignore */ }
            }
        }
    }
}
