# coding=utf-8
"""
任务运行控制模块

负责 Web 设置中心的"立即运行"/"测试通知"功能：
- 进程检测（含 cron 触发的运行，防止并发重叠）
- 日志文件管理（output/logs/，静态托管已屏蔽该目录）
- 日志尾部读取

安全设计：
- subprocess 全部使用 argv 列表传参，无 shell 拼接
- 日志读取仅限 run-*.log 白名单模式
"""

import glob
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from trendradar.webadmin.config_io import get_logs_dir

# 进程锁文件（记录 Web 触发运行的 PID）
LOCK_FILE = Path("/tmp/trendradar_web_run.lock")

# 最近一次启动的子进程句柄（用于回收僵尸进程）
_last_process: Optional[Any] = None

# 日志文件名白名单模式
_RUN_LOG_RE = re.compile(r"^run-[\w.-]+\.log$")

# 日志尾部默认行数
DEFAULT_TAIL_LINES = 200


def _app_root() -> Path:
    """应用根目录（Docker 中为 /app，本地为仓库根）"""
    return Path(os.getcwd())


def _read_proc_cmdline(pid: int) -> str:
    """读取进程 cmdline（Linux），失败返回空字符串"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _pid_alive(pid: int) -> bool:
    """进程是否存活（跨平台，僵尸进程视为已结束）"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """检查进程是否为僵尸进程（已退出但未被回收）"""
    try:
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.exists():
            # /proc/<pid>/stat 格式: pid (comm) state ...
            fields = stat_path.read_text().rsplit(")", 1)[-1].split()
            return bool(fields) and fields[0] == "Z"
    except OSError:
        return False
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip().startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return False


def _reap_last_process() -> None:
    """非阻塞回收上次启动的子进程，避免僵尸进程占用锁"""
    global _last_process
    if _last_process is not None:
        try:
            _last_process.poll()
        except Exception:
            pass


def _matches_trendradar_cmdline(cmdline: str) -> bool:
    """
    精确匹配主程序调用形式: python -m trendradar [flags]

    排除 webadmin 自身（-m trendradar.webadmin）与命令行里
    恰好包含 "trendradar" 字样的无关进程（grep、编辑器等）。
    """
    return bool(re.search(r"(?:^|\s)-m\s+trendradar(?:\s|$)", cmdline))


def find_trendradar_processes() -> List[Dict[str, Any]]:
    """
    查找正在运行的 trendradar 主程序进程（含 cron 触发的）

    Linux 下扫描 /proc，其他平台回退到 ps。
    """
    processes: List[Dict[str, Any]] = []

    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            cmdline = _read_proc_cmdline(pid)
            if _matches_trendradar_cmdline(cmdline):
                processes.append({"pid": pid, "cmdline": cmdline[:200]})
    else:
        try:
            output = subprocess.run(
                ["ps", "-ax", "-o", "pid=,command="],
                capture_output=True, text=True, timeout=5,
            )
            for line in output.stdout.splitlines():
                line = line.strip()
                if "ps -ax" in line:
                    continue  # 跳过 ps 自身
                pid_str, _, cmd = line.partition(" ")
                if pid_str.isdigit() and _matches_trendradar_cmdline(cmd):
                    pid = int(pid_str)
                    if _is_zombie(pid):
                        continue  # 跳过僵尸进程
                    processes.append({"pid": pid, "cmdline": cmd[:200]})
        except (OSError, subprocess.SubprocessError):
            pass

    return processes


def _lock_pid() -> Optional[int]:
    """读取锁文件中的 PID，进程已死亡时清理锁文件"""
    _reap_last_process()
    if not LOCK_FILE.exists():
        return None
    try:
        pid = int(LOCK_FILE.read_text().strip())
    except (ValueError, OSError):
        _cleanup_lock()
        return None

    if not _pid_alive(pid):
        _cleanup_lock()
        return None
    return pid


def _cleanup_lock() -> None:
    """清理锁文件"""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def is_run_in_progress() -> bool:
    """是否有任务正在运行（Web 触发或 cron 触发）"""
    if _lock_pid() is not None:
        return True
    return bool(find_trendradar_processes())


def start_run(source_ip: str = "") -> Dict[str, Any]:
    """
    立即运行一次主程序（异步：启动后立即返回）

    Returns:
        {ok, message, log_name}
    """
    if is_run_in_progress():
        return {"ok": False, "message": "已有任务正在运行（可能是定时任务触发），请等待完成后再试", "log_name": None}

    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    log_name = f"run-{ts}.log"
    log_path = logs_dir / log_name

    try:
        log_file = open(log_path, "w", encoding="utf-8")
        log_file.write(f"# 手动触发（来源: {source_ip or '本机'}）\n")
        log_file.flush()
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}  # 实时输出日志
        process = subprocess.Popen(
            [sys.executable, "-m", "trendradar"],
            cwd=str(_app_root()),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as e:
        return {"ok": False, "message": f"启动失败: {e}", "log_name": None}

    try:
        LOCK_FILE.write_text(str(process.pid))
    except OSError:
        pass

    global _last_process
    _last_process = process

    return {"ok": True, "message": f"任务已启动（PID: {process.pid}）", "log_name": log_name}


def run_test_notification(timeout: int = 90) -> Dict[str, Any]:
    """
    发送测试通知（同步阻塞，带超时）

    复用主程序 --test-notification 能力。
    """
    if is_run_in_progress():
        return {"ok": False, "message": "已有任务正在运行，请等待完成后再试"}

    try:
        result = subprocess.run(
            [sys.executable, "-m", "trendradar", "--test-notification"],
            cwd=str(_app_root()),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return {"ok": result.returncode == 0, "message": "执行完成", "output": output[-4000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"执行超时（>{timeout}s），请检查通知渠道配置", "output": ""}
    except OSError as e:
        return {"ok": False, "message": f"执行失败: {e}", "output": ""}


def run_status(tail_lines: int = DEFAULT_TAIL_LINES) -> Dict[str, Any]:
    """
    当前运行状态 + 最新日志尾部

    Returns:
        {running, pid, log_name, tail}
    """
    web_pid = _lock_pid()
    running = is_run_in_progress()
    pid = web_pid or (find_trendradar_processes() or [{}])[0].get("pid")

    log_name, tail = None, ""
    logs = list_run_logs(limit=1)
    if logs:
        log_name = logs[0]["name"]
        tail = read_run_log(log_name, tail_lines)

    return {"running": running, "pid": pid, "log_name": log_name, "tail": tail}


def list_run_logs(limit: int = 20) -> List[Dict[str, Any]]:
    """列出最近的运行日志（按时间倒序）"""
    logs_dir = get_logs_dir()
    if not logs_dir.is_dir():
        return []
    logs = []
    for path in sorted(logs_dir.glob("run-*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not _RUN_LOG_RE.match(path.name):
            continue
        logs.append({
            "name": path.name,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)),
            "size": path.stat().st_size,
        })
        if len(logs) >= limit:
            break
    return logs


def read_run_log(name: str, tail_lines: int = DEFAULT_TAIL_LINES) -> str:
    """
    读取运行日志尾部（仅限 run-*.log 白名单）
    """
    if not _RUN_LOG_RE.match(name) or ".." in name:
        return ""
    log_path = get_logs_dir() / name
    if not log_path.is_file():
        return ""

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-tail_lines:])
    except OSError:
        return ""
