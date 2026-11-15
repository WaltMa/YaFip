"""启动器与服务端共用的运行时工具。

- 项目根目录解析（YAFIP_HOME 优先）
- .env 加载（真实环境变量优先，不覆盖）
- 端口解析（YAFIP_PORT > app_config.server.port > 8203）
- 运行信息文件 data/yafip.run.json（pid / port / url）
- 本机健康探测与停止请求（绕过系统代理）
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.request
from pathlib import Path

DEFAULT_PORT = 8203
RUN_INFO_FILE = "yafip.run.json"


def resolve_root() -> Path:
    """定位项目根目录：优先 YAFIP_HOME，其次包含 web/index.html 的目录。"""
    env = os.environ.get("YAFIP_HOME")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    candidates = [Path.cwd(), here.parents[2]]
    if len(here.parents) > 3:
        candidates.append(here.parents[3])
    for candidate in candidates:
        if (candidate / "web" / "index.html").exists():
            return candidate
    return here.parents[2]


def load_env(root: Path) -> None:
    """加载 .env；已存在的真实环境变量不被覆盖。"""
    path = Path(os.environ.get("YAFIP_ENV_FILE") or (Path(root) / ".env"))
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def read_config(root: Path, key: str, default: str | None = None) -> str | None:
    """直接读 SQLite 配置（供启动器在应用未起来时使用）。"""
    path = Path(root) / "data" / "yafip.db"
    if not path.exists():
        return default
    try:
        conn = sqlite3.connect(str(path), timeout=1.0)
        try:
            row = conn.execute("SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return default
    return str(row[0]) if row and row[0] is not None else default


def resolve_port(root: Path) -> int:
    env_port = (os.environ.get("YAFIP_PORT") or "").strip()
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            pass
    value = read_config(root, "server.port")
    try:
        return int(value) if value else DEFAULT_PORT
    except (TypeError, ValueError):
        return DEFAULT_PORT


def migrate_legacy_port(root: Path) -> None:
    """旧版本默认端口 8765 -> 8203；仅在值仍为旧默认时迁移一次。"""
    path = Path(root) / "data" / "yafip.db"
    if not path.exists():
        return
    try:
        from yafip.config import DEFAULTS, LEGACY_SERVER_PORT, PORT_MIGRATION_KEY
    except Exception:  # noqa: BLE001 - 迁移失败不阻塞启动
        return
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            migrated = conn.execute(
                "SELECT 1 FROM app_config WHERE key=?", (PORT_MIGRATION_KEY,)
            ).fetchone()
            if migrated is not None:
                return
            from datetime import datetime

            now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                "UPDATE app_config SET value=?, updated_at=? WHERE key='server.port' AND value=?",
                (DEFAULTS["server.port"], now, LEGACY_SERVER_PORT),
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_config(key, value, updated_at) VALUES(?,?,?)",
                (PORT_MIGRATION_KEY, "1", now),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


# -- 运行信息 ---------------------------------------------------------------
def run_info_path(root: Path) -> Path:
    return Path(root) / "data" / RUN_INFO_FILE


def write_run_info(root: Path, port: int) -> None:
    from datetime import datetime

    path = run_info_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "port": int(port),
        "url": f"http://127.0.0.1:{port}",
        "started_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_run_info(root: Path) -> dict:
    try:
        data = json.loads(run_info_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def clear_run_info(root: Path, pid: int | None = None) -> None:
    """删除运行信息；指定 pid 时只删除自己写入的那份。"""
    path = run_info_path(root)
    if pid is not None and read_run_info(root).get("pid") != pid:
        return
    try:
        path.unlink()
    except OSError:
        pass


# -- 本机 HTTP --------------------------------------------------------------
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health(port: int, timeout: float = 1.5) -> dict | None:
    """返回 /api/system/health 的 data；不可达返回 None。"""
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/api/system/health", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 探测失败即视为未运行
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def request_stop(port: int, force: bool = False, timeout: float = 10.0) -> dict:
    """调用平滑停止接口，返回接口 data。"""
    url = f"http://127.0.0.1:{port}/api/system/stop"
    if force:
        url += "?force=true"
    request = urllib.request.Request(url, data=b"", method="POST")
    with _OPENER.open(request, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}
