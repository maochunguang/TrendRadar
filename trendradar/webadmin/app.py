# coding=utf-8
"""
Web 设置中心应用组装

- /settings        设置页面（单页应用）
- /                托管 output/ 静态报告（保持原 8080 行为，但屏蔽 logs/ 与 meta/）
- /api/*           REST API（鉴权中间件保护）

鉴权设计（fail-closed）：
- ADMIN_TOKEN 已设置：所有 /api 请求需携带 X-Admin-Token 头，
  连续失败 5 次后该 IP 冷却 10 分钟
- 未设置：GET 放行（页面只读模式），写方法一律 403
"""

import hmac
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from trendradar.webadmin import api
from trendradar.webadmin.config_io import get_output_dir

STATIC_DIR = Path(__file__).parent / "static"

# 静态托管中被屏蔽的目录（运行日志、doctor 报告等含配置信息）
BLOCKED_OUTPUT_DIRS = {"logs", "meta"}

# 鉴权失败限制
AUTH_FAIL_LIMIT = 5
AUTH_COOLDOWN_SECONDS = 600


class OutputStaticFiles(StaticFiles):
    """output 目录静态托管，屏蔽 logs/、meta/ 等敏感子目录"""

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        parts = Path(path).parts
        if parts and (parts[0] in BLOCKED_OUTPUT_DIRS or ".." in parts):
            return JSONResponse({"error": "Not Found"}, status_code=404)
        return await super().get_response(path, scope)


class AdminAuthMiddleware(BaseHTTPMiddleware):
    """
    /api 鉴权中间件

    ADMIN_TOKEN 已设置时校验 X-Admin-Token；未设置时写方法返回 403。
    """

    def __init__(self, app: Any):
        super().__init__(app)
        self._fail_times: Dict[str, list] = {}

    def _is_rate_limited(self, ip: str) -> bool:
        """连续失败超过限制后冷却"""
        now = time.time()
        fails = [t for t in self._fail_times.get(ip, []) if now - t < AUTH_COOLDOWN_SECONDS]
        self._fail_times[ip] = fails
        return len(fails) >= AUTH_FAIL_LIMIT

    def _record_fail(self, ip: str) -> None:
        self._fail_times.setdefault(ip, []).append(time.time())

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if not path.startswith("/api"):
            return await call_next(request)

        token = os.environ.get("ADMIN_TOKEN", "").strip()
        ip = request.client.host if request.client else "unknown"

        if not token:
            # fail-closed：未配置 token 时，只允许只读请求
            if request.method in ("GET", "HEAD", "OPTIONS"):
                return await call_next(request)
            return JSONResponse(
                {"error": "未设置 ADMIN_TOKEN，写入功能已禁用。请在 docker/.env 中配置 ADMIN_TOKEN 后重启容器"},
                status_code=403,
            )

        provided = request.headers.get("X-Admin-Token", "")
        if hmac.compare_digest(provided, token):
            return await call_next(request)

        if self._is_rate_limited(ip):
            return JSONResponse(
                {"error": "认证失败次数过多，请 10 分钟后再试"}, status_code=429
            )
        self._record_fail(ip)
        return JSONResponse({"error": "认证失败：token 无效"}, status_code=401)


async def settings_page(request: Request) -> Response:
    """设置页面入口"""
    response = FileResponse(STATIC_DIR / "index.html")
    response.headers["Cache-Control"] = "no-cache"  # 每次协商校验，避免升级后使用旧页面
    return response


async def settings_asset(request: Request) -> Response:
    """设置页面静态资源（app.js / style.css）"""
    name = request.path_params["name"]
    if name not in ("app.js", "style.css"):
        return JSONResponse({"error": "Not Found"}, status_code=404)
    response = FileResponse(STATIC_DIR / name)
    response.headers["Cache-Control"] = "no-cache"
    return response


async def api_not_found(request: Request) -> JSONResponse:
    return JSONResponse({"error": "接口不存在"}, status_code=404)


def create_app() -> Starlette:
    """组装 Starlette 应用"""
    output_dir = get_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    routes = [
        Route("/settings", settings_page),
        Route("/settings/{name}", settings_asset),
        Route("/api/status", api.get_status, methods=["GET"]),
        Route("/api/config/structured", api.get_config_structured, methods=["GET"]),
        Route("/api/config/structured", api.put_config_structured, methods=["PUT"]),
        Route("/api/config/raw", api.get_config_raw, methods=["GET"]),
        Route("/api/config/raw", api.put_config_raw, methods=["PUT"]),
        Route("/api/file/{name:path}", api.get_file, methods=["GET"]),
        Route("/api/file/{name:path}", api.put_file, methods=["PUT"]),
        Route("/api/run", api.post_run, methods=["POST"]),
        Route("/api/run/status", api.get_run_status, methods=["GET"]),
        Route("/api/run/logs", api.get_run_logs, methods=["GET"]),
        Route("/api/run/logs/{name}", api.get_run_log_detail, methods=["GET"]),
        Route("/api/test-notification", api.post_test_notification, methods=["POST"]),
        Route("/api/{rest:path}", api_not_found, methods=["GET", "POST", "PUT", "DELETE"]),
        Mount("/", app=OutputStaticFiles(directory=str(output_dir), html=True, check_dir=False)),
    ]

    app = Starlette(routes=routes)
    app.add_middleware(AdminAuthMiddleware)
    return app


def serve() -> None:
    """启动 Web 设置中心（python -m trendradar.webadmin）"""
    import uvicorn

    port = int(os.environ.get("WEBSERVER_PORT", "8080"))
    if not 1 <= port <= 65535:
        port = 8080
    host = os.environ.get("WEBADMIN_HOST", "0.0.0.0")

    auth = "已启用" if os.environ.get("ADMIN_TOKEN", "").strip() else "未设置（页面只读模式）"
    print(f"🌐 Web 设置中心启动: http://localhost:{port}/settings")
    print(f"  🔒 ADMIN_TOKEN: {auth}")
    print(f"  📁 报告托管: {get_output_dir().resolve()}（logs/ 与 meta/ 已屏蔽）")

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
