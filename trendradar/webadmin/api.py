# coding=utf-8
"""
Web 设置中心 API 端点

所有端点由 app.py 的鉴权中间件保护：
- ADMIN_TOKEN 已设置：需要 X-Admin-Token 请求头
- 未设置：GET 允许（页面只读），写方法返回 403

端点约定：成功返回 JSON，失败返回 {"error": "..."}，HTTP 状态码区分错误类型。
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

from trendradar.webadmin import config_io, runner
from trendradar.webadmin.frequency_editor import (
    parse_frequency,
    serialize_frequency,
    validate_frequency,
)
from trendradar.webadmin.opml import OpmlError, make_feed_id, parse_opml

# 主程序版本（从 pyproject 读取，读取失败不影响服务）
try:
    import tomllib
    _pyproject = Path(__file__).parent.parent.parent / "pyproject.toml"
    with open(_pyproject, "rb") as f:
        APP_VERSION = tomllib.load(f).get("project", {}).get("version", "unknown")
except Exception:
    APP_VERSION = "unknown"


def _json_ok(data: Dict[str, Any] = None) -> JSONResponse:
    return JSONResponse(data or {})


def _json_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _read_json_body(request: Request) -> Dict[str, Any]:
    """解析 JSON 请求体，非法时抛出 ValueError"""
    try:
        body = await request.body()
        data = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"请求体不是合法 JSON: {e}")
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


# ═══════════════════════════════════════════════════════
# 状态
# ═══════════════════════════════════════════════════════


async def get_status(request: Request) -> JSONResponse:
    """运行状态概览（页面首屏）"""
    config_path = Path(config_io.get_config_path())

    # 各配置文件信息
    config_files = []
    for name, label in [
        (config_path.name, "主配置"),
        ("frequency_words.txt", "热点关键词"),
        ("timeline.yaml", "调度时间线"),
    ]:
        path = (config_path.parent / name) if name != "frequency_words.txt" else Path(config_io.get_frequency_path())
        if path.exists():
            stat = path.stat()
            config_files.append({
                "name": name,
                "label": label,
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            })

    logs = runner.list_run_logs(limit=1)

    # supercronic 是否为 PID 1（仅 Linux 容器内可判断）
    supercronic_pid1 = False
    try:
        with open("/proc/1/cmdline", "rb") as f:
            supercronic_pid1 = b"supercronic" in f.read()
    except OSError:
        pass

    return _json_ok({
        "version": APP_VERSION,
        "cron_schedule": os.environ.get("CRON_SCHEDULE", ""),
        "run_mode": os.environ.get("RUN_MODE", "cron"),
        "immediate_run": os.environ.get("IMMEDIATE_RUN", ""),
        "supercronic_pid1": supercronic_pid1,
        "admin_auth_enabled": bool(os.environ.get("ADMIN_TOKEN", "").strip()),
        "secrets_editable": config_io.secrets_editable(),
        "config_files": config_files,
        "last_run_log": logs[0]["name"] if logs else None,
        "last_run_time": logs[0]["mtime"] if logs else None,
        "running": runner.is_run_in_progress(),
        "env_shadowed": config_io.get_env_shadowed(),
        "text_files": config_io.list_whitelisted_files(),
    })


# ═══════════════════════════════════════════════════════
# config.yaml 读写
# ═══════════════════════════════════════════════════════


async def get_config_structured(request: Request) -> JSONResponse:
    """结构化配置（密钥已脱敏，附 env 覆盖信息）"""
    try:
        return _json_ok(config_io.get_structured_config())
    except Exception as e:
        return _json_error(f"读取配置失败: {e}", 500)


async def put_config_structured(request: Request) -> JSONResponse:
    """保存结构化配置"""
    try:
        body = await _read_json_body(request)
    except ValueError as e:
        return _json_error(str(e))

    data = body.get("data")
    if not isinstance(data, dict):
        return _json_error("缺少 data 字段（完整 YAML 结构的对象）")

    ok, errors, warnings, changed = config_io.save_structured_config(
        data, source_ip=request.client.host if request.client else ""
    )
    if not ok:
        return JSONResponse({"errors": errors, "warnings": warnings}, status_code=422)
    return _json_ok({"message": "配置已保存", "warnings": warnings, "changed": changed})


async def get_config_raw(request: Request) -> JSONResponse:
    """config.yaml 原文（密钥已脱敏）"""
    try:
        return _json_ok({"text": config_io.get_config_raw()})
    except Exception as e:
        return _json_error(f"读取配置失败: {e}", 500)


async def put_config_raw(request: Request) -> JSONResponse:
    """保存 config.yaml 原文"""
    try:
        body = await _read_json_body(request)
    except ValueError as e:
        return _json_error(str(e))

    text = body.get("text")
    if not isinstance(text, str):
        return _json_error("缺少 text 字段（YAML 文本）")

    ok, errors, warnings = config_io.save_config_raw(
        text, source_ip=request.client.host if request.client else ""
    )
    if not ok:
        return JSONResponse({"errors": errors, "warnings": warnings}, status_code=422)
    return _json_ok({"message": "配置已保存", "warnings": warnings})


# ═══════════════════════════════════════════════════════
# 白名单文本文件读写
# ═══════════════════════════════════════════════════════


async def get_file(request: Request) -> JSONResponse:
    """读取白名单文本文件"""
    name = request.path_params["name"]
    path = config_io._resolve_whitelisted(name)
    if path is None:
        return _json_error("文件不在允许列表内", 403)
    if not path.exists():
        return _json_error(f"文件不存在: {name}", 404)
    try:
        return _json_ok({"name": name, "text": config_io.read_text_file(path)})
    except Exception as e:
        return _json_error(f"读取失败: {e}", 500)


async def put_file(request: Request) -> JSONResponse:
    """保存白名单文本文件"""
    name = request.path_params["name"]
    path = config_io._resolve_whitelisted(name)
    if path is None:
        return _json_error("文件不在允许列表内", 403)

    try:
        body = await _read_json_body(request)
    except ValueError as e:
        return _json_error(str(e))

    text = body.get("text")
    if not isinstance(text, str):
        return _json_error("缺少 text 字段")

    # timeline.yaml 保存前校验语法
    warnings = []
    if name.endswith((".yaml", ".yml")):
        import yaml as _yaml
        try:
            data = _yaml.safe_load(text)
            if data is not None and not isinstance(data, dict):
                return _json_error("YAML 顶层结构必须是键值映射", 422)
        except _yaml.YAMLError as e:
            return _json_error(f"YAML 语法错误: {e}", 422)

    try:
        result = config_io.write_text_file(
            path, text, source_ip=request.client.host if request.client else ""
        )
    except OSError as e:
        return _json_error(f"写入失败: {e}", 500)

    return _json_ok({"message": "文件已保存", **result, "warnings": warnings})


# ═══════════════════════════════════════════════════════
# 关键词词组结构化编辑
# ═══════════════════════════════════════════════════════


async def get_frequency_structured(request: Request) -> JSONResponse:
    """解析 frequency_words.txt 为结构化词组模型"""
    path = config_io._resolve_whitelisted("frequency_words.txt")
    if path is None or not path.exists():
        return _json_error("frequency_words.txt 不存在", 404)
    try:
        model = parse_frequency(config_io.read_text_file(path))
        return _json_ok(model)
    except ValueError as e:
        return _json_error(str(e), 422)
    except Exception as e:
        return _json_error(f"解析失败: {e}", 500)


async def put_frequency_structured(request: Request) -> JSONResponse:
    """保存结构化词组模型（序列化回文本 + 校验 + 备份 + 审计）"""
    try:
        body = await _read_json_body(request)
    except ValueError as e:
        return _json_error(str(e))

    model = body.get("model")
    if not isinstance(model, dict):
        return _json_error("缺少 model 字段（结构化词组对象）")

    errors = validate_frequency(model)
    if errors:
        return JSONResponse({"errors": errors}, status_code=422)

    path = config_io._resolve_whitelisted("frequency_words.txt")
    if path is None:
        return _json_error("文件不在允许列表内", 403)

    # 先序列化，并用主程序解析器在临时文件上验证（不落盘正式文件）
    try:
        text = serialize_frequency(model)
    except Exception as e:
        return _json_error(f"序列化失败: {e}", 500)

    import os as _os
    import tempfile as _tempfile
    tmp_name = ""
    try:
        with _tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(text)
            tmp_name = tmp.name
        from trendradar.core.frequency import load_frequency_words
        groups, filter_words, global_filters = load_frequency_words(tmp_name)
        print(f"[设置中心] 词组保存前验证: {len(groups)} 个词组, {len(filter_words)} 个过滤词, {len(global_filters)} 个全局过滤词")
    except Exception as e:
        return JSONResponse(
            {"errors": [f"序列化结果无法被主程序解析，请检查内容: {e}"]}, status_code=422
        )
    finally:
        if tmp_name:
            try:
                _os.unlink(tmp_name)
            except OSError:
                pass

    try:
        result = config_io.write_text_file(
            path, text, source_ip=request.client.host if request.client else ""
        )
    except OSError as e:
        return _json_error(f"写入失败: {e}", 500)

    return _json_ok({
        "message": "关键词已保存",
        "groups": len(groups),
        **result,
    })


# ═══════════════════════════════════════════════════════
# OPML 导入
# ═══════════════════════════════════════════════════════


async def post_rss_opml(request: Request) -> JSONResponse:
    """从 OPML 内容导入 RSS 订阅（按 URL 去重，追加到 rss.feeds）"""
    try:
        body = await _read_json_body(request)
    except ValueError as e:
        return _json_error(str(e))

    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return _json_error("缺少 content 字段（OPML 文本）")

    try:
        feeds = parse_opml(content)
    except OpmlError as e:
        return _json_error(str(e), 422)

    try:
        structured = config_io.get_structured_config()
        data = structured["data"]
        rss_section = data.get("rss") or {}
        existing_feeds = rss_section.get("feeds") or []
    except Exception as e:
        return _json_error(f"读取配置失败: {e}", 500)

    existing_urls = {str(f.get("url", "")).strip().rstrip("/") for f in existing_feeds if isinstance(f, dict)}
    existing_ids = [str(f.get("id", "")) for f in existing_feeds if isinstance(f, dict)]

    added: List[Dict[str, Any]] = []
    skipped = 0
    for feed in feeds:
        url_key = feed["url"].strip().rstrip("/")
        if url_key in existing_urls:
            skipped += 1
            continue
        existing_ids.append(make_feed_id(feed["name"], existing_ids))
        new_id = existing_ids[-1]
        added.append({"id": new_id, "name": feed["name"], "url": feed["url"], "enabled": True})
        existing_urls.add(url_key)

    if not added:
        return _json_ok({
            "message": f"没有新增订阅（{skipped} 条已存在）",
            "added": 0,
            "skipped": skipped,
        })

    rss_section["feeds"] = existing_feeds + added
    data["rss"] = rss_section

    ok, errors, warnings, _ = config_io.save_structured_config(
        data, source_ip=request.client.host if request.client else ""
    )
    if not ok:
        return JSONResponse({"errors": errors, "warnings": warnings}, status_code=422)

    return _json_ok({
        "message": f"已导入 {len(added)} 个 RSS 源（跳过已存在 {skipped} 个）",
        "added": len(added),
        "skipped": skipped,
        "warnings": warnings,
    })


# ═══════════════════════════════════════════════════════
# 任务运行
# ═══════════════════════════════════════════════════════


async def post_run(request: Request) -> JSONResponse:
    """立即运行一次主程序"""
    result = runner.start_run(source_ip=request.client.host if request.client else "")
    return _json_ok(result) if result["ok"] else _json_error(result["message"], 409)


async def get_run_status(request: Request) -> JSONResponse:
    """运行状态 + 最新日志尾部"""
    return _json_ok(runner.run_status())


async def get_run_logs(request: Request) -> JSONResponse:
    """列出运行日志"""
    return _json_ok({"logs": runner.list_run_logs()})


async def get_run_log_detail(request: Request) -> JSONResponse:
    """读取指定运行日志尾部"""
    name = request.path_params["name"]
    text = runner.read_run_log(name)
    if not text:
        return _json_error("日志不存在或为空", 404)
    return _json_ok({"name": name, "text": text})


async def post_test_notification(request: Request) -> JSONResponse:
    """发送测试通知（同步执行）"""
    result = runner.run_test_notification()
    return _json_ok(result) if result["ok"] else JSONResponse(result, status_code=422)
