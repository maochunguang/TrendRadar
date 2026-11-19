# GitHub Trending 镜像反代（Cloudflare Workers）

`github.com` 在国内网络下经常不可达（HTML 页面尤其不稳，`api.github.com` 反而可用）。
此 Worker 把 GitHub Trending 页面透传，供 TrendRadar 的 `github.mirror_url` 使用。

## 为什么不用现成镜像站

实测（2026-11-19）：bgithub.xyz（403）、kkgithub.com（404）、gh-proxy.com /
ghproxy.net / ghfast.top（403/404）等通用加速站**只代理文件下载（raw/release），
不代理 HTML 页面**；OSS Insight 的 trends API 已停服。所以免费且稳定的方案是
自建 Cloudflare Workers 反代。

## 部署（免费，约 3 分钟）

1. 注册/登录 [Cloudflare Dashboard](https://dash.cloudflare.com)（免费套餐即可，无需自有域名）
2. 左侧 **Workers & Pages → Create → Create Worker → Deploy**（模板随意）
3. **Edit code** → 粘贴 [github-mirror.js](./github-mirror.js) 全部内容 → **Deploy**
4. 复制 Worker 地址，形如 `https://gh-mirror.your-name.workers.dev`
5. 填入 TrendRadar 配置：

```yaml
github:
  mirror_url: https://gh-mirror.your-name.workers.dev
```

6. 验证：浏览器打开 `https://xxx.workers.dev/trending/daily`，能看到 GitHub
   Trending 页面即成功；TrendRadar 下轮运行日志显示
   `[GitHub] 获取 30 条榜单数据` 且不再出现降级提示。

## 安全设计

- 只透传 GET/HEAD（只读镜像，拒绝登录/写入）
- 路径白名单：仅 trending 页与仓库页 + 静态资源，其余 404（防止被当通用代理滥用）
- 剥离 Cookie（不带登录态）
- 免费额度 10 万请求/天，本用途每 30 分钟 3 个页面，绰绰有余

## 备注

- Worker 是无状态的，Cloudflare 边缘到 github.com 的连通性极好
- 若 GitHub 改版导致页面解析失败，TrendRadar 会自动降级到 Search API 近似榜
