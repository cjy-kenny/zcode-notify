@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ZCode 任务通知服务
echo ==============================================
echo   ZCode 任务通知服务（保持本窗口开着）
echo.
echo   管理面板与手机端地址见下方服务横幅
echo   关闭本窗口即停止服务
echo ==============================================
python server.py
pause
