# -*- coding: utf-8 -*-
"""后台打字进程：读取 pending_type.json 里的手机消息，等 UI 稳定后
   打进 ZCode 桌面输入栏并发送（带截图留证 + 自校验）。
   由 Stop hook 以独立进程拉起，不受 hook 超时限制；失败保留文件跨边界重试。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PENDING = os.path.join(HERE, 'pending_type.json')
SHOT = os.path.join(HERE, 'idle_layout.png')
DEBUG = os.path.join(HERE, 'hook_debug.log')


def log(note):
    try:
        with open(DEBUG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'at': time.time(), 'mode': 'worker',
                                'note': str(note)[:200]}, ensure_ascii=False) + '\n')
    except Exception:
        pass


def main():
    if not os.path.exists(PENDING):
        return 0
    try:
        with open(PENDING, encoding='utf-8') as f:
            text = json.load(f).get('text', '')
    except Exception as e:
        log('pending 读取失败: %s' % e)
        return 0
    if not text.strip():
        os.remove(PENDING)
        return 0

    sys.path.insert(0, HERE)
    import desktop_inject

    time.sleep(1.5)   # 等桌面 UI 从「运行中」切回「空闲」，输入栏恢复
    desktop_inject.capture_window(SHOT)
    log('开始打字（%d 字）' % len(text))
    for attempt in range(3):
        if desktop_inject.type_into_zcode(text, verify=True):
            os.remove(PENDING)
            log('desktop-typed ✓（第 %d 次尝试）' % (attempt + 1))
            return 0
        log('第 %d 次打字未验证成功，重试' % (attempt + 1))
        time.sleep(2)
    log('三次尝试均失败：pending_type.json 保留，下个回合边界重试')
    return 1


if __name__ == '__main__':
    sys.exit(main())
