@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在构建 ZCode 任务通知 APK（无需 Gradle，复用随身编程的工具链，约 1 分钟）...
python android\build_apk.py
if errorlevel 1 pause
