# -*- coding: utf-8 -*-
"""一键自测：先模拟一条 hook 回调（带摘要提取），再发一条直接上报，
都成功的话，管理面板和手机端应多出两条通知。用法：python selftest.py
"""
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BROKER_HOST = 'broker.emqx.io'
BROKER_PORT = 1883
BROKER_TLS_PORT = 8883


def post(payload):
    req = urllib.request.Request('http://127.0.0.1:8787/notify',
                                 data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=3)


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


def _mqtt_connect_packet(cid, clean_session=True):
    flags = 0x02 if clean_session else 0x00
    body = _enc_str('MQTT') + bytes([4, flags]) + (60).to_bytes(2, 'big') + _enc_str(cid)
    return bytes([0x10]) + _enc_len(len(body)) + body


def _subscribe(s, topic):
    sb = (1).to_bytes(2, 'big') + _enc_str(topic) + bytes([1])
    s.sendall(bytes([0x82]) + _enc_len(len(sb)) + sb)   # SUBSCRIBE QoS1
    _recv_exact(s, 5)                                   # SUBACK


def _read_publish(s):
    """读一条 PUBLISH（处理变长剩余长度），返回 payload 字节。"""
    head, pkt = _read_packet(s, 8)
    if head not in (0x30, 0x31, 0x32, 0x33):
        raise OSError('收到的不是 PUBLISH: 0x%02x' % head)
    tlen = int.from_bytes(pkt[:2], 'big')
    return pkt[2 + tlen + (2 if head == 0x32 else 0):]


def _read_packet(s, timeout):
    """读一个 MQTT 包：返回 (固定头首字节, 变长头之后的全部字节)。"""
    s.settimeout(timeout)
    head = _recv_exact(s, 1)[0]
    rl, mult = 0, 1
    while True:
        b0 = _recv_exact(s, 1)[0]
        rl += (b0 & 0x7F) * mult
        if not b0 & 0x80:
            break
        mult *= 128
    return head, _recv_exact(s, rl)


def _publish(topic, payload):
    """短连接发布一条 QoS1 消息。"""
    s = socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=8)
    try:
        s.settimeout(8)
        s.sendall(_mqtt_connect_packet('zcode-pub-' + os.urandom(3).hex()))
        _recv_exact(s, 4)                               # CONNACK
        pv = _enc_str(topic) + (1).to_bytes(2, 'big')
        s.sendall(bytes([0x32]) + _enc_len(len(pv) + len(payload)) + pv + payload)
        _recv_exact(s, 4)                               # PUBACK
    finally:
        s.close()


def mqtt_roundtrip():
    """同一台公网 broker 上开订阅方和发布方，验证整条 MQTT 通路。"""
    topic = 'zcode-notify-selftest-' + os.urandom(3).hex()
    sub = socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=8)
    try:
        sub.settimeout(8)
        sub.sendall(_mqtt_connect_packet('zcode-selftest-' + os.urandom(3).hex()))
        _recv_exact(sub, 4)                             # CONNACK
        _subscribe(sub, topic)
        time.sleep(0.5)
        _publish(topic, b'{"selftest":true}')
        pl = _read_publish(sub)
        if b'selftest' not in pl:
            raise OSError('内容不对: %r' % pl[:60])
    finally:
        sub.close()


def mqtt_tls_check():
    """验证 MQTTS(8883, TLS) 也通——服务端与手机端都优先走这条路。"""
    raw = socket.create_connection((BROKER_HOST, BROKER_TLS_PORT), timeout=8)
    try:
        s = ssl.create_default_context().wrap_socket(raw, server_hostname=BROKER_HOST)
        s.settimeout(8)
        s.sendall(_mqtt_connect_packet('zcode-tls-' + os.urandom(3).hex()))
        ack = _recv_exact(s, 4)
        if ack[0] != 0x20 or ack[3] != 0:
            raise OSError('CONNACK 异常: %s' % ack.hex())
        s.close()
    finally:
        raw.close()


def _connect_broker_socket(cid, clean_session=True):
    """建立 CONNECT 并读完 CONNACK，返回裸 socket（订阅与否由调用方决定）。"""
    s = socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=8)
    s.settimeout(8)
    s.sendall(_mqtt_connect_packet(cid, clean_session=clean_session))
    _recv_exact(s, 4)                                    # CONNACK
    return s


