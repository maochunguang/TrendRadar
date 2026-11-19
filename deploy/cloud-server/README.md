# GitHub Trending JSON API（部署在自有云服务器）

在**你的云服务器**上跑一个轻量 API，替 TrendRadar 抓取并解析 GitHub
Trending，返回结构化 JSON。比 nginx 纯反代更优：解析在云端完成、自带
20 分钟缓存（GitHub trending 本就是小时级变化）、上游失败时用旧缓存兜底。

## 部署前提（先测再部署）

云服务器必须能稳定访问 github.com：

```bash
curl -s -o /dev/null -w "%{http_code}" -m 10 https://github.com/trending
```

- 境外/香港节点：通常稳定返回 200 ✅
- 境内节点：与 NAS 同受干扰，时好时坏 ⚠️（不达标就换境外节点或改用
  Cloudflare Workers 方案，见 `../cloudflare-worker/`）

## 部署（零依赖，标准库实现）

```bash
sudo mkdir -p /opt/github-trending-api
sudo cp github_trending_api.py /opt/github-trending-api/
```

## systemd 常驻

```bash
sudo tee /etc/systemd/system/gh-trending-api.service > /dev/null << 'EOF'
[Unit]
Description=GitHub Trending JSON API
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/github-trending-api/github_trending_api.py
Restart=always
RestartSec=5
# 可选：端口 / 缓存 / 访问令牌
Environment=PORT=8787
Environment=CACHE_TTL_SECONDS=1200
# Environment=API_TOKEN=换一个随机密钥

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now gh-trending-api
```

## 验证

```bash
curl -s http://127.0.0.1:8787/trending/daily | head -c 500
# {"generated_at": "...", "period": "daily", "count": 25, "repos": [...]}
```

## TrendRadar 侧配置

```yaml
github:
  mirror_url: http://<云服务器IP>:8787
```

- 若开启 API_TOKEN，TrendRadar 侧把 token 拼在地址上：
  `mirror_url: http://<云服务器IP>:8787?token=你的密钥`
- TrendRadar 按响应 Content-Type 自动识别：JSON（本 API）与 HTML
  （Cloudflare Workers 反代等）都支持，同一配置项两种后端。

## 安全提示

- 8787 端口公网暴露时建议开启 `API_TOKEN`（防止被扫描滥用消耗流量）
- 也可在云服务器防火墙只放行 NAS 出口 IP
