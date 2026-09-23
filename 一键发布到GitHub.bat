@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ===========================================================================
rem  阿卡莉 1.0 —— 一键发布到 GitHub（私有仓库 akali-1.0）
rem  等价于手动执行：git add -A  +  git commit  +  git push
rem  用法：双击运行（自动附时间戳说明）
rem        或带一句说明：一键发布到GitHub.bat "修了攻略输出的类数"
rem  说明：提交身份用 -c 一次性传入（不改动你的全局 git 配置）
rem ===========================================================================

set "GITNAME=MiyamotoXC"
set "GITMAIL=196594543+MiyamotoXC@users.noreply.github.com"

echo ============================================================
echo   一键发布到 GitHub
echo   仓库：https://github.com/MiyamotoXC/akali-1.0
echo ============================================================
echo.

echo [1/3] 暂存全部改动...
git add -A
echo.
echo 本次将提交的改动：
git status --short
echo.

set "MSG=%~1"
if "%MSG%"=="" set "MSG=更新 %date% %time%"

echo [2/3] 提交：%MSG%
git -c user.name="%GITNAME%" -c user.email="%GITMAIL%" commit -m "%MSG%"
if errorlevel 1 (
  echo.
  echo [!] 提交未成功：通常是没有改动，或 git 身份/网络异常。
  echo.
  pause
  exit /b 0
)

echo.
echo [3/3] 推送到 GitHub...
git push
echo.

echo ============================================================
echo   完成。按任意键关闭窗口。
echo ============================================================
pause >nul
