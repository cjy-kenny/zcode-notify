# -*- coding: utf-8 -*-
"""生成应用图标（纯标准库写 PNG）：橙色渐变圆角底 + 白色 Z 字。
产出：web/icons/icon-192.png、icon-512.png（PWA）与
      android/res/mipmap-xxxhdpi/ic_launcher.png（APK 启动器图标）。
"""
import os
import struct
import zlib

SIZES = {'icon-512.png': 512, 'icon-192.png': 192}
BG_TOP = (255, 122, 69)   # #ff7a45
BG_BOT = (255, 99, 53)    # #ff6335
Z_MASK = ['11111',
          '00010',
          '00100',
          '01000',
          '11111']


def draw(n):
    px = []
    r = n * 0.22          # 圆角半径
    cell = n * 0.11       # Z 字块大小（5 格 ≈ 0.55n 宽）
    off = (n - 5 * cell) / 2.0
    for y in range(n):
        row = []
        for x in range(n):
            dx = min(x, n - 1 - x)
            dy = min(y, n - 1 - y)
            if dx < r and dy < r and (r - dx) ** 2 + (r - dy) ** 2 > r * r:
                row.append((0, 0, 0, 0))          # 圆角外透明
                continue
            t = y / float(n - 1)
            bg = tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOT)) + (255,)
            gx = int((x - off) / cell)
            gy = int((y - off) / cell)
            row.append((255, 255, 255, 255)
                        if 0 <= gx < 5 and 0 <= gy < 5 and Z_MASK[gy][gx] == '1' else bg)
        px.append(row)
    return px


def save(path, px, n):
    raw = b''.join(b'\x00' + b''.join(struct.pack('4B', *p) for p in row) for row in px)
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n'
                + chunk(b'IHDR', struct.pack('>IIBBBBB', n, n, 8, 6, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(raw, 9))
                + chunk(b'IEND', b''))


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    out_web = os.path.join(root, 'web', 'icons')
    out_res = os.path.join(root, 'android', 'res', 'mipmap-xxxhdpi')
    os.makedirs(out_web, exist_ok=True)
    os.makedirs(out_res, exist_ok=True)
    for name, n in SIZES.items():
        px = draw(n)
        save(os.path.join(out_web, name), px, n)
        if n == 192:
            save(os.path.join(out_res, 'ic_launcher.png'), px, n)
    print('图标已生成：', ', '.join(SIZES))


if __name__ == '__main__':
    main()
