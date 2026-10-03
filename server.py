# -*- coding: utf-8 -*-
"""ZCode 任务通知 —— 服务端

接收 ZCode Stop hook 的完成回调，通过 SSE 实时推送到手机端 app 和管理面板。
零依赖：纯 Python 标准库。启动：python server.py（或双击 启动通知服务.bat）

接口一览：
  GET  /          管理面板（电脑浏览器打开）
  GET  /phone     手机端通知页（手机浏览器/app 打开）
  GET  /events    SSE 实时推送流（token 只用于写接口鉴权，读取不设限）
  GET  /history   最近通知列表（JSON）
  GET  /status    服务状态（客户端数、hook 配置检测等）
  POST /notify    上报一条通知（本机回环免 token，局域网需 ?token=）
  POST /config    设置远程页面链接 remote_url（同样鉴权，存 state.json）
"""
import json
import os
import queue
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, 'web')
STATE = os.path.join(ROOT, 'state.json')
INBOX = os.path.join(ROOT, 'inbox.json')   # 手机发来的消息，等 Stop hook 取走注入会话
SESSIONS = os.path.join(ROOT, 'sessions.json')   # hook 上报的会话注册表（手机选话题用）
PORT = 8787
PAGE_VERSION = '0.14'   # 注入手机页页脚，用户一眼确认拿到的是不是最新页面
MAX_HISTORY = 200

_lock = threading.Lock()
_clients = []            # 每个 SSE 客户端一个 Queue
_history = []            # [{id,title,body,project,ts,link}]
_token = ''              # 写接口（局域网 POST /notify）的口令，首次启动生成
_remote_url = ''         # zcode 远程页面链接：每条通知带上，手机点卡片直达输入框

BROKER_HOST = 'broker.emqx.io'   # 公网 MQTT 中转（跨网络推送）；可改成自建 mosquitto 地址
BROKER_TLS_PORT = 8883           # MQTTS 优先，连不上自动回退 1883 明文
BROKER_PORT = 1883
_broker_mode = ['tls']           # 记住上次成功的连接方式，避免每次都白等一次回退
_mqtt_topic = ''                 # 订阅码：手机端凭它订阅公网通道，随机生成即机密
_mqtt_q = queue.Queue(maxsize=100)


# ---------------------------------------------------------------- 状态与推送

def load_state():
    global _token, _history, _mqtt_topic, _remote_url
    try:
        with open(STATE, encoding='utf-8') as f:
            st = json.load(f)
        _token = st.get('token', '')
        _history = st.get('history', [])
        _mqtt_topic = st.get('mqtt_topic', '')
        _remote_url = st.get('remote_url', '')
    except Exception:
        _token, _history, _mqtt_topic, _remote_url = '', [], '', ''
    if not _token:
        _token = os.urandom(8).hex()
        save_state()
    if not _mqtt_topic:
        _mqtt_topic = os.urandom(16).hex()
        save_state()


def save_state():
    try:
        with open(STATE, 'w', encoding='utf-8') as f:
            json.dump({'token': _token, 'mqtt_topic': _mqtt_topic,
                       'remote_url': _remote_url,
                       'history': _history[-MAX_HISTORY:]},
                      f, ensure_ascii=False, indent=1)
    except Exception as e:
        print('state.json 写入失败:', e)


def load_inbox():
    try:
        with open(INBOX, encoding='utf-8') as f:
            box = json.load(f)
        return box if isinstance(box, list) else []
    except Exception:
        return []


def save_inbox(box):
    try:
        tmp = INBOX + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(box[-20:], f, ensure_ascii=False, indent=1)
        os.replace(tmp, INBOX)   # 原子替换：hook 并发读永远读不到半截 JSON
    except Exception as e:
        print('inbox 写入失败:', e)


