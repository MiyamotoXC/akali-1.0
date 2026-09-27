# -*- coding: utf-8 -*-
"""心跳与自动关闭 —— 从 web_app.py 拆出的独立模块。

承载两个路由（/api/heartbeat、/api/shutdown）与心跳超时看门狗线程。
路由用 FastAPI 的 APIRouter 声明，由 web_app 执行 app.include_router ——
子模块不持有 app 实例，避免 web_app ↔ 子模块循环导入。

启动标识 _BOOT_ID 由 web_app 注入：它决定页面是否判定自己为"上次遗留的旧页面"。
"""
import os
import threading as _threading
import time

from fastapi import APIRouter

from core import runtime

router = APIRouter()

_BOOT_ID = ''


def configure(boot_id: str):
    """注入本次启动标识（web_app 启动时调用）。"""
    global _BOOT_ID
    _BOOT_ID = boot_id or ''


# ==================== 心跳与自动关闭 ====================
import threading as _threading
import signal as _signal

# 最后一次心跳时间（用于检测客户端是否在线）
_last_heartbeat = 0.0
_heartbeat_lock = _threading.Lock()
_server_should_shutdown = False
_shutdown_event = _threading.Event()


@router.get("/api/heartbeat")
def heartbeat():
    """前端定期调用，维持服务存活。返回当前服务器时间。

    免鉴权（见 _AUTH_PUBLIC_EXACT）：未解锁的登录页也会打心跳，带口令反而会被
    中间件判 401 —— 线上表现为每 30 秒一条 401 刷屏；心跳只回时间戳、无敏感数据，
    放行无越权风险。

    boot 只在**本地模式**下发（用户口径 2026-09-27）：它的用途是「服务重启后让上一轮
    遗留的页面自动失效」。本地单机重启＝用户自己重开，关掉旧页面合理；但云端重启是
    运维动作（改配置/发版），不该把正在用的测试用户踢下线（前端判 `data.boot &&` 后再
    自毁，空串即跳过），故云端返回空串。
    """
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.time()
    return {"ok": True, "server_time": time.time(),
            "boot": "" if runtime.is_cloud() else _BOOT_ID}


@router.post("/api/shutdown")
def shutdown():
    """前端关闭页面时调用，触发服务优雅关闭。
    仅当启用了 AKALI_AUTO_SHUTDOWN=1 时才生效；默认不响应关闭信号。"""
    _auto_shutdown = (os.environ.get('AKALI_AUTO_SHUTDOWN') or '').strip() in ('1', 'true', 'on', 'yes')
    if not _auto_shutdown:
        return {"ok": True, "msg": "自动关闭已禁用，服务保持运行"}
    global _server_should_shutdown
    _server_should_shutdown = True
    _shutdown_event.set()
    return {"ok": True, "msg": "服务即将关闭"}


def _pid_process_name(pid: int) -> str:
    """查询指定 PID 的进程映像名（tasklist），失败返回空串。仅 Windows 有效。"""
    if os.name != 'nt':
        return ''
    try:
        import subprocess
        result = subprocess.run(
            ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, creationflags=0x08000000, timeout=5
        )
        line = (result.stdout or '').strip()
        if line and ',' in line:
            # CSV 首个字段即映像名，形如 "python.exe"
            return line.split(',')[0].strip('"')
    except Exception:
        pass
    return ''


def _take_over_stale_server(host: str, port: int) -> bool:
    """启动接管：若端口被上一次的阿卡莉服务（python 进程）占用则自动终止，返回是否已腾出端口。
    仅终止 python 映像的监听进程；其他程序占用时不做处理，由调用方走备用端口逻辑。
    旧服务退出后，遗留的旧页面会通过启动标识（boot id）心跳对比自动失效关闭。"""
    # 进程接管依赖 Windows 的 netstat / taskkill；Linux（云端容器）跳过
    if os.name != 'nt':
        return False
    pid = None
    try:
        import subprocess
        result = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True, text=True, creationflags=0x08000000, timeout=5
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and f':{port}' in parts[1] and 'LISTENING' in line:
                try:
                    pid = int(parts[-1])
                except ValueError:
                    pass
                break
    except Exception:
        return False
    if not pid or pid == os.getpid():
        return False
    name = _pid_process_name(pid)
    if 'python' not in name.lower():
        return False
    try:
        import subprocess
        subprocess.run(
            ['taskkill', '/PID', str(pid), '/F'],
            capture_output=True, creationflags=0x08000000, timeout=5
        )
        print(f"[接管] 已终止上次的服务进程 PID={pid}（{name}）", flush=True)
        time.sleep(0.8)
        return True
    except Exception as e:
        print(f"[接管] 终止旧进程失败: {e}", flush=True)
        return False


def _heartbeat_watcher(interval=30, timeout=120):
    """后台线程：监控心跳，超时则触发关闭。
    
    Args:
        interval: 检查间隔（秒）
        timeout: 无心跳超时时间（秒），超过则自动关闭
    """
    global _last_heartbeat, _server_should_shutdown
    _last_heartbeat = time.time()
    print(f"[心跳监控] 已启动，超时时间 {timeout} 秒")
    while not _shutdown_event.is_set():
        _shutdown_event.wait(interval)
        if _server_should_shutdown:
            print("[心跳监控] 收到关闭信号，准备退出...")
            break
        if _last_heartbeat and (time.time() - _last_heartbeat > timeout):
            print(f"[心跳监控] {timeout} 秒内无心跳，准备自动退出...")
            _server_should_shutdown = True
            break


