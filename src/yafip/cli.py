"""启动脚本的命令行入口：start / stop / restart。

用法
    python -m yafip.cli                 # 等价于 start（前台）
    python -m yafip.cli start -b        # 后台运行，关闭终端不影响服务
    python -m yafip.cli stop            # 平滑停止：等待运行中的任务执行完
    python -m yafip.cli stop --force    # 取消运行中的任务后尽快退出
    python -m yafip.cli restart [-b]    # 先 stop 再 start

只输出生命周期级别的少量信息，并同步追加到 logs/start_yafip.log。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from yafip import runtime


def log(root: Path, message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[yafip] {message}", flush=True)
    path = Path(root) / "logs" / "start_yafip.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _active_tasks(info: dict | None) -> int:
    tasks = (info or {}).get("tasks") or {}
    return sum(int(tasks.get(key, 0) or 0) for key in ("PENDING", "RUNNING", "STOPPING"))


def _open_browser(url: str) -> None:
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 打开浏览器失败不影响服务
        pass


def _open_when_ready(port: int, url: str, timeout: float = 60.0) -> None:
    def worker() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if runtime.health(port, timeout=0.5) is not None:
                _open_browser(url)
                return
            time.sleep(0.5)

    threading.Thread(target=worker, name="yafip-browser", daemon=True).start()


def _wait_ready(port: int, process: subprocess.Popen | None, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.health(port, timeout=1.0) is not None:
            return True
        if process is not None and process.poll() is not None:
            # 启动器进程可能先退出再交接给子进程，最后再确认一次
            return runtime.health(port, timeout=1.0) is not None
        time.sleep(0.5)
    return False


def _ensure_whitelist(root: Path) -> None:
    """启动前检查白名单种子；初始化结果属于重要信息，输出到控制台与启动日志。"""
    try:
        from yafip.bootstrap import initialize

        result = initialize(root)
    except Exception as exc:  # noqa: BLE001 - 初始化失败不阻塞服务启动
        log(root, f"白名单初始化失败：{exc}")
        return
    if result.status in ("created", "seeded"):
        log(root, result.message)


def start(root: Path, background: bool = False, no_browser: bool = False) -> int:
    runtime.migrate_legacy_port(root)
    port = runtime.resolve_port(root)
    url = f"http://127.0.0.1:{port}"

    if runtime.health(port) is not None:
        log(root, f"服务已在运行：{url}")
        if not no_browser:
            _open_browser(url)
        return 0

    _ensure_whitelist(root)

    if not background:
        log(root, f"前台启动：{url}（Ctrl+C 停止，关闭本窗口即停止服务）")
        if not no_browser:
            _open_when_ready(port, url)
        from yafip.server import main as server_main

        code = server_main()
        log(root, "服务已停止")
        return int(code or 0)

    console_log = Path(root) / "logs" / "yafip.console.log"
    console_log.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["YAFIP_HOME"] = str(root)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONUTF8", "1")

    with console_log.open("ab") as console:
        kwargs: dict = {
            "cwd": str(root),
            "stdin": subprocess.DEVNULL,
            "stdout": console,
            "stderr": subprocess.STDOUT,
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            kwargs["start_new_session"] = True

        process = subprocess.Popen([sys.executable, "-m", "yafip.server"], **kwargs)

    if _wait_ready(port, process):
        run_info = runtime.read_run_info(root)
        pid = run_info.get("pid") or process.pid
        log(root, f"后台启动完成：{url}（PID {pid}，关闭本窗口不影响服务）")
        if not no_browser:
            _open_browser(url)
        return 0

    log(root, f"后台启动失败，请查看 {Path(root) / 'logs' / 'yafip.log'} 与 yafip.console.log")
    return 1


def stop(root: Path, force: bool = False) -> int:
    info = runtime.read_run_info(root)
    port = runtime.resolve_port(root)
    try:
        if info.get("port"):
            port = int(info["port"])
    except (TypeError, ValueError):
        pass

    current = runtime.health(port)
    if current is None:
        log(root, "服务未运行")
        runtime.clear_run_info(root)
        return 0

    try:
        result = runtime.request_stop(port, force=force)
    except Exception as exc:  # noqa: BLE001
        log(root, f"停止请求失败：{exc}")
        return 1

    active = _active_tasks(current)
    if force:
        log(root, f"已请求强制停止（运行中任务 {active} 个，取消后退出）")
    elif active:
        log(root, f"已请求平滑停止：等待 {active} 个任务执行完成后退出")
    else:
        log(root, "已请求平滑停止：无运行中任务，正在退出")
    if not result.get("accepted", True):
        log(root, "停止流程已在执行中，继续等待")

    deadline = time.monotonic() + 90 if force else None
    last_report = time.monotonic()
    while runtime.health(port) is not None:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            log(root, "强制停止等待超时，请手动结束服务进程")
            return 1
        if now - last_report >= 30:
            running = _active_tasks(runtime.health(port))
            if running:
                log(root, f"仍在等待任务完成：{running} 个运行中")
            last_report = now
        time.sleep(1.0)

    runtime.clear_run_info(root)
    log(root, "服务已停止")
    return 0


def restart(root: Path, background: bool = False, no_browser: bool = False) -> int:
    code = stop(root)
    if code != 0:
        return code
    time.sleep(1.0)
    return start(root, background=background, no_browser=no_browser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yafip", description="yafip 启停工具")
    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="启动服务（默认前台）")
    p_start.add_argument("-b", "--background", action="store_true", help="后台运行，关闭终端不影响服务")
    p_start.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    p_stop = sub.add_parser("stop", help="平滑停止：等待运行中的任务执行完")
    p_stop.add_argument("--force", action="store_true", help="取消运行中的任务并尽快退出")

    p_restart = sub.add_parser("restart", help="重启（先 stop 再 start）")
    p_restart.add_argument("-b", "--background", action="store_true", help="重启后后台运行")
    p_restart.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    return parser


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or args_list[0].startswith("-"):
        args_list.insert(0, "start")
    args = build_parser().parse_args(args_list)

    root = runtime.resolve_root()
    runtime.load_env(root)

    if args.command == "stop":
        return stop(root, force=args.force)
    if args.command == "restart":
        return restart(root, background=args.background, no_browser=args.no_browser)
    return start(root, background=args.background, no_browser=args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
