# -*- coding: utf-8 -*-
"""ZCode Stop hook：会话回合结束时，把「任务完成」推送到手机。

由 ZCode 进程调用，stdin 是事件 JSON（cwd / transcript_path 等，尽力解析）。
通知服务没开就静默跳过——hook 绝不能阻塞或报错拖慢会话。
"""
import json
import os
import sys
import urllib.request

PORT = 8787


def last_text(transcript_path, cap=1500):
    """取会话转录里最后一段助手回复的全文（截 cap 字）；读不到返回空。"""
    try:
        with open(transcript_path, encoding='utf-8') as f:
            lines = f.readlines()
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            msg = obj.get('message') or {}
            content = msg.get('content')
            if isinstance(content, list):
                texts = [c.get('text', '') for c in content
                         if isinstance(c, dict) and c.get('type') == 'text']
            elif isinstance(content, str):
                texts = [content]
            else:
                continue
            text = ' '.join(t for t in texts if t).strip()
            if text:
                return text.replace('\n', ' ')[:cap]
    except Exception:
        pass
    return ''


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'stop'
    try:
        ev = json.load(sys.stdin)
    except Exception:
        ev = {}
    if not isinstance(ev, dict):
        ev = {}
    cwd = ev.get('cwd') or os.environ.get('ZCODE_PROJECT_DIR') or os.getcwd()
    project = os.path.basename(str(cwd).rstrip('\\/')) or 'ZCode'
    if project.lower() in ('default', 'workspace', 'zcode'):
        project = 'ZCode'   # 默认工作区名没信息量，统一显示 ZCode
    if mode == 'start':
        prompt = str(ev.get('prompt') or ev.get('user_prompt') or '').replace('\n', ' ').strip()
        title = 'ZCode 收到新任务'
        body = '「%s」开始处理' % project
        if prompt:
            body += '：' + (prompt[:60] + '…' if len(prompt) > 60 else prompt)
        full = prompt[:1500]
    else:
        text = last_text(ev.get('transcript_path') or ev.get('transcriptPath') or '')
        title = 'ZCode 任务完成'
        body = '「%s」执行完毕' % project
        if text:
            body += '：' + (text[:60] + '…' if len(text) > 60 else text)
        full = text
    payload = json.dumps({'title': title, 'body': body, 'full': full, 'project': project},
                         ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:%d/notify' % PORT, data=payload,
                                 headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # 通知服务未启动：静默，不影响会话
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # hook 的任何异常都不允许影响会话本身
