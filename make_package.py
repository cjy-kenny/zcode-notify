# -*- coding: utf-8 -*-
"""把 APK 打成手机传输用的压缩包（微信会把 .apk 改名，压缩包能原样传过去）。
   产物：dist/ZCode任务通知-安装包-vX.Y.zip（内含 APK + 使用说明.txt）。
   用法：python make_package.py（自动取 dist 里最新版本的 APK）
"""
import glob
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, 'dist')


def main():
    apks = sorted(glob.glob(os.path.join(DIST, 'zcode-notify-v*.apk')))
    if not apks:
        raise SystemExit('dist/ 里没有 APK，先运行 打包APK.bat')
    apk = apks[-1]
    version = os.path.basename(apk)[len('zcode-notify-v'):-len('.apk')]
    out = os.path.join(DIST, 'ZCode任务通知-安装包-v%s.zip' % version)

    readme = """ZCode 任务通知 v%s 使用说明
================================

这是一份安卓安装包（APK），传到手机后这样装：

1. 在手机上打开本压缩包，解压出 zcode-notify-v%s.apk
   （微信里：长按文件 -> 用其他应用打开 -> 文件管理器/解压工具）
2. 点 APK 安装；提示"未知来源/禁止安装"时，选择允许该来源
3. 首次打开 app：
   - 手机连上和电脑同一个 Wi-Fi，app 会自动搜索电脑并连上
   - 允许"通知"权限
4. 电脑端：双击 zcode-notify 文件夹里的 启动通知服务.bat
   服务没开的话手机是收不到的；首次弹防火墙窗口要点"允许"
5. 之后电脑上的 ZCode 任务开始/完成，手机都会弹通知，
   app 在后台、锁屏也能收（建议把它加入电池白名单）

人在外面、手机走流量也能收（经公网中转），
前提是电脑端服务在线并且联了网。

更多说明见电脑端 zcode-notify/README.md
""" % (version, version)

    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(apk, 'zcode-notify-v%s.apk' % version)
        z.writestr('使用说明.txt', readme)
    print('安装包已生成: %s (%.1f KB)' % (out, os.path.getsize(out) / 1024))
    return 0


if __name__ == '__main__':
    sys.exit(main())
