@echo off
chcp 65001 >nul
setlocal
set "SRV=%~dp0server.py"
set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\zcode-notify.vbs"
where pythonw >nul 2>nul
if errorlevel 1 (
  echo 没找到 pythonw.exe：请确认 Python 已安装并加入 PATH 后再试。
  pause
  exit /b 1
)
> "%VBS%" echo CreateObject("WScript.Shell").Run "pythonw ""%SRV%""", 0, False
echo 已设置开机自启：每次登录 Windows 会静默启动通知服务（无窗口）。
echo 位置: %VBS%
echo 取消请运行 取消开机自启.bat
pause
