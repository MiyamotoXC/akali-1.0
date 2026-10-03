@echo off
rem 本文件以 ANSI/GBK（代码页 936）保存：启动时切到 936 才能正确显示下面的中文
rem （勿改回 UTF-8 + chcp 65001：cmd 会错位解析中文并报 not recognized 错）
chcp 936 >nul
title 拂晓用户端启动器
cd /d "%~dp0"

rem 锁定项目 venv 解释器，避免 PATH 中其它 python（如无依赖的 managed python）抢占导致启动失败
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem 检查 8000 端口占用（服务已启动则不重复拉起）
set "SKIP=0"
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul 2>&1
if "%errorlevel%"=="0" set "SKIP=1"

if "%SKIP%"=="1" (
    echo [提示] 8000 端口已有服务在运行，不重复启动服务窗口。
    echo 如需重启服务，请先在服务窗口按 Ctrl+C 退出。
)
if "%SKIP%"=="0" (
    rem 清除项目内旧字节码，避免源码已更新但服务仍加载同秒生成的陈旧 pyc。
    for /d /r "%~dp0.." %%D in (__pycache__) do @if exist "%%D" rd /s /q "%%D"
    echo [启动] 正在启动用户端服务 web_app.py（127.0.0.1:8000）...
    rem 注：%PY% 此处未加引号（本机路径无空格）；若项目路径含空格，需改成 ""%PY%"" 的双引号转义写法
    start "拂晓服务" cmd /k "chcp 936 >nul && %PY% -B -X utf8 web_app.py"
    timeout /t 6 /nobreak >nul
)

rem ── 关于内网穿透（cpolar 隧道）：已移除 ──
rem 用户口径 2026-09-24：已有正式云服务器（docker compose 常驻运行），
rem 本机不再需要把 8000 端口穿到公网；原 cpolar 自动启动逻辑一并删除。
rem 若日后仍需临时暴露本机（例如给云服务器传大文件），见 README「传输大文件到服务器」一节，
rem 按需临时搭建即可，不做成常驻功能。

echo.
echo ============================================
echo  拂晓用户端启动完成
echo   本地服务：浏览器打开 http://127.0.0.1:8000
echo   云端服务：部署在云服务器上（docker compose 常驻，无需在本机启动）
echo   关闭：在服务窗口按 Ctrl+C 退出即可
echo ============================================
pause
