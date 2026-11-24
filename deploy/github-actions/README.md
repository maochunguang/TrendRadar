# GitHub Actions 采集 Trending → Gitee 分发

架构：**GitHub Actions（境外，抓 github.com 稳定）→ 定时把 JSON 写入仓库 →
同步推送到 Gitee → TrendRadar（NAS）从 Gitee 拉取**。

为什么不用 Gitee 主动同步：Gitee 免费版的"强制同步"只有网页按钮（手动），
没有定时自动同步能力；由 GitHub Actions 主动 `git push` 到 Gitee 才可控频率。

## 新鲜度

| 档位 | Actions cron | 端到端新鲜度 |
|---|---|---|
| 最快 | `*/5 * * * *` | 5~20 分钟（Actions 定时触发有几分钟随机延迟）|
| **默认** | `*/30 * * * *` | 30~45 分钟（与 TrendRadar 运行周期一致）|

Trending 是小时级慢变数据，默认档足够。

## 部署步骤（约 10 分钟，一次性）

1. **建 Gitee 仓库**：注册/登录 gitee.com → 新建仓库 `gh-trending-data`
   （**公开**，可空仓库），并在 **Gitee → 设置 → 私人令牌** 生成 token
   （勾选 `projects` 权限）
2. **建 GitHub 数据仓库**：如 `maochunguang/gh-trending-data`（**公开**，
   公开仓库 Actions 免费不限时长）
3. 复制本目录文件到数据仓库根目录：
   - `fetch_trending.py`
   - `trending-sync.yml` → 存为 `.github/workflows/trending-sync.yml`
4. 数据仓库 **Settings → Secrets and variables → Actions** 添加：
   - `GITEE_USER` = Gitee 用户名
   - `GITEE_TOKEN` = Gitee 私人令牌
5. Actions 页面手动 Run workflow 触发一次 → 检查仓库里出现
   `data/trending/{daily,weekly,monthly}.json`，且 Gitee 仓库同步到同结构

## TrendRadar 侧接入

设置中心「GitHub 榜单」→「Trending 镜像反代」填（NAS 上 Gitee 直连畅通）：

```
https://gitee.com/<你的Gitee用户名>/gh-trending-data/raw/main
```

TrendRadar 会按 `mirror_url + /trending/{daily|weekly|monthly}` 拉取
JSON 文件（`Content-Type` 为 text/plain 也能自动按 JSON 解析），
页面数据来源标注变为官方 trending 数据（带"今日 +N 星"）。

## 备注

- 每次运行会往仓库提交一个 commit（数据有变化时），仓库会持续增长——
  数据文件不大（3 × 25 条 ≈ 30KB/次），无实际影响；介意的话可定期清历史
- GitHub 页面若改版导致解析失败，脚本不覆盖旧数据，TrendRadar 侧继续读
  上一份 + 自动降级 Search API 近似榜
