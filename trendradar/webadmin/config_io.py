# coding=utf-8
"""
配置读写核心模块

负责 Web 设置中心的配置文件读写：
- ruamel.yaml round-trip 读写（保留注释）
- 写入前校验、自动备份、审计日志
- 字段分类：密钥字段（默认只读+脱敏，EDIT_SECRETS=true 后可编辑）、环境变量覆盖字段、URL 字段
- 原始 YAML 文本的密钥脱敏与还原

安全设计：
- 密钥类字段（webhook/token/AI key/S3 等）默认不允许通过网页修改：
  GET 时以掩码占位符返回，PUT 时仅接受"保持原值"
- 设置环境变量 EDIT_SECRETS=true 后开放密钥编辑（写入 config.yaml），
  适用于仅在 127.0.0.1/SSH 隧道等受控网络访问设置中心的部署
- 无论是否开放编辑，环境变量覆盖的字段修改都会被忽略并返回提示
  （Docker 下 env 优先于 config.yaml）
"""

import copy
import io
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from trendradar.core.config import parse_multi_account_config, validate_paired_configs

# ═══════════════════════════════════════════════════════
# 路径解析
# ═══════════════════════════════════════════════════════

MASK_TOKEN = "__TR_MASKED__"

# 每次保存保留的备份份数
MAX_BACKUPS = 5


def get_config_path() -> str:
    """获取 config.yaml 路径（与主程序 loader 一致）"""
    return os.environ.get("CONFIG_PATH", "config/config.yaml")


def get_frequency_path() -> str:
    """获取 frequency_words.txt 路径（与主程序一致）"""
    return os.environ.get("FREQUENCY_WORDS_PATH", "config/frequency_words.txt")


def get_config_dir() -> Path:
    """获取配置目录"""
    return Path(get_config_path()).parent


def get_output_dir() -> Path:
    """获取输出目录（与主程序 storage.local.data_dir 默认值一致）"""
    return Path("output")


def get_logs_dir() -> Path:
    """获取日志目录"""
    return get_output_dir() / "logs"


# ═══════════════════════════════════════════════════════
# 字段分类
# ═══════════════════════════════════════════════════════

# 密钥字段（YAML 路径）：只读 + 脱敏，不允许通过网页修改
# 设计依据：这些凭据应通过 docker/.env 环境变量管理（与 docker-compose.yml 的 env 一致），
# 页面展示掩码，修改会被服务端拒绝
CREDENTIAL_FIELDS = [
    "ai.api_key",
    "notification.channels.feishu.webhook_url",
    "notification.channels.dingtalk.webhook_url",
    "notification.channels.wework.webhook_url",
    "notification.channels.telegram.bot_token",
    "notification.channels.telegram.chat_id",
    "notification.channels.email.password",
    "notification.channels.ntfy.token",
    "notification.channels.bark.url",
    "notification.channels.slack.webhook_url",
    "notification.channels.generic_webhook.webhook_url",
    "storage.remote.access_key_id",
    "storage.remote.secret_access_key",
]

