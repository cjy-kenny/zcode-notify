# -*- coding: utf-8 -*-
"""ZCode hook 全能脚本：开始/完成推送 + 手机消息反向注入 + 桌面打字交接。

由 ZCode 进程调用（config.json 注册 pre/start/stop 三种模式），stdin 是事件 JSON。
通知服务没开就静默跳过——hook 绝不能阻塞或报错拖慢会话。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

PORT = 8787
HERE = os.path.dirname(os.path.abspath(__file__))
PENDING = os.path.join(HERE, 'pending_type.json')
HOOK_DEBUG = os.path.join(HERE, 'hook_debug.log')


def debug_log(mode, sid, note):
    try:
        with open(HOOK_DEBUG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'at': time.time(), 'mode': mode, 'sid': sid[:24],
                                'note': str(note)[:200]}, ensure_ascii=False) + '\n')
    except Exception:
        pass


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


def see_session(ev, cwd, busy=None):
    """向服务器上报本会话（sid + 项目名 + 忙碌状态）。"""
    sid = str(ev.get('session_id') or ev.get('sessionId') or '')[:64]
    if not sid:
        return
    try:
        payload = {'sid': sid, 'cwd': cwd, 'label': os.path.basename(cwd) or 'ZCode'}
        if busy is not None:
            payload['busy'] = bool(busy)
        req = urllib.request.Request('http://127.0.0.1:%d/session-see' % PORT,
                                     data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        pass


def take_inbox(current_sid):
    """向服务器取走投递给本会话（或未指定目标）的手机消息。

    走 HTTP 而不是读文件：hook 脚本可能存在多份副本（工作区/生产），
    但服务器和收件箱只有一份——以服务器为唯一事实源，杜绝路径错位。
    """
    try:
        url = 'http://127.0.0.1:%d/inbox-take?sid=%s' % (
            PORT, urllib.parse.quote(current_sid or ''))
        with urllib.request.urlopen(url, timeout=3) as r:
            box = json.load(r).get('items') or []
    except Exception:
        debug_log('take', current_sid, 'inbox-take failed')
        return None
    if not box:
        return None
    try:   # 投递流水：万一输出格式不被支持，消息还能找回
        with open(os.path.join(HERE, 'delivered.log'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'at': time.time(), 'sid': current_sid, 'items': box},
                               ensure_ascii=False) + '\n')
    except Exception:
        pass
    return box


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
    sid = str(ev.get('session_id') or ev.get('sessionId') or '')[:64]
    debug_log(mode, sid, 'fired')

    if mode == 'pre':
        # 回合进行中：每次工具调用前注入手机消息（additionalContext，不影响工具本身）
        see_session(ev, cwd, busy=True)
        box = take_inbox(sid)
        if box:
            lines = ['%d. %s' % (i, str(m.get('text', '')).strip()[:200])
                     for i, m in enumerate(box[:10], 1)]
            ctx = ('【手机消息】手机端发来 %d 条新消息，请当作新的用户指令处理：\n%s\n'
                   '（完成当前工作后正常结束即可，手机端会收到完成通知）'
                   % (len(box), '\n'.join(lines)))
            debug_log('pre', sid, 'deliver %d' % len(box))
            sys.stdout.write(json.dumps({'hookEventName': 'PreToolUse',
                                         'additionalContext': ctx}, ensure_ascii=False))
        else:
            debug_log('pre', sid, 'empty')
        return 0

    if mode == 'start':
        # 回合开始：标记忙碌 + 推「收到新任务」
        see_session(ev, cwd, busy=True)
        prompt = str(ev.get('prompt') or ev.get('user_prompt') or '').replace('\n', ' ').strip()
        title = 'ZCode 收到新任务'
        body = '「%s」开始处理' % project
        if prompt:
            body += '：' + (prompt[:60] + '…' if len(prompt) > 60 else prompt)
        full = prompt[:1500]

    else:   # stop / session_start（flush）
        see_session(ev, cwd, busy=False)
        # 反向通道：收件箱有手机消息，或上次的桌面打字还没成功 → 交给后台打字进程
        box = take_inbox(sid)
        pending_text = ''
        if box:
            pending_text = '\n'.join(str(m.get('text', '')).strip()[:200] for m in box[:10])
        else:
            try:
                with open(PENDING, encoding='utf-8') as f:
                    pending_text = str(json.load(f).get('text') or '').strip()
            except Exception:
                pending_text = ''
        if pending_text:
            # 交接给独立后台打字进程（不受 hook 超时限制；失败保留文件跨边界重试）。
            # 打字成功 → 消息作为新任务提交 → 新回合的 hook 会发「收到新任务」。
            debug_log('stop', sid, 'deliver，交给打字进程（%d 字）' % len(pending_text))
            try:
                with open(PENDING, 'w', encoding='utf-8') as f:
                    json.dump({'text': pending_text}, f, ensure_ascii=False)
                import subprocess
                subprocess.Popen(
                    [sys.executable, os.path.join(HERE, 'desktop_type_worker.py')],
                    cwd=HERE, creationflags=0x00000008)
                debug_log('stop', sid, 'worker-spawned')
            except Exception as e:
                debug_log('stop', sid, 'worker-spawn 失败: %s' % e)
            return 0   # 消息经打字进程提交为新任务，不走注入

        # 无待办：正常的完成通知（摘要 + 全文）
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
