@echo off
chcp 936 >nul
rem ============================================================
rem  Android ADB 鼠标侧键控制器 - 启动脚本
rem  说明：本文件用 GBK 编码，中文只出现在调用 python 之前，
rem        避免 cmd 用 UTF-8 解析时把中文行撕成碎片（窗口一闪而过）。
rem ============================================================
cd /d "%~dp0"
title Android ADB 鼠标侧键控制器
set "PY="

call :try_cmd "py -3"
if not defined PY call :try_cmd "python"
if not defined PY call :try_cmd "python3"

for %%V in (313 312 311 310 39 38) do (
    if not defined PY call :try_file "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
    if not defined PY call :try_file "C:\Program Files\Python%%V\python.exe"
)
if not defined PY call :try_file "C:\Program Files\Python313\python.exe"

if not defined PY (
    echo.
    echo [错误] 没有找到可用的 Python 3，程序无法启动。
    echo.
    echo   1. 请先安装 Python 3.9 或更高版本：https://www.python.org/downloads/
    echo      安装时务必勾选 "Add python.exe to PATH"。
    echo   2. 如果已经装好，请把 python.exe 所在目录加入系统 PATH，
    echo      或右键编辑本文件，把下面这行改成你的 python.exe 完整路径：
    echo          set "PY=C:\Python313\python.exe"
    echo.
    pause
    exit /b 1
)

echo 使用的 Python: %PY%
echo.

rem 依赖检查：缺 pynput 就装一次
%PY% -c "import pynput" >nul 2>nul
if errorlevel 1 (
    echo [提示] 缺少 pynput，正在安装依赖...
    %PY% -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请手动执行: %PY% -m pip install pynput
        echo.
        pause
        exit /b 1
    )
)

%PY% main.py %*
set "CODE=%ERRORLEVEL%"
rem Exit reason + log path are printed by main.py (_explain_exit);
rem this script only keeps the window open. ASCII only on purpose.
if not "%CODE%"=="0" pause
endlocal & exit /b %CODE%

:try_cmd
%~1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=%~1"
exit /b 0

:try_file
if exist %~1 (
    %~1 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=%~1"
)
exit /b 0
