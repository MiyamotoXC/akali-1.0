@echo off
chcp 936 >nul
cd /d "%~dp0"
setlocal

rem ===========================================================================
rem  阿卡莉 1.0 —— 部署到云端（腾讯云 43.142.42.138 / /root/akali）
rem
rem  原理：docker-compose.yml 把 用户端特用/通用工具/浏览器扩展/deploy 从**宿主机**
rem        挂载进容器，所以改代码只需「同步文件 + 重建容器」，无需 docker build
rem        （只有 requirements.txt 变了才需要 build）。
rem
rem  策略（自动判断）：
rem    · 服务器上 /root/akali 是 git 仓库  -> git pull（最干净，只拉差异）
rem    · 不是 git 仓库                     -> 本机打包上传（排除缓存/面板备份/知识库）
rem    · git 仓库但拉取失败               -> 会提示；可加参数 upload 强制走上传
rem
rem  用法：双击运行（提示时输入服务器 root 密码）
rem        强制上传：部署到云端.bat upload
rem ===========================================================================

set "HOST=43.142.42.138"
set "DIR=/root/akali"
set "PKG=akali_deploy.tgz"

echo ============================================================
echo   阿卡莉 1.0  部署到云端   %HOST%
echo ============================================================
echo.

if /i "%~1"=="upload" goto upload

echo [1/4] 探测服务器仓库形态（请输入 root 密码）...
ssh root@%HOST% "cd %DIR% || exit 3; if [ -d .git ]; then GIT_TERMINAL_PROMPT=0 git pull --ff-only || exit 2; else exit 4; fi"
if errorlevel 4 goto upload
if errorlevel 3 (
  echo.
  echo [!] 服务器上没有 %DIR% 目录，请确认部署路径。
  pause
  exit /b 1
)
if errorlevel 2 (
  echo.
  echo [!] 这是 git 仓库，但 git pull 失败。
  echo     常见原因：私仓在服务器上没有凭据（部署密钥），
  echo               或服务器工作区有本地改动（可先 git stash / git checkout -- .）。
  echo     想直接覆盖上传：重跑本脚本并加参数 upload
  echo                    部署到云端.bat upload
  pause
  exit /b 1
)
echo      git pull 完成。
goto recreate

:upload
echo [1/4] 打包本机代码...
del /q "%PKG%" 2>nul
rem 注意：刻意不上传 docker-compose.yml / Dockerfile / requirements.txt
rem       服务器那份编排用宿主绑定挂载(./users_data、./反馈信息/反馈收集)；
rem       覆盖成仓库版本会切到命名卷，导致用户表与反馈「消失」。要改编排请在服务器上单独处理。
tar -czf "%PKG%" --exclude=__pycache__ --exclude=*.pyc --exclude=panels --exclude=知识库 --exclude=.env --exclude=users.json --exclude=users.test.json 通用工具 用户端特用 浏览器扩展 开发者入口 deploy docs workspace pyrightconfig.json README.md 一键发布到GitHub.bat
if errorlevel 1 (
  echo [!] 打包失败。
  pause
  exit /b 1
)
for %%A in ("%PKG%") do echo        包大小 %%~zA 字节
echo [2/4] 上传到服务器（会再要一次密码）...
scp "%PKG%" root@%HOST%:/tmp/%PKG%
if errorlevel 1 (
  echo [!] 上传失败（密码错误或网络不通）。
  del /q "%PKG%" 2>nul
  pause
  exit /b 1
)
echo [3/4] 在服务器上解包...
ssh root@%HOST% "cd %DIR% && tar -xzf /tmp/%PKG% && rm -f /tmp/%PKG% && echo UNPACK_OK"
del /q "%PKG%" 2>nul

:recreate
echo.
echo [4/4] 部署前自检口令 + 重建容器...
ssh root@%HOST% "cd %DIR% && if grep -qE '^AKALI_OWNER_TOKEN=.+' .env || grep -qE '^AKALI_INVITE_CODE=.+' .env; then docker compose up -d --force-recreate akali && sleep 3 && docker compose ps && echo '--- .env 关键项（仅列名，不显示值）---' && sed 's/=.*//' .env | grep -E 'AKALI_OWNER_TOKEN|AKALI_INVITE_CODE|FX_QUOTA|FX_REDIS|FX_CLOUD'; else echo ENV_AUTH_MISSING; exit 9; fi"
if errorlevel 9 (
  echo.
  echo [!] 服务器 .env 里 AKALI_OWNER_TOKEN / AKALI_INVITE_CODE 都是空的。
  echo     新版代码不再认旧名 API_TOKEN —— 这样重建会把鉴权彻底关掉（公网可被白嫖）。
  echo     请先按「环境变量配置说明.md - 旧部署迁移」补好 .env 再重跑本脚本。
  echo     （已中止，容器未重建，站点仍跑旧代码。）
  pause
  exit /b 1
)

echo.
echo --- 云端 /api/auth/status ---
curl.exe -s -m 15 http://%HOST%/api/auth/status
echo.
echo.
echo ============================================================
echo   完成。上面能看到 "run_mode":"cloud" 即部署成功。
echo   提示：若 .env 关键项里缺 FX_QUOTA_* 或 AKALI_OWNER_TOKEN，
echo         请编辑服务器 %DIR%/.env 后重跑本脚本。
echo ============================================================
pause