# 环境变量覆盖映射（YAML 路径 → 环境变量名）
# 与 trendradar/core/loader.py 的实际覆盖逻辑保持一致（env 非空时覆盖 config.yaml）
ENV_OVERRIDE_MAP = {
    "app.timezone": "TIMEZONE",
    "advanced.debug": "DEBUG",
    "advanced.max_accounts_per_channel": "MAX_ACCOUNTS_PER_CHANNEL",
    "platforms.api_url": "PLATFORMS_API_URL",
    "report.sort_by_position_first": "SORT_BY_POSITION_FIRST",
    "report.max_news_per_keyword": "MAX_NEWS_PER_KEYWORD",
    "schedule.enabled": "SCHEDULE_ENABLED",
    "schedule.preset": "SCHEDULE_PRESET",
    "filter.method": "AI_FILTER_ENABLED",
    "ai.model": "AI_MODEL",
    "ai.api_key": "AI_API_KEY",
    "ai.api_base": "AI_API_BASE",
    "ai.timeout": "AI_TIMEOUT",
    "ai_analysis.enabled": "AI_ANALYSIS_ENABLED",
    "ai_translation.enabled": "AI_TRANSLATION_ENABLED",
    "ai_translation.language": "AI_TRANSLATION_LANGUAGE",
    "storage.backend": "STORAGE_BACKEND",
    "storage.formats.txt": "STORAGE_TXT_ENABLED",
    "storage.formats.html": "STORAGE_HTML_ENABLED",
    "storage.local.retention_days": "LOCAL_RETENTION_DAYS",
    "storage.remote.retention_days": "REMOTE_RETENTION_DAYS",
    "storage.remote.endpoint_url": "S3_ENDPOINT_URL",
    "storage.remote.bucket_name": "S3_BUCKET_NAME",
    "storage.remote.access_key_id": "S3_ACCESS_KEY_ID",
    "storage.remote.secret_access_key": "S3_SECRET_ACCESS_KEY",
    "storage.remote.region": "S3_REGION",
    "storage.pull.enabled": "PULL_ENABLED",
    "storage.pull.days": "PULL_DAYS",
    "notification.channels.feishu.webhook_url": "FEISHU_WEBHOOK_URL",
    "notification.channels.dingtalk.webhook_url": "DINGTALK_WEBHOOK_URL",
    "notification.channels.wework.webhook_url": "WEWORK_WEBHOOK_URL",
    "notification.channels.wework.msg_type": "WEWORK_MSG_TYPE",
    "notification.channels.telegram.bot_token": "TELEGRAM_BOT_TOKEN",
    "notification.channels.telegram.chat_id": "TELEGRAM_CHAT_ID",
    "notification.channels.email.from": "EMAIL_FROM",
    "notification.channels.email.password": "EMAIL_PASSWORD",
    "notification.channels.email.to": "EMAIL_TO",
    "notification.channels.email.smtp_server": "EMAIL_SMTP_SERVER",
    "notification.channels.email.smtp_port": "EMAIL_SMTP_PORT",
    "notification.channels.ntfy.server_url": "NTFY_SERVER_URL",
    "notification.channels.ntfy.topic": "NTFY_TOPIC",
    "notification.channels.ntfy.token": "NTFY_TOKEN",
    "notification.channels.bark.url": "BARK_URL",
    "notification.channels.slack.webhook_url": "SLACK_WEBHOOK_URL",
    "notification.channels.generic_webhook.webhook_url": "GENERIC_WEBHOOK_URL",
    "notification.channels.generic_webhook.payload_template": "GENERIC_WEBHOOK_TEMPLATE",
}

# URL 类字段：保存时校验协议，内网地址给出警告
URL_FIELDS = {
    "platforms.api_url": "热榜数据 API 地址",
    "ai.api_base": "AI API 地址",
    "storage.remote.endpoint_url": "远程存储端点",
    "notification.channels.ntfy.server_url": "ntfy 服务器地址",
}


def get_env_shadowed() -> Dict[str, str]:
    """返回当前被环境变量覆盖的配置路径 → 环境变量名"""
    result = {}
    for path, env_name in ENV_OVERRIDE_MAP.items():
        if os.environ.get(env_name, "").strip():
            result[path] = env_name
    return result


def secrets_editable() -> bool:
    """密钥字段是否允许通过网页编辑（由 EDIT_SECRETS 环境变量控制）"""
    return os.environ.get("EDIT_SECRETS", "").strip().lower() in ("1", "true", "yes", "on")


# ═══════════════════════════════════════════════════════
# 白名单文件
# ═══════════════════════════════════════════════════════

# 安全文件名模式（用于 ai_filter/ 等子目录内的提示词文件）
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _resolve_whitelisted(name: str) -> Optional[Path]:
    """
    将文件名解析为白名单内的实际路径，防止路径穿越

    允许：
    - frequency_words.txt（FREQUENCY_WORDS_PATH）
    - timeline.yaml / timeline.en.yaml
    - config 目录下的 *.txt（AI 提示词等）
    - ai_filter/*.txt

    不允许：任何包含 ..、以 / 或 \\ 开头的路径
    """
    if not name or name.startswith(("/", "\\")) or ".." in name:
        return None

    config_dir = get_config_dir()

    if name == "frequency_words.txt":
        return Path(get_frequency_path())

    parts = name.split("/")
    if any(not _SAFE_FILENAME_RE.match(p) for p in parts):
        return None

    candidate = (config_dir / name).resolve()
    # 双重校验：解析后必须仍在配置目录内
    try:
        candidate.relative_to(config_dir.resolve())
    except ValueError:
        return None

    return candidate


