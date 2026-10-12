@echo off
rem kmcf A股每日复盘：双击启动桌面软件（pythonw 运行，不显示黑色控制台窗口）
setlocal
set "ROOT=%~dp0"
set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
if not exist "%PYW%" goto :novenv
rem /D 设置工作目录（去掉末尾的反斜杠，避免路径里的 \" 被误解析）
start "" /D "%ROOT:~0,-1%" "%PYW%" "%ROOT%main.py"
endlocal
exit /b 0

:novenv
chcp 65001 >nul
echo 没有找到 Python 虚拟环境：
echo   %PYW%
echo.
echo 请先在项目目录里创建虚拟环境并安装依赖：
echo   python -m venv .venv
echo   .venv\Scripts\pip install -r requirements.txt
echo.
pause
endlocal
exit /b 1
