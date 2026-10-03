# -*- coding: utf-8 -*-
"""ZCode 任务通知 APK 构建流水线（无需 Gradle/AGP）。
   工具链优先复用 ../biancheng-app/_build（腾讯镜像下载的 build-tools + platform），
   该目录不存在时自动回退本机 Android SDK（ANDROID_SDK_ROOT 或默认安装位置）。
   步骤：aapt2 编译链接 -> javac -> d8 转 dex -> 组装 zip -> zipalign -> apksigner 签名。
"""
import glob
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))     # zcode-notify/android
PROJECT = os.path.dirname(HERE)                       # zcode-notify
TOOLCHAIN = os.path.join(os.path.dirname(PROJECT), 'biancheng-app', '_build')
SDK = os.environ.get('ANDROID_SDK_ROOT',
                     os.path.expandvars(r'%LOCALAPPDATA%\Android\Sdk'))
if os.path.isdir(TOOLCHAIN):
    BT = os.path.join(TOOLCHAIN, 'android-14')
    PLAT_JAR = os.path.join(TOOLCHAIN, 'android-34', 'android.jar')
else:   # 本机 SDK：取版本号最大的 build-tools
    bts = sorted(glob.glob(os.path.join(SDK, 'build-tools', '*.*.*')))
    BT = bts[-1] if bts else os.path.join(SDK, 'build-tools', '37.0.0')
    PLAT_JAR = os.path.join(SDK, 'platforms', 'android-34', 'android.jar')
OUT = os.path.join(HERE, 'build')
DIST = os.path.join(PROJECT, 'dist')
VERSION = '0.6'
APK = os.path.join(DIST, 'zcode-notify-v%s.apk' % VERSION)
LIBS = glob.glob(os.path.join(HERE, 'libs', '*.jar'))   # 第三方 jar（如 Paho MQTT）


def run(cmd):
    print('>>', ' '.join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit('命令失败（退出码 %d）: %s' % (r.returncode, ' '.join(cmd)))


def check_tools():
    missing = [p for p in [os.path.join(BT, 'aapt2.exe'), PLAT_JAR,
                           os.path.join(BT, 'lib', 'd8.jar'),
                           os.path.join(BT, 'lib', 'apksigner.jar'),
                           os.path.join(BT, 'zipalign.exe')] if not os.path.exists(p)]
    if missing:
        src = 'biancheng-app/_build' if os.path.isdir(TOOLCHAIN) else '本机 Android SDK（%s）' % SDK
        sys.exit('缺少工具链文件（当前使用 %s，请先确认其完整）：\n' % src + '\n'.join(missing))


def step_aapt2():
    os.makedirs(OUT, exist_ok=True)
    res_zip = os.path.join(OUT, 'res.zip')
    run([os.path.join(BT, 'aapt2.exe'), 'compile', '--dir',
         os.path.join(HERE, 'res'), '-o', res_zip])
    run([os.path.join(BT, 'aapt2.exe'), 'link', '-o', os.path.join(OUT, 'base.apk'),
         '-I', PLAT_JAR,
         '--manifest', os.path.join(HERE, 'AndroidManifest.xml'),
         '--min-sdk-version', '24', '--target-sdk-version', '34',
         '--version-code', VERSION.replace('.', ''), '--version-name', VERSION,
         '--auto-add-overlay', res_zip])
    print('[1/5] 资源编译链接完成 (base.apk)')


def step_javac():
    cls = os.path.join(OUT, 'classes')
    shutil.rmtree(cls, ignore_errors=True)
    os.makedirs(cls)
    srcs = glob.glob(os.path.join(HERE, 'src', '**', '*.java'), recursive=True)
    run(['javac', '--release', '11', '-encoding', 'UTF-8',
         '-classpath', ';'.join([PLAT_JAR] + LIBS), '-d', cls] + srcs)
    print('[2/5] Java 编译完成')


def step_d8():
    dexdir = os.path.join(OUT, 'dex')
    shutil.rmtree(dexdir, ignore_errors=True)
    os.makedirs(dexdir)
    clss = glob.glob(os.path.join(OUT, 'classes', '**', '*.class'), recursive=True)
    # build-tools 34 自带的 d8(R8 8.2.2-dev) 在新版 JDK 上有 NPE，优先用下载的新版 R8
    r8_new = os.path.join(TOOLCHAIN, 'r8-9.4.28.jar')
    d8_jar = r8_new if os.path.exists(r8_new) else os.path.join(BT, 'lib', 'd8.jar')
    run(['java', '-cp', d8_jar,
         'com.android.tools.r8.D8', '--release', '--lib', PLAT_JAR,
         '--min-api', '24', '--output', dexdir] + clss + LIBS)
    print('[3/5] dex 转换完成')


def step_assemble():
    base = os.path.join(OUT, 'base.apk')
    unsigned = os.path.join(OUT, 'unsigned.apk')
    with zipfile.ZipFile(base) as zin, \
            zipfile.ZipFile(unsigned, 'w', zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            # Android 11+ 要求 resources.arsc 不压缩
            zi.compress_type = (zipfile.ZIP_STORED
                                if info.filename == 'resources.arsc' else info.compress_type)
            zi.external_attr = info.external_attr
            zout.writestr(zi, data)
        zout.write(os.path.join(OUT, 'dex', 'classes.dex'), 'classes.dex')
    print('[4/5] APK 组装完成')


def step_align_sign():
    os.makedirs(DIST, exist_ok=True)
    aligned = os.path.join(OUT, 'aligned.apk')
    run([os.path.join(BT, 'zipalign.exe'), '-f', '4',
         os.path.join(OUT, 'unsigned.apk'), aligned])
    ks = os.path.join(TOOLCHAIN if os.path.isdir(TOOLCHAIN) else OUT, 'debug.keystore')
    if not os.path.exists(ks):
        run(['keytool', '-genkeypair', '-keystore', ks, '-storetype', 'PKCS12',
             '-storepass', 'android', '-keypass', 'android',
             '-alias', 'androiddebugkey', '-keyalg', 'RSA', '-keysize', '2048',
             '-validity', '10000', '-dname', 'CN=Android Debug,O=Android,C=US'])
    run(['java', '-jar', os.path.join(BT, 'lib', 'apksigner.jar'), 'sign',
         '--ks', ks, '--ks-pass', 'pass:android', '--key-pass', 'pass:android',
         '--ks-key-alias', 'androiddebugkey', '--out', APK, aligned])
    print('[5/5] 签名完成')
    print('=' * 46)
    print('APK 产物: %s (%.1f MB)' % (APK, os.path.getsize(APK) / 1048576))
    print('=' * 46)


if __name__ == '__main__':
    check_tools()
    step_aapt2()
    step_javac()
    step_d8()
    step_assemble()
    step_align_sign()