def list_whitelisted_files() -> List[Dict[str, Any]]:
    """列出白名单内实际存在的文本文件（供前端展示）"""
    known = [
        ("frequency_words.txt", "热点关键词"),
        ("timeline.yaml", "调度时间线"),
        ("ai_analysis_prompt.txt", "AI 分析提示词"),
        ("ai_interests.txt", "AI 兴趣描述"),
        ("ai_translation_prompt.txt", "AI 翻译提示词"),
    ]
    files = []
    config_dir = get_config_dir()
    for name, label in known:
        path = _resolve_whitelisted(name)
        if path and path.exists():
            files.append({"name": name, "label": label})

    ai_filter_dir = config_dir / "ai_filter"
    if ai_filter_dir.is_dir():
        for p in sorted(ai_filter_dir.glob("*.txt")):
            files.append({"name": f"ai_filter/{p.name}", "label": f"AI 筛选提示词（{p.name}）"})

    return files


# ═══════════════════════════════════════════════════════
# 基础读写
# ═══════════════════════════════════════════════════════


def read_text_file(path: Path) -> str:
    """读取文本文件"""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text_file(path: Path, content: str, source_ip: str = "") -> Dict[str, Any]:
    """写入文本文件（先备份再原子写入，返回审计信息）"""
    changed = path.exists() and read_text_file(path) != content
    backup_path = _backup_file(path) if changed else None
    _atomic_write(path, content)
    if changed:
        _write_audit(source_ip, path.name, action="update", summary="文本文件更新")
    return {"backup": str(backup_path) if backup_path else None, "changed": changed}