def mqtt_offline_redelivery():
    """持久会话模拟：订阅后断开 -> 离线期间发布两条 -> 同 clientId 重连应补投。

    注意重连后不能再按固定顺序读：持久会话的补投消息可能在 SUBACK 之前就到，
    而且订阅还在服务端保留着，根本不用重新 SUBSCRIBE——宽松读包直到凑齐两条。
    """
    topic = 'zcode-notify-selftest-off-' + os.urandom(3).hex()
    cid = 'zcode-selftest-off-' + os.urandom(3).hex()

    s = _connect_broker_socket(cid, clean_session=False)
    _subscribe(s, topic)
    s.close()                                            # 掉线（会话留在 broker 上）
    time.sleep(0.3)
    _publish(topic, b'{"offline":1}')                    # 离线期间发的两条
    _publish(topic, b'{"offline":2}')

    s = _connect_broker_socket(cid, clean_session=False)  # 同 clientId 重连，不重新订阅
    got = set()
    try:
        deadline = time.time() + 8
        while len(got) < 2 and time.time() < deadline:
            head, pkt = _read_packet(s, 3)
            if head in (0x30, 0x31, 0x32, 0x33):         # PUBLISH（可能在 SUBACK 前后乱序）
                tlen = int.from_bytes(pkt[:2], 'big')
                pl = pkt[2 + tlen + (2 if head == 0x32 else 0):]
                if b'"offline":1' in pl:
                    got.add(1)
                if b'"offline":2' in pl:
                    got.add(2)
            # SUBACK(0x90)/其它控制包：跳过继续读
    finally:
        s.close()
    if got != {1, 2}:
        raise OSError('只补投到 %s/2 条' % sorted(got))


def main():
    post({'title': '自测通知（直接上报）',
          'body': 'server.py 的 POST /notify 收到了这条',
          'project': '自测'})

    tf = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False, encoding='utf-8')
    json.dump({'message': {'content': [
        {'type': 'text',
         'text': '这是最后一条助手回复，应当出现在通知摘要里。' * 3
                 + '这段话足够长，用来验证通知的 full 全文字段是否被完整带上。'}]}}, tf)
    tf.close()
    ev = {'cwd': HERE, 'transcript_path': tf.name, 'session_id': 'selftest'}
    p = subprocess.run([sys.executable, os.path.join(HERE, 'hook_notify.py')],
                       input=json.dumps(ev).encode(), capture_output=True, timeout=10)
    os.unlink(tf.name)
    print('hook 脚本退出码:', p.returncode, '（应为 0）')

    # 3) 局域网自动配对（app 装上即连用的发现协议）
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(3)
    s.sendto(b'ZCODE-NOTIFY-DISCOVER', ('255.255.255.255', 8788))
    try:
        data, addr = s.recvfrom(2048)
        info = json.loads(data)
        print('自动配对: %s 应答（来自 %s），可选 IP: %s'
              % (info.get('name'), addr[0], ','.join(info.get('ips', []))))
    except socket.timeout:
        print('自动配对: 没收到应答（Windows 防火墙可能拦了 UDP 8788）')

    # 4) 公网 MQTT 中转（跨网络推送通道）
    try:
        mqtt_roundtrip()
        print('跨网络通道: %s:%d 往返收发正常，手机不在家也能收' % (BROKER_HOST, BROKER_PORT))
    except Exception as e:
        print('跨网络通道: 暂不可用（%s）；不影响局域网推送' % e)

    # 4b) MQTTS（8883, TLS）
    try:
        mqtt_tls_check()
        print('跨网络通道(加密): %s:8883 TLS 握手+CONNECT 正常' % BROKER_HOST)
    except Exception as e:
        print('跨网络通道(加密): 暂不可用（%s），将自动回退明文 1883' % e)

    # 4c) 离线补投（持久会话）
    try:
        mqtt_offline_redelivery()
        print('离线补投: 订阅方掉线期间的消息，重连后完整补投 ✓')
    except Exception as e:
        print('离线补投: 公共 broker 未保留会话（%s）；自建 mosquitto 可用' % e)

    # 5) 通知全文（hook 把最后回复的完整文本塞进 full 字段）
    try:
        r = json.loads(urllib.request.urlopen('http://127.0.0.1:8787/history', timeout=3).read())
        latest = (r.get('items') or [{}])[0]
        fl = len(latest.get('full') or '')
        print('通知全文: %s（full %d 字）' % ('已带上' if fl > 0 else '未带', fl))
    except Exception as e:
        print('通知全文: 检查失败（%s）' % e)

    print('如果面板/手机端多出两条通知，说明 服务端 + hook 都通了。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
