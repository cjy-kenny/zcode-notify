# -*- coding: utf-8 -*-
"""把一条消息"打"进 ZCode 桌面窗口：等价于用户亲手输入，空闲时也能立刻起回合。

做法：找到 ZCode 主窗口 → 拉到前台 → 消息放进剪贴板 → Ctrl+V → Enter → 还原剪贴板。
全部 ctypes 实现，失败静默返回 False（调用方回退到收件箱路线）。
"""
import ctypes
import subprocess
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 64 位下必须声明句柄宽度，否则 GlobalAlloc 返回值被截断成 32 位，
# GlobalLock 拿到 NULL → memmove 直接访问违例
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002


def _pids_by_name(name='ZCode.exe'):
    try:
        tl = subprocess.run(['tasklist'], capture_output=True).stdout.decode('utf-8', 'replace')
        return [l.split()[1] for l in tl.splitlines() if l.startswith(name)]
    except Exception:
        return []


def find_window():
    """返回 ZCode 主窗口句柄（可见、有标题、属于 ZCode.exe 的最上层窗口）。"""
    pids = set(_pids_by_name())
    if not pids:
        return None
    result = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if str(pid.value) in pids:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value.strip():
                    result.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return result[0][0] if result else None


def _clip_get():
    """读剪贴板文本（读不到返回空串）。"""
    try:
        if not user32.OpenClipboard(None):
            return ''
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ''
            p = ctypes.windll.kernel32.GlobalLock(h)
            if not p:
                return ''
            try:
                return ctypes.c_wchar_p(p).value or ''
            finally:
                ctypes.windll.kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
    except Exception:
        return ''


def _clip_set(text):
    """写剪贴板文本。"""
    user32.OpenClipboard(None)
    try:
        user32.EmptyClipboard()
        h = ctypes.windll.kernel32.GlobalAlloc(GMEM_MOVEABLE, (len(text) + 1) * 2)
        p = ctypes.windll.kernel32.GlobalLock(h)
        ctypes.memmove(p, ctypes.create_unicode_buffer(text), (len(text) + 1) * 2)
        ctypes.windll.kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
    finally:
        user32.CloseClipboard()


def _key(vk, up=False):
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def _paste():
    _key(VK_CONTROL)
    time.sleep(0.03)
    _key(VK_V)
    time.sleep(0.03)
    _key(VK_V, up=True)
    _key(VK_CONTROL, up=True)


RATIOS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'click_ratio.txt')
VK_ESCAPE = 0x1B


def _ratios():
    """候选点击高度（自窗口底部起算的比例），首选上次验证成功的值。"""
    try:
        base = [float(open(RATIOS_FILE).read().strip())]
    except Exception:
        base = [0.13]
    for r in [0.13, 0.18, 0.10, 0.22]:
        if r not in base:
            base.append(r)
    return base


def _history_has(text, timeout=6):
    """查服务端通知历史：这条消息是否已作为新任务进入 ZCode。"""
    import json
    import urllib.request
    end = time.time() + timeout
    key = ('【手机消息】' + text)[:24]
    while time.time() < end:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/history', timeout=2) as r:
                items = json.load(r).get('items') or []
            for it in items:
                if it.get('title') == 'ZCode 收到新任务' and key in (it.get('body') or ''):
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def type_into_zcode(text, verify=True, max_ratios=None):
    """把消息输入 ZCode 桌面窗口并回车发送。

    verify=True 时查新任务列表确认真的进了会话（自校验，成功的高度记入
    click_ratio.txt 供下次首选）；verify=False 供 hook 的快速路径。
    点击高度自适应：从候选比例依次尝试，哪次成功记住哪次。
    """
    try:
        ratios = _ratios()
        if max_ratios:
            ratios = ratios[:max_ratios]
        win = find_window()
        if not win:
            return False
        for ratio in ratios:
            hwnd = find_window() or win
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)
            _key(0x12)
            user32.SetForegroundWindow(hwnd)
            _key(0x12, up=True)
            time.sleep(0.3)
            _key(VK_ESCAPE)   # 关掉可能停留/打开的下拉（如模型选择器）
            time.sleep(0.15)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetCursorPos(rect.left + (rect.right - rect.left) // 2,
                                rect.bottom - int((rect.bottom - rect.top) * ratio))
            time.sleep(0.12)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(0.25)
            old = _clip_get()
            for attempt in range(3):
                if _clip_set('【手机消息】' + text):
                    break
                time.sleep(0.2)
            time.sleep(0.1)
            _paste()
            time.sleep(0.15)
            _key(VK_RETURN)
            time.sleep(0.1)
            user32.SetCursorPos(pt.x, pt.y)
            _clip_set(old)
            if not verify or _history_has(text):
                try:
                    open(RATIOS_FILE, 'w').write(str(ratio))
                except Exception:
                    pass
                return True
        return False
    except Exception:
        return False
