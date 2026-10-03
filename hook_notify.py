# -*- coding: utf-8 -*-
"""ZCode Stop hook：会话回合结束时，把「任务完成」推送到手机。

由 ZCode 进程调用，stdin 是事件 JSON（cwd / transcript_path 等，尽力解析）。
通知服务没开就静默跳过——hook 绝不能阻塞或报错拖慢会话。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

PORT = 8787
HOOK_DEBUG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hook_debug.log')


def debug_log(mode, sid, note):
    try:
        with open(HOOK_DEBUG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'at': time.time(), 'mode': mode, 'sid': sid[:24],
                                'note': str(note)[:200]}, ensure_ascii=False) + '\n')
    except Exception:
        pass


def _write_inbox(box):
    try:
        with open(INBOX, 'w', encoding='utf-8') as f:
            json.dump(box, f, ensure_ascii=False, indent=1)
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
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'delivered.log'),
                  'a', encoding='utf-8') as f:
            f.write(json.dumps({'at': time.time(), 'sid': current_sid, 'items': box},
                               ensure_ascii=False) + '\n')
    except Exception:
        pass
    return box


def see_session(ev, cwd, busy=None):
    """向服务器上报本会话（sid + 项目名 + 忙碌状态），手机端「选择话题」的数据源。"""
    sid = str(ev.get('session_id') or ev.get('sessionId') or '')[:64]
    if not sid:
        return
    try:
        payload = {'sid': sid, 'cwd': cwd}
        if busy is not None:
            payload['busy'] = bool(busy)
        req = urllib.request.Request('http://127.0.0.1:%d/session-see' % PORT,
                                     data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=1)
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


def type_into_desktop(text):
    """回合刚结束、输入栏恢复时，把消息直接打进 ZCode 桌面输入栏并发送。

    不做历史校验（hook 有超时预算），只试前两个候选位置；失败返回 False。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import desktop_inject
        return desktop_inject.type_into_zcode(text, verify=False, max_ratios=2)
    except Exception:
        debug_log('stop', '', 'desktop-type error')
        return False


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
    see_session(ev, cwd)
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
        see_session(ev, cwd, busy=True)   # 回合开始：标记会话忙碌
        prompt = str(ev.get('prompt') or ev.get('user_prompt') or '').replace('\n', ' ').strip()
        title = 'ZCode 收到新任务'
        body = '「%s」开始处理' % project
        if prompt:
            body += '：' + (prompt[:60] + '…' if len(prompt) > 60 else prompt)
        full = prompt[:1500]
    else:
        see_session(ev, cwd, busy=False)   # 回合结束：会话回到空闲
        # 反向通道：收件箱有投给本会话（或未指定目标）的手机消息 → 以「续跑指令」
        # 形式注入会话（本轮不算完成，不发完成通知；ZCode 处理完消息、真正结束时才发）
        box = take_inbox(sid)
        if box:
            text = '\n'.join(str(m.get('text', '')).strip()[:200] for m in box[:10])
            debug_log('stop', sid, 'deliver %d' % len(box))
            # 首选：回合刚结束，输入栏正在恢复——直接把消息打进桌面输入栏并发送
            # （用户要的形态：手机发 → 自动出现在 ZCode 输入栏 → 发送 → 任务开始）
            time.sleep(1.2)
            if type_into_desktop(text):
                debug_log('stop', sid, 'desktop-typed')
                return 0   # 消息已作为新任务提交，新回合的 hook 会发「收到新任务」
            # 回退：桌面打字失败（UI 未就绪等），以注入续跑的方式送达
            lines = ['%d. %s' % (i, str(m.get('text', '')).strip()[:200])
                     for i, m in enumerate(box[:10], 1)]
            reason = ('【手机消息】手机端发来 %d 条新消息，请当作新的用户指令处理：\n%s\n'
                      '（处理完成后正常结束本轮即可，手机端会收到完成通知；'
                      '如果只是打招呼，回一句即可）' % (len(box), '\n'.join(lines)))
            # ZCode 的 Stop 续跑键是 continue:true（读自 glm/zcode.cjs 的 Lio：
            # e===Stop && t.continue===true → stopShouldContinue）；
            # hookSpecificOutput.Stop.additionalContext 再把内容注入对话，双保险
            sys.stdout.write(json.dumps({
                'continue': True,
                'stopReason': reason,
                'reason': reason,
                'hookSpecificOutput': {'hookEventName': 'Stop', 'additionalContext': reason},
            }, ensure_ascii=False))
            return 0
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