def _atomic_write(path: Path, content: str) -> None:
    """原子写入：先写临时文件再替换，避免写一半被 cron 任务读到"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.replace(tmp, path)


def _backup_file(path: Path) -> Optional[Path]:
    """备份文件到 <name>.bak-<时间戳>，保留最近 MAX_BACKUPS 份"""
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{ts}")
    shutil.copy2(path, backup)

    # 清理旧备份
    pattern = f"{path.name}.bak-*"
    backups = sorted(path.parent.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[MAX_BACKUPS:]:
        try:
            old.unlink()
        except OSError:
            pass
    return backup


def _write_audit(source_ip: str, file_name: str, action: str, summary: str) -> None:
    """追加审计日志（output/logs/config-audit.jsonl，静态托管已屏蔽该目录）"""
    try:
        logs_dir = get_logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ip": source_ip or "-",
            "file": file_name,
            "action": action,
            "summary": summary,
        }
        with open(logs_dir / "config-audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # 审计日志失败不阻断配置保存
        pass


# ═══════════════════════════════════════════════════════
# config.yaml 读写（round-trip 保留注释）
# ═══════════════════════════════════════════════════════


def _make_ruamel_yaml() -> YAML:
    """创建 round-trip 模式的 YAML 实例（保留注释与格式）"""
    yml = YAML()
    yml.preserve_quotes = True
    # 匹配 config.yaml 的缩进风格；width 放宽避免长 URL 被折行
    yml.indent(mapping=2, sequence=4, offset=2)
    yml.width = 4096
    return yml


def load_config_ruamel() -> CommentedMap:
    """以 round-trip 模式加载 config.yaml（保留注释结构）"""
    yml = _make_ruamel_yaml()
    with open(get_config_path(), "r", encoding="utf-8") as f:
        data = yml.load(f)
    if not isinstance(data, CommentedMap):
        raise ValueError("config.yaml 顶层结构必须是键值映射")
    return data


def get_structured_config() -> Dict[str, Any]:
    """
    获取结构化配置（YAML 原始形状）

    默认对密钥字段脱敏；EDIT_SECRETS=true 时返回真实值且不设只读限制。

    返回:
        {
            "data": {...YAML 内容...},
            "env_shadowed": {路径: 环境变量名},
            "readonly_fields": [密钥字段路径]（开放编辑时为空）,
            "secrets_editable": bool,
        }
    """
    data = _to_plain(copy.deepcopy(load_config_ruamel()))
    editable = secrets_editable()
    readonly: List[str] = []
    if not editable:
        data = _mask_credentials(data)
        readonly = list(CREDENTIAL_FIELDS)
    return {
        "data": data,
        "env_shadowed": get_env_shadowed(),
        "readonly_fields": readonly,
        "secrets_editable": editable,
    }


def _to_plain(value: Any) -> Any:
    """将 ruamel 结构转为普通 dict/list（便于 JSON 序列化）"""
    if isinstance(value, CommentedMap):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, CommentedSeq):
        return [_to_plain(v) for v in value]
    return value


def _get_by_path(data: Any, path: str) -> Any:
    """按点分路径取值，不存在返回 None"""
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _mask_credentials(data: Dict[str, Any]) -> Dict[str, Any]:
    """将密钥字段值替换为掩码占位符（空值保持为空）"""
    for path in CREDENTIAL_FIELDS:
        value = _get_by_path(data, path)
        if isinstance(value, str) and value.strip():
            _set_by_path(data, path, MASK_TOKEN)
    return data


def _set_by_path(data: Dict[str, Any], path: str, value: Any) -> None:
    """按点分路径设置值（中间节点不存在时跳过）"""
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict):
        cur[parts[-1]] = value


def save_structured_config(
    new_data: Dict[str, Any], source_ip: str = ""
) -> Tuple[bool, List[str], List[str], List[Dict[str, Any]]]:
    """
    保存结构化配置（完整 YAML 形状的 dict）

    流程：掩码还原 → 密钥字段防护 → env 覆盖字段防护 → 校验 →
          round-trip 合并（保留注释）→ 备份 → 原子写入 → 审计

    Returns:
        (是否成功, errors, warnings, 变更列表 [{path, old, new}])
    """
    errors: List[str] = []
    warnings: List[str] = []
    changed: List[Dict[str, Any]] = []

    try:
        current = load_config_ruamel()
    except Exception as e:
        return False, [f"读取当前配置失败: {e}"], [], []

    plain_current = _to_plain(copy.deepcopy(current))
    editable = secrets_editable()

    # 1. 掩码还原：提交数据中的 MASK_TOKEN 一律还原为当前实际值
    #    （默认模式下密钥只读；EDIT_SECRETS 模式下防止旧页面残留掩码被写入）
    for path in CREDENTIAL_FIELDS:
        value = _get_by_path(new_data, path)
        if isinstance(value, str) and MASK_TOKEN in value:
            _set_by_path(new_data, path, _get_by_path(plain_current, path))

    if not editable:
        # 2a. 密钥字段防护（默认模式）：留空也还原为原值，任何实际改动都被拒绝
        for path in CREDENTIAL_FIELDS:
            new_value = _get_by_path(new_data, path)
            old_value = _get_by_path(plain_current, path)
            if isinstance(new_value, str) and not new_value.strip():
                _set_by_path(new_data, path, old_value)
                new_value = old_value
            if new_value != old_value:
                errors.append(f"密钥字段 {path} 不允许通过网页修改，请使用环境变量管理")
    else:
        # 2b. 可编辑模式：留空视为删除该值（与手改 config.yaml 等效），允许修改
        pass

    if errors:
        return False, errors, warnings, changed

    # 3. env 覆盖字段防护：被 env 覆盖的字段，改动会被忽略
    shadowed = get_env_shadowed()
    for path, env_name in shadowed.items():
        new_value = _get_by_path(new_data, path)
        old_value = _get_by_path(plain_current, path)
        if new_value != old_value:
            _set_by_path(new_data, path, old_value)
            warnings.append(f"{path} 当前由环境变量 {env_name} 生效，本次修改已忽略")

    # 4. 校验
    errors, validate_warnings = validate_config_data(new_data)
    warnings.extend(validate_warnings)
    if errors:
        return False, errors, warnings, changed

    # 5. 计算变更（基于还原后的数据）
    changed = _diff_data(plain_current, new_data)

    # 6. round-trip 合并写入（保留注释）
    try:
        _merge_into_commented_tree(current, new_data)

        yml = _make_ruamel_yaml()
        buf = io.StringIO()
        yml.dump(current, buf)

        config_path = Path(get_config_path())
        backup_path = _backup_file(config_path)
        _atomic_write(config_path, buf.getvalue())
    except Exception as e:
        return False, [f"写入配置失败: {e}"], warnings, changed

    # 7. 审计（密钥字段的值不写入审计日志）
    def _audit_entry(c: Dict[str, Any]) -> str:
        if c["path"] in CREDENTIAL_FIELDS:
            return f"{c['path']}: ***（密钥已更新）"
        return f"{c['path']}: {c['summary']}"

    summary = "; ".join(_audit_entry(c) for c in changed[:10])
    if len(changed) > 10:
        summary += f" 等 {len(changed)} 处"
    _write_audit(source_ip, config_path.name, action="update", summary=summary or "无变化")

    return True, [], warnings, changed


def _merge_into_commented_tree(target: Any, source: Any) -> None:
    """
    将普通 dict/list 合并进 ruamel round-trip 树，尽量保留注释

    - dict：递归合并，标量直接赋值（key 的注释由 ruamel 保留）
    - 带 id 字段的列表：按 id 匹配原地更新/删除/追加，保留已有条目的注释
    - 其他列表：整体替换（条目内注释可能丢失）
    """
    if isinstance(target, CommentedMap) and isinstance(source, dict):
        for key, value in source.items():
            if key not in target:
                target[key] = value
                continue
            existing = target[key]
            if isinstance(existing, CommentedMap) and isinstance(value, dict):
                _merge_into_commented_tree(existing, value)
            elif isinstance(existing, CommentedSeq) and isinstance(value, list):
                _merge_commented_seq(existing, value)
            else:
                target[key] = value
        return

    if isinstance(target, CommentedSeq) and isinstance(source, list):
        _merge_commented_seq(target, source)
        return

    raise ValueError("配置结构不兼容，无法合并")


def _merge_commented_seq(target: CommentedSeq, source: List[Any]) -> None:
    """
    合并列表：带 id 的字典列表按 id 原地增删改以保留注释

    注意：不要重建整个 CommentedSeq（slice 清空后逐个 append 会破坏
    ruamel 内部的注释索引，导致序列化时条目错位），必须原地操作。
    """
    if (
        target
        and all(isinstance(item, CommentedMap) for item in target)
        and source
        and all(isinstance(item, dict) for item in source)
        and all("id" in item for item in source)
    ):
        source_by_id = {item.get("id"): item for item in source}

        # 删除：target 中不在 source 里的项（从后往前，避免索引位移）
        for idx in range(len(target) - 1, -1, -1):
            if target[idx].get("id") not in source_by_id:
                del target[idx]

        # 更新已有项 + 追加新项（保持 target 现有顺序，新项追加到尾部）
        for item in source:
            matched = next(
                (existing for existing in target if existing.get("id") == item.get("id")),
                None,
            )
            if matched is not None:
                _merge_into_commented_tree(matched, item)
            else:
                target.append(item)
        return

    # 无 id 的列表：内容完全一致时跳过重写，避免丢失列表项的行内注释
    if len(target) == len(source) and all(target[i] == source[i] for i in range(len(source))):
        return

    target[:] = source


# ═══════════════════════════════════════════════════════
# 原始 YAML 文本读写（脱敏）
# ═══════════════════════════════════════════════════════


def get_config_raw() -> str:
    """获取 config.yaml 原文；默认模式下密钥字段值替换为掩码，EDIT_SECRETS 模式返回原文"""
    text = read_text_file(Path(get_config_path()))
    if secrets_editable():
        return text
    try:
        data = yaml.safe_load(text) or {}
        for path in CREDENTIAL_FIELDS:
            value = _get_by_path(data, path)
            if isinstance(value, str) and value.strip():
                text = text.replace(value, MASK_TOKEN)
    except yaml.YAMLError:
        # 解析失败时原文返回（反正也无法通过校验保存）
        pass
    return text


def save_config_raw(text: str, source_ip: str = "") -> Tuple[bool, List[str], List[str]]:
    """
    保存 config.yaml 原文

    流程：掩码还原 → YAML 语法解析 → 密钥字段防护 → 结构校验 → 备份 → 写入 → 审计
    """
    errors: List[str] = []
    warnings: List[str] = []

    current_path = Path(get_config_path())
    current_data = {}
    try:
        current_data = yaml.safe_load(read_text_file(current_path)) or {}
    except yaml.YAMLError as e:
        return False, [f"读取当前配置失败: {e}"], []

    # 1. 掩码还原（原始文本层面替换，保持用户编辑的其他内容不变）
    for path in CREDENTIAL_FIELDS:
        value = _get_by_path(current_data, path)
        if isinstance(value, str) and value.strip():
            text = text.replace(MASK_TOKEN, value, 1)

    # 2. YAML 语法解析
    try:
        new_data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return False, [f"YAML 语法错误: {e}"], []

    if not isinstance(new_data, dict):
        return False, ["config.yaml 顶层结构必须是键值映射"], []

    # 3. 密钥字段防护（仅默认模式；EDIT_SECRETS 模式允许修改，但掩码仍还原为原值）
    if not secrets_editable():
        for path in CREDENTIAL_FIELDS:
            new_value = _get_by_path(new_data, path)
            old_value = _get_by_path(current_data, path)
            if isinstance(new_value, str) and (MASK_TOKEN in new_value or not new_value.strip()):
                _set_by_path(new_data, path, old_value)
                new_value = old_value
            if new_value != old_value:
                errors.append(f"密钥字段 {path} 不允许通过网页修改，请使用环境变量管理")

        if errors:
            return False, errors, warnings

    # 4. 结构校验
    errors, validate_warnings = validate_config_data(new_data)
    warnings.extend(validate_warnings)
    if errors:
        return False, errors, warnings

    # 5. 备份 + 原子写入 + 审计
    backup_path = _backup_file(current_path)
    _atomic_write(current_path, text)
    _write_audit(source_ip, current_path.name, action="update", summary="原始 YAML 编辑")

    return True, [], warnings


# ═══════════════════════════════════════════════════════
# 校验
# ═══════════════════════════════════════════════════════


def validate_config_data(data: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """
    校验配置数据，返回 (errors, warnings)

    errors 阻止保存；warnings 仅提示。
    """
    errors: List[str] = []
    warnings: List[str] = []

    # 顶层必须是映射，且允许的段类型正确
    for section in ["app", "schedule", "platforms", "rss", "announcements", "newsflash", "github", "report", "filter",
                    "ai_filter", "display", "notification", "storage", "ai",
                    "ai_analysis", "ai_translation", "advanced"]:
        if section in data and not isinstance(data[section], dict):
            errors.append(f"配置段 {section} 必须是键值映射")

    if errors:
        return errors, warnings

    # 时区合法性
    timezone = _get_by_path(data, "app.timezone")
    if timezone:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(str(timezone))
        except Exception:
            errors.append(f"时区无效: {timezone}（参考 Asia/Shanghai、America/New_York）")

    # 枚举值
    report_mode = _get_by_path(data, "report.mode")
    if report_mode and report_mode not in ("daily", "current", "incremental"):
        errors.append(f"report.mode 无效: {report_mode}（可选 daily/current/incremental）")

    display_mode = _get_by_path(data, "report.display_mode")
    if display_mode and display_mode not in ("keyword", "platform"):
        errors.append(f"report.display_mode 无效: {display_mode}（可选 keyword/platform）")

    filter_method = _get_by_path(data, "filter.method")
    if filter_method and filter_method not in ("keyword", "ai"):
        errors.append(f"filter.method 无效: {filter_method}（可选 keyword/ai）")

    ai_analysis_mode = _get_by_path(data, "ai_analysis.mode")
    if ai_analysis_mode and ai_analysis_mode not in ("follow_report", "daily", "current", "incremental"):
        errors.append(f"ai_analysis.mode 无效: {ai_analysis_mode}")

    storage_backend = _get_by_path(data, "storage.backend")
    if storage_backend and storage_backend not in ("auto", "local", "remote"):
        errors.append(f"storage.backend 无效: {storage_backend}（可选 auto/local/remote）")

    wework_msg_type = _get_by_path(data, "notification.channels.wework.msg_type")
    if wework_msg_type and wework_msg_type not in ("markdown", "text"):
        errors.append(f"企业微信 msg_type 无效: {wework_msg_type}（可选 markdown/text）")

    # 通知渠道配对校验（复用主程序逻辑）
    channels = _get_by_path(data, "notification.channels") or {}
    telegram = channels.get("telegram", {}) or {}
    tokens = parse_multi_account_config(telegram.get("bot_token", ""))
    chat_ids = parse_multi_account_config(telegram.get("chat_id", ""))
    if tokens or chat_ids:
        valid, _ = validate_paired_configs(
            {"bot_token": tokens, "chat_id": chat_ids}, "Telegram",
            required_keys=["bot_token", "chat_id"],
        )
        if not valid:
            errors.append("Telegram bot_token 与 chat_id 多账号数量不一致（分号分隔，须一一配对）")

    # URL 字段校验
    for path, label in URL_FIELDS.items():
        url = _get_by_path(data, path)
        if isinstance(url, str) and url.strip():
            error, url_warning = _validate_url(url, label)
            if error:
                errors.append(error)
            if url_warning:
                warnings.append(url_warning)

    # RSS feed URL 校验
    feeds = _get_by_path(data, "rss.feeds") or []
    for i, feed in enumerate(feeds):
        if not isinstance(feed, dict):
            errors.append(f"rss.feeds[{i}] 必须是键值映射（id/name/url）")
            continue
        if not feed.get("id"):
            errors.append(f"rss.feeds[{i}] 缺少 id")
        if not feed.get("url"):
            errors.append(f"rss.feeds[{i}] ({feed.get('id', '未命名')}) 缺少 url")
        else:
            error, url_warning = _validate_url(str(feed["url"]), f"RSS 源 {feed.get('id')}")
            if error:
                errors.append(error)
            if url_warning:
                warnings.append(url_warning)

    # 平台 sources 校验
    sources = _get_by_path(data, "platforms.sources") or []
    for i, source in enumerate(sources):
        if not isinstance(source, dict):
            errors.append(f"platforms.sources[{i}] 必须是键值映射（id/name）")
            continue
        if not source.get("id"):
            errors.append(f"platforms.sources[{i}] 缺少 id")

    # 权重求和提示
    weight = _get_by_path(data, "advanced.weight") or {}
    try:
        total = sum(float(weight.get(k, 0)) for k in ("rank", "frequency", "hotness"))
        if abs(total - 1.0) > 0.01:
            warnings.append(f"advanced.weight 三项权重之和为 {total:.2f}（建议等于 1）")
    except (TypeError, ValueError):
        errors.append("advanced.weight 的 rank/frequency/hotness 必须是数字")

    return errors, warnings


_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|\[?::1\]?)",
    re.IGNORECASE,
)


def _validate_url(url: str, label: str) -> Tuple[Optional[str], Optional[str]]:
    """校验单个 URL，返回 (error, warning)"""
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return f"{label} 必须以 http:// 或 https:// 开头: {url}", None
    if _PRIVATE_HOST_RE.match(url.split("://", 1)[1]):
        return None, f"{label} 指向内网地址（{url}），请确认这是你自己的服务"
    return None, None


# ═══════════════════════════════════════════════════════
# 变更对比
# ═══════════════════════════════════════════════════════


def _diff_data(old: Any, new: Any, prefix: str = "") -> List[Dict[str, Any]]:
    """递归对比两份数据，返回变更列表（用于审计与前端提示）"""
    changes: List[Dict[str, Any]] = []

    if isinstance(old, dict) and isinstance(new, dict):
        for key in set(old.keys()) | set(new.keys()):
            path = f"{prefix}.{key}" if prefix else str(key)
            old_value = old.get(key)
            new_value = new.get(key)
            if isinstance(old_value, (dict, list)) or isinstance(new_value, (dict, list)):
                if isinstance(old_value, dict) and isinstance(new_value, dict):
                    changes.extend(_diff_data(old_value, new_value, path))
                elif old_value != new_value:
                    changes.append({
                        "path": path,
                        "old": _brief(old_value),
                        "new": _brief(new_value),
                        "summary": _change_summary(old_value, new_value),
                    })
            elif old_value != new_value:
                changes.append({
                    "path": path,
                    "old": _brief(old_value),
                    "new": _brief(new_value),
                    "summary": f"{_brief(old_value)} → {_brief(new_value)}",
                })
        return changes

    if old != new:
        changes.append({
            "path": prefix,
            "old": _brief(old),
            "new": _brief(new),
            "summary": f"{_brief(old)} → {_brief(new)}",
        })
    return changes


def _brief(value: Any, limit: int = 60) -> str:
    """值的简短展示（用于 diff/审计）"""
    if value is None:
        return "(删除)"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _change_summary(old: Any, new: Any) -> str:
    """容器类型变更的摘要"""
    old_len = len(old) if hasattr(old, "__len__") else 0
    new_len = len(new) if hasattr(new, "__len__") else 0
    if isinstance(old, dict) or isinstance(new, dict):
        return f"结构变更（{old_len} → {new_len} 项）"
    return f"列表变更（{old_len} → {new_len} 项）"
