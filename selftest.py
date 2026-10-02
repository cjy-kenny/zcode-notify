# -*- coding: utf-8 -*-
"""一键自测：先模拟一条 hook 回调（带摘要提取），再发一条直接上报，
都成功的话，管理面板和手机端应多出两条通知。用法：python selftest.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BROKER_HOST = 'broker.emqx.io'
BROKER_PORT = 1883


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


def _mqtt_connect_packet(cid):
    body = _enc_str('MQTT') + bytes([4, 0x02]) + (60).to_bytes(2, 'big') + _enc_str(cid)
    return bytes([0x10]) + _enc_len(len(body)) + body


def mqtt_roundtrip():
    """同一台公网 broker 上开订阅方和发布方，验证整条 MQTT 通路。"""
    topic = 'zcode-notify-selftest-' + os.urandom(3).hex()
    sub = socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=8)
    try:
        sub.settimeout(8)
        sub.sendall(_mqtt_connect_packet('zcode-selftest-' + os.urandom(3).hex()))
        _recv_exact(sub, 4)                                  # CONNACK
        sb = (1).to_bytes(2, 'big') + _enc_str(topic) + bytes([1])
        sub.sendall(bytes([0x82]) + _enc_len(len(sb)) + sb)  # SUBSCRIBE QoS1
        _recv_exact(sub, 5)                                  # SUBACK
        time.sleep(0.5)
        pub = socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=8)
        try:
            pub.settimeout(8)
            pub.sendall(_mqtt_connect_packet('zcode-selftest-pub'))
            _recv_exact(pub, 4)                              # CONNACK
            pv = _enc_str(topic) + (1).to_bytes(2, 'big')
            payload = b'{"selftest":true}'
            pub.sendall(bytes([0x32]) + _enc_len(len(pv) + len(payload)) + pv + payload)
            _recv_exact(pub, 4)                              # PUBACK
        finally:
            pub.close()
        head = _recv_exact(sub, 1)[0]
        if head not in (0x30, 0x31, 0x32, 0x33):
            raise OSError('收到的不是 PUBLISH: 0x%02x' % head)
        rl, mult = 0, 1
        while True:
            b0 = _recv_exact(sub, 1)[0]
            rl += (b0 & 0x7F) * mult
            if not b0 & 0x80:
                break
            mult *= 128
        _recv_exact(sub, rl)
    finally:
        sub.close()


def main():
    post({'title': '自测通知（直接上报）',
          'body': 'server.py 的 POST /notify 收到了这条',
          'project': '自测'})

    tf = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False, encoding='utf-8')
    json.dump({'message': {'content': [
        {'type': 'text', 'text': '这是最后一条助手回复，应当出现在通知摘要里'}]}}, tf)
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

    print('如果面板/手机端多出两条通知，说明 服务端 + hook 都通了。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
