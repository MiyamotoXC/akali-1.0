@echo off
rem UTF-8 编码脚本：先切换代码页为 65001，再输出中文（兼容 GBK/UTF-8 两种系统设置）
chcp 65001 >nul
title 拂晓用户端启动器
cd /d "%~dp0"

rem 锁定项目 venv 解释器，避免 PATH 中其它 python（如无依赖的 managed python）抢占导致启动失败
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem 默认使用大模型 function-calling 路由；需要排障时可在启动前显式设置 legacy
if not defined AKALI_ROUTE_MODE set "AKALI_ROUTE_MODE=agent"

rem 检查 8000 端口占用（服务已启动则不重复拉起）
set SKIP=0
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul 2>&1
if "%errorlevel%"=="0" set SKIP=1

if "%SKIP%"=="1" (
    echo [提示] 8000 端口已有服务在运行，不重复启动服务窗口。
    echo 如需重启服务，请先在服务窗口按 Ctrl+C 退出。
)
if "%SKIP%"=="0" (
    rem 清除项目内旧字节码，避免源码已更新但服务仍加载同秒生成的陈旧 pyc。
    for /d /r "%~dp0.." %%D in (__pycache__) do @if exist "%%D" rd /s /q "%%D"
    echo [启动] 正在启动用户端服务 web_app.py（127.0.0.1:8000）...
    start "拂晓服务" cmd /c "chcp 65001 >nul && ""%PY%"" -B -X utf8 web_app.py"
    timeout /t 6 /nobreak >nul
)

rem cpolar 隧道（免费版随机分配地址，每次启动可能变化，以窗口输出为准）
echo [启动] cpolar 隧道窗口已另开，请在该窗口查看公网访问地址...
start "cpolar隧道" /D "D:\cpolar" cmd /c "chcp 65001 >nul && cpolar.exe http 8000 -log=stdout"

echo.
echo ============================================
echo  拂晓用户端启动完成
echo   1. 本地服务：浏览器打开 http://127.0.0.1:8000
echo   2. cpolar公网地址：以隧道窗口输出为准
echo      形如 https://xxxx.r7.cpolar.top/?token=...
echo  关闭：在两个窗口分别 Ctrl+C 退出即可
echo  注意：公网隧道地址每次启动会变化
echo ============================================
pause
