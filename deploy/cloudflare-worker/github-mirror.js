/**
 * GitHub Trending 镜像反代（Cloudflare Workers）
 *
 * 用途：TrendRadar 的 github.mirror_url 配置使用。github.com 在国内网络下
 * 间歇性/持续性不可达，而 Cloudflare 的边缘节点可以稳定访问 github.com；
 * 此 Worker 把请求原样透传到 github.com（仅允许页面路径，拒绝大文件下载）。
 *
 * 免费额度：10 万请求/天（本用途每 30 分钟 3 个页面，绰绰有余）。
 * 无需自有域名：部署后使用 workers.dev 免费子域即可。
 *
 * 部署步骤（约 3 分钟）：
 *   1. 注册/登录 https://dash.cloudflare.com（免费）
 *   2. 左侧 Workers & Pages → Create → Create Worker → Deploy（模板随意）
 *   3. Edit code → 粘贴本文件全部内容 → Deploy
 *   4. 复制 Worker 地址（形如 https://xxx.your-name.workers.dev）
 *   5. 填入 TrendRadar 配置：github.mirror_url: https://xxx.your-name.workers.dev
 *
 * 验证：浏览器打开 https://xxx.workers.dev/trending/daily，
 *       能看到 GitHub Trending 页面即成功。
 */

const UPSTREAM = "https://github.com";

// 只透传页面路径（trending/repo 页）；拦截大文件下载与 API（镜像站只服务页面）
const BLOCKED_PREFIXES = [
    "/login", "/logout", "/session", "/settings", "/notifications",
];

export default {
    async fetch(request) {
        const url = new URL(request.url);

        // 只允许 GET/HEAD（只读镜像，不接受登录/写入）
        if (request.method !== "GET" && request.method !== "HEAD") {
            return new Response("Method Not Allowed", { status: 405 });
        }

        // 可选：简单的路径白名单（trending 与仓库页），其余 404，防止被滥用当通用代理
        const path = url.pathname;
        const isTrending = path === "/" || path === "/trending" || path.startsWith("/trending/");
        const isRepoPage = /^\/[^/]+\/[^/]+(\/?$|\/(blob|tree|issues|pulls|stargazers|forks|about)$)/.test(path);
        const isStaticAsset = path.startsWith("/assets/") || path.endsWith(".css") || path.endsWith(".js") || path.endsWith(".png") || path.endsWith(".svg");
        if (!isTrending && !isRepoPage && !isStaticAsset) {
            return new Response("Not Found (this mirror only serves trending/repo pages)", { status: 404 });
        }

        for (const prefix of BLOCKED_PREFIXES) {
            if (path.startsWith(prefix)) {
                return new Response("Not Found", { status: 404 });
            }
        }

        // 透传到 github.com（保留原请求头里的 User-Agent / Accept）
        const upstreamUrl = UPSTREAM + path + url.search;
        const upstreamReq = new Request(upstreamUrl, {
            method: request.method,
            headers: new Headers(request.headers),
            redirect: "follow",
        });
        upstreamReq.headers.delete("cookie");

        try {
            const resp = await fetch(upstreamReq);
            // 标注镜像来源，便于排查
            const headers = new Headers(resp.headers);
            headers.set("x-mirrored-from", "github.com");
            return new Response(resp.body, {
                status: resp.status,
                statusText: resp.statusText,
                headers,
            });
        } catch (err) {
            return new Response(`Mirror upstream error: ${err.message}`, { status: 502 });
        }
    },
};
