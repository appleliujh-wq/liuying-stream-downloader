@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 流影

where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo 没找到 Python。请先安装 Python 3.10 或更高版本：
  echo https://www.python.org/downloads/
  echo 安装时勾选 "Add python.exe to PATH"，装完重新双击本文件。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 正在创建虚拟环境…
  %PY% -m venv .venv
  if errorlevel 1 (
    echo 创建虚拟环境失败。请确认 Python 已勾选 PATH。
    pause
    exit /b 1
  )
)

echo 正在安装依赖…
".venv\Scripts\python.exe" -m pip install -U pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo 依赖安装失败，请检查网络后重试。
  pause
  exit /b 1
)

if not exist downloads mkdir downloads
if not exist data mkdir data
set "DOWNLOAD_DIR=%cd%\downloads"

echo.
echo 流影已启动，浏览器打开 http://127.0.0.1:8787
echo 不要关闭这个黑窗口。关掉它，网页也就停了。
echo.
start "" "http://127.0.0.1:8787"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8787
pause
