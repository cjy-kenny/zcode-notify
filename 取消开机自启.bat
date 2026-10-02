@echo off
chcp 65001 >nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\zcode-notify.vbs" 2>nul
echo 已取消开机自启（若之前设置过）。
pause