def load_sessions():
    try:
        with open(SESSIONS, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_sessions(d):
    try:
        with open(SESSIONS, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print('sessions 写入失败:', e)


def broadcast(item):
    with _lock:
        _history.append(item)
        del _history[:-MAX_HISTORY]
        dead = []
        for q in _clients:
            try:
                q.put_nowait(item)
            except Exception:
                dead.append(q)
        for q in dead:
            _clients.remove(q)
    save_state()
    if item.get('kind') == 'sent':
        return   # 自己发的消息只进页面气泡，不发系统通知、不走公网
    try:  # 跨网络通道：手机不在同一 Wi-Fi 时也能收到
        _mqtt_q.put_nowait(json.dumps(item, ensure_ascii=False))
    except Exception:
        pass


# ---------------------------------------------------------------- 环境检测

def detect_ip():
    """取本机局域网 IP（UDP connect 只选路由不发包）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith('127.'):
            return ip
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith('127.'):
                return ip
    except Exception:
        pass
    return '127.0.0.1'


def all_ips():
    ips = []
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = info[4][0]
        if not ip.startswith('127.') and ip not in ips:
            ips.append(ip)
    primary = detect_ip()
    if primary in ips:
        ips.remove(primary)
    return [primary] + ips


def hook_configured():
    """检测 ZCode 配置里是否注册了本项目的 hook 脚本。"""
    script = os.path.join(ROOT, 'hook_notify.py')
    for path in [os.path.expanduser('~/.zcode/cli/config.json'),
                 os.path.join(ROOT, '..', '.zcode', 'config.json')]:
        try:
            with open(path, encoding='utf-8') as f:
                if 'hook_notify.py' in f.read() and os.path.exists(script):
                    return True
        except Exception:
            continue
    return False


# ------------------------------------------------- UDP 自动配对（装上即连）

def discovery_responder():
    """UDP :8788 应答手机 app 的「自动搜索」广播。

    手机 app 发一条 ZCODE-NOTIFY-DISCOVER 广播，本机回 name + 可用 IP + 端口，
    app 探测 /status 通了就自动连上。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.bind(('', 8788))
    except OSError as e:
        print('自动配对端口 8788 被占用（%s），app 内手动粘贴地址仍可用。' % e)
        return
    while True:
        try:
            data, addr = s.recvfrom(2048)
        except OSError:
            continue
        if data.startswith(b'ZCODE-NOTIFY-DISCOVER'):
            payload = json.dumps({'name': socket.gethostname(), 'port': PORT,
                                  'ips': all_ips()}).encode('utf-8')
            try:
                s.sendto(payload, addr)
            except OSError:
                pass


# ------------------------------------------------- 公网 MQTT 中转（跨网络推送）

def _enc_str(s):
    b = s.encode('utf-8')
    return len(b).to_bytes(2, 'big') + b


def _enc_len(n):
    out = bytearray()
    while True:
        d = n % 128
        n //= 128
        if n:
            d |= 0x80
        out.append(d)
        if not n:
            return bytes(out)


def _recv_exact(s, n):
    buf = b''
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise OSError('对端关闭连接')
        buf += c
    return buf


def _connect_broker(timeout=5):
    """连 broker：MQTTS(8883) 优先，失败自动回退明文(1883)。"""
    if _broker_mode[0] == 'tls':
        try:
            raw = socket.create_connection((BROKER_HOST, BROKER_TLS_PORT), timeout=timeout)
            s = ssl.create_default_context().wrap_socket(raw, server_hostname=BROKER_HOST)
            s.settimeout(timeout)
            return s
        except Exception:
            _broker_mode[0] = 'plain'
    s = socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=timeout)
    s.settimeout(timeout)
    return s


def mqtt_publish_once(topic, payload):
    """极简 MQTT 3.1.1（纯标准库）：CONNECT → PUBLISH(QoS1) → 等 PUBACK → 断开。

    每条通知一条短连接：量小无所谓，胜在零依赖、无状态、不怕断。
    """
    body = (_enc_str('MQTT') + bytes([4, 0x02]) + (60).to_bytes(2, 'big')
            + _enc_str('zcode-server-' + os.urandom(4).hex()))
    connect = bytes([0x10]) + _enc_len(len(body)) + body
    pub_vh = _enc_str(topic) + (1).to_bytes(2, 'big')
    pub = bytes([0x32]) + _enc_len(len(pub_vh) + len(payload)) + pub_vh + payload

    s = _connect_broker()
    try:
        s.sendall(connect)
        ack = _recv_exact(s, 4)
        if ack[0] != 0x20 or ack[3] != 0:
            raise OSError('CONNACK 异常: %s' % ack.hex())
        s.sendall(pub)
        pa = _recv_exact(s, 4)
        if pa[0] != 0x40:
            raise OSError('PUBACK 异常: %s' % pa.hex())
    finally:
        s.close()


def mqtt_publisher():
    while True:
        payload = _mqtt_q.get()
        try:
            mqtt_publish_once('zcode-notify/' + _mqtt_topic, payload.encode('utf-8'))
        except Exception:
            pass  # 公网不通就算了：局域网 SSE 还在；下条通知再试


# ---------------------------------------------------------------- HTTP

MIME = {'.html': 'text/html; charset=utf-8', '.json': 'application/json; charset=utf-8',
        '.js': 'text/javascript; charset=utf-8', '.png': 'image/png', '.ico': 'image/x-icon'}


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # 关掉默认访问日志，只打关键事件

    # ---- 基础工具 ----

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _html(self, code, filename):
        try:
            with open(os.path.join(WEB, filename), 'rb') as f:
                data = f.read()
            data = data.replace(b'__TOKEN__', _token.encode())
            data = data.replace(b'__PAGE_VERSION__', PAGE_VERSION.encode())
            self._send(code, 'text/html; charset=utf-8', data)
        except Exception as e:
            self._send(500, 'text/plain; charset=utf-8', ('页面缺失: %s' % e).encode('utf-8'))

    def _file(self, filename):
        path = os.path.normpath(os.path.join(WEB, filename))
        if not path.startswith(WEB) or not os.path.isfile(path):
            return self._send(404, 'text/plain', b'not found')
        ext = os.path.splitext(filename)[1]
        with open(path, 'rb') as f:
            self._send(200, MIME.get(ext, 'application/octet-stream'), f.read())

    def _token_ok(self):
        q = urllib.parse.urlparse(self.path).query
        tok = urllib.parse.parse_qs(q).get('token', [''])[0]
        return tok == _token

    def _loopback(self):
        return self.client_address[0] in ('127.0.0.1', '::1')

    # ---- GET ----

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/':
            return self._html(200, 'dashboard.html')
        if path == '/phone':
            return self._html(200, 'phone.html')
        if path == '/manifest.json':
            return self._file('manifest.json')
        if path == '/qr.min.js':
            return self._file('qr.min.js')
        if path == '/jsqr.min.js':
            return self._file('jsqr.min.js')
        if path in ('/icons/icon-192.png', '/icons/icon-512.png'):
            return self._file(path.lstrip('/'))
        if path == '/inbox-take':
            # hook 取走手机消息：sid 空=投递所有未指定目标的消息，指定则精确匹配。
            # 收件箱文件只有服务器一份，hook 一律走这里取——脚本副本可以有多份，
            # 真相只有一份。
            if not (self._loopback() or self._token_ok()):
                return self._send(403, 'text/plain', b'forbidden')
            q = urllib.parse.urlparse(self.path).query
            sid = urllib.parse.parse_qs(q).get('sid', [''])[0][:64]
            with _lock:
                box = load_inbox()
                now = time.time()
                deliver = [m for m in box
                           if not str(m.get('sid') or '') or str(m.get('sid')) == sid]
                keep = [m for m in box if m not in deliver
                        and now - float(m.get('ts') or 0) < 86400]
                save_inbox(keep)
            return self._send(200, 'application/json; charset=utf-8',
                              json.dumps({'items': deliver}, ensure_ascii=False).encode('utf-8'))
        if path == '/events':
            return self._events()
        if path == '/sessions':
            # 话题列表：hook 上报过的会话，按最近活跃排序
            if not (self._loopback() or self._token_ok()):
                return self._send(403, 'text/plain', b'forbidden')
            with _lock:
                d = load_sessions()
            items = [{'sid': k, 'label': v.get('label', ''), 'last': v.get('last', 0)}
                     for k, v in d.items()]
            items.sort(key=lambda x: -x['last'])
            return self._send(200, 'application/json; charset=utf-8',
                              json.dumps({'items': items}, ensure_ascii=False).encode('utf-8'))
        if path == '/history':
            with _lock:
                items = list(reversed(_history[-50:]))
            return self._send(200, 'application/json; charset=utf-8',
                              json.dumps({'items': items}, ensure_ascii=False).encode('utf-8'))
        if path == '/status':
            st = {'clients': len(_clients), 'total': len(_history),
                  'ip': detect_ip(), 'ips': all_ips(), 'port': PORT,
                  'token': _token, 'mqtt': _mqtt_topic, 'hook': hook_configured(),
                  'remote_url': _remote_url}
            return self._send(200, 'application/json; charset=utf-8',
                              json.dumps(st, ensure_ascii=False).encode('utf-8'))
        self._send(404, 'text/plain', b'not found')

    # ---- SSE ----

    def _events(self):
        if not (self._loopback() or self._token_ok()):
            return self._send(403, 'text/plain; charset=utf-8', '口令不对'.encode('utf-8'))
        q = queue.Queue()
        with _lock:
            _clients.append(q)
        print('[推送] 新客户端接入 %s（当前 %d 个）' % (self.client_address[0], len(_clients)), flush=True)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        try:
            while True:
                try:
                    item = q.get(timeout=15)
                    data = json.dumps(item, ensure_ascii=False)
                    self.wfile.write(('event: notify\ndata: ' + data + '\n\n').encode('utf-8'))
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')   # 心跳，防中间设备断空闲连接
                self.wfile.flush()
        except Exception:
            pass
        finally:
            with _lock:
                if q in _clients:
                    _clients.remove(q)
            print('[推送] 客户端断开 %s（剩余 %d 个）' % (self.client_address[0], len(_clients)), flush=True)

    # ---- POST ----

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/config':
            return self._config()
        if path == '/send':
            # 反向通道：手机 → 服务器收件箱 → Stop hook 注入 ZCode 会话
            if not (self._loopback() or self._token_ok()):
                return self._send(403, 'text/plain; charset=utf-8', '口令不对'.encode('utf-8'))
            try:
                length = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            text = str(data.get('text') or '').strip()[:500]
            if not text:
                return self._send(400, 'text/plain; charset=utf-8', 'text 必填'.encode('utf-8'))
            sid = str(data.get('sid') or '')[:64]   # 空 = 自动（最新活跃会话）
            with _lock:
                box = load_inbox()
                box.append({'text': text, 'ts': time.time(),
                            'from': self.client_address[0], 'sid': sid})
                save_inbox(box)
            print('[手机消息] %s' % text[:80], flush=True)
            broadcast({'id': os.urandom(4).hex(), 'title': '📱 手机消息',
                       'body': text, 'kind': 'sent', 'sid': sid, 'ts': time.time()})
            return self._send(200, 'application/json; charset=utf-8',
                              json.dumps({'ok': True, 'via': 'inbox', 'count': len(box)},
                                         ensure_ascii=False).encode('utf-8'))
        if path == '/session-see':
            # hook 上报会话（sid + 项目名），手机端「选择话题」的数据源
            if not self._loopback():
                return self._send(403, 'text/plain', b'forbidden')
            try:
                length = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except Exception:
                data = {}
            sid = str(data.get('sid') or '')[:64] if isinstance(data, dict) else ''
            if sid:
                with _lock:
                    d = load_sessions()
                    d[sid] = {'label': str(data.get('label') or '')[:40],
                              'cwd': str(data.get('cwd') or '')[:200],
                              'last': time.time()}
                    cutoff = time.time() - 7 * 86400
                    d = {k: v for k, v in d.items() if v.get('last', 0) > cutoff}
                    save_sessions(d)
            self.send_response(204)
            self.end_headers()
            return
        if path == '/scanlog':
            # 扫码调试：手机端每一步记一笔（scan_debug.log），盲修变精修
            if not (self._loopback() or self._token_ok()):
                return self._send(403, 'text/plain', b'forbidden')
            try:
                length = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except Exception:
                data = {}
            line = json.dumps({'ip': self.client_address[0], 'at': time.time(),
                               'step': str(data.get('step'))[:40],
                               'info': str(data.get('info'))[:200]}, ensure_ascii=False)
            try:
                with open(os.path.join(ROOT, 'scan_debug.log'), 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except Exception:
                pass
            self.send_response(204)
            self.end_headers()
            return
        if path != '/notify':
            return self._send(404, 'text/plain', b'not found')
        if not (self._loopback() or self._token_ok()):
            return self._send(403, 'text/plain; charset=utf-8', '口令不对'.encode('utf-8'))
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        item = {
            'id': os.urandom(4).hex(),
            'title': str(data.get('title') or 'ZCode 通知')[:80],
            'body': str(data.get('body') or '')[:500],
            'full': str(data.get('full') or '')[:2000],   # 完整文本，手机卡片点开看
            'project': str(data.get('project') or '')[:40],
            'ts': time.time(),
        }
        link = str(data.get('link') or _remote_url)       # 请求未指定时用全局远程页面链接
        if link:
            item['link'] = link[:500]
        broadcast(item)
        print('[通知] %s | %s' % (item['title'], item['body'][:50]), flush=True)
        self.send_response(204)
        self.end_headers()

    def _config(self):
        """设置远程页面链接（本机回环免 token，局域网需 ?token=）。"""
        if not (self._loopback() or self._token_ok()):
            return self._send(403, 'text/plain; charset=utf-8', '口令不对'.encode('utf-8'))
        global _remote_url
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
        except Exception:
            data = {}
        _remote_url = str(data.get('remote_url') or '')[:500] if isinstance(data, dict) else ''
        save_state()
        return self._send(200, 'application/json; charset=utf-8',
                          json.dumps({'remote_url': _remote_url}, ensure_ascii=False).encode('utf-8'))


def main():
    load_state()
    ip = detect_ip()
    threading.Thread(target=discovery_responder, daemon=True).start()
    threading.Thread(target=mqtt_publisher, daemon=True).start()
    try:
        httpd = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    except OSError:
        print('启动失败：端口 %d 被占用（可能已有一个通知服务在跑）。' % PORT)
        return 1
    print('=' * 54, flush=True)
    print('  ZCode 任务通知服务已启动', flush=True)
    print('  管理面板  :  http://%s:%d/' % (ip, PORT), flush=True)
    print('  手机端地址:  http://%s:%d/phone' % (ip, PORT), flush=True)
    print('  自动配对:  手机 app 打开即自动连接（同一 Wi-Fi，UDP 8788）', flush=True)
    print('  跨网络推送: 经 %s 中转，手机走流量/不在家也能收' % BROKER_HOST, flush=True)
    print('  （IP 变了以面板显示为准）', flush=True)
    print('=' * 54, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
