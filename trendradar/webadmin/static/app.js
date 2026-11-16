/* TrendRadar Web 设置中心 */
"use strict";

const MASK_TOKEN = "__TR_MASKED__";

const state = {
  token: localStorage.getItem("tr_admin_token") || "",
  authRequired: false,
  status: null,
  data: null,        // 当前编辑中的结构化配置（YAML 形状）
  loaded: null,      // 载入时的快照（用于 diff）
  envShadowed: {},   // 路径 → 环境变量名
  readonly: [],      // 密钥字段路径（EDIT_SECRETS 未开启时）
  secretsEditable: false,
  textFiles: [],     // 白名单文本文件
  activeFile: null,
  activeFileText: "",
  activeLog: null,
  freqModel: { header: "", global_filters: [], filters: [], groups: [] },
  newsToken: "",
  newsGroups: [],
  freqMode: "structured",
  pollTimer: null,
};

/* ══════════════ 基础工具 ══════════════ */

function $(id) { return document.getElementById(id); }

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(msg, type = "") {
  const item = document.createElement("div");
  item.className = `toast-item ${type}`;
  item.textContent = msg;
  $("toast").appendChild(item);
  setTimeout(() => item.remove(), type === "error" ? 6000 : 3000);
}

function getPath(obj, path) {
  let cur = obj;
  for (const part of path.split(".")) {
    if (cur == null || typeof cur !== "object") return undefined;
    cur = cur[part];
  }
  return cur;
}

function setPath(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (typeof cur[parts[i]] !== "object" || cur[parts[i]] === null) cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}

function deepClone(v) { return JSON.parse(JSON.stringify(v)); }

function fmtBytes(n) {
  if (n > 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n > 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

/* ══════════════ API ══════════════ */

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (state.token) headers["X-Admin-Token"] = state.token;

  const resp = await fetch(path, { ...options, headers });
  let data = {};
  try { data = await resp.json(); } catch { /* 空响应 */ }

  if (resp.status === 401) { showLogin(); throw { message: data.error || "需要登录", status: 401 }; }

  if (!resp.ok) {
    const msg = data.error || (data.errors && data.errors.join("；")) || `请求失败 (${resp.status})`;
    throw { message: msg, status: resp.status, data };
  }
  return data;
}

function showLogin() {
  state.token = "";
  localStorage.removeItem("tr_admin_token");
  $("loginMask").classList.add("show");
  $("loginToken").focus();
}

async function doLogin() {
  const token = $("loginToken").value.trim();
  if (!token) return;
  state.token = token;
  try {
    await api("/api/status");
    localStorage.setItem("tr_admin_token", token);
    $("loginMask").classList.remove("show");
    toast("登录成功", "success");
    boot();
  } catch (e) {
    if (e.status !== 401) { $("loginMask").classList.remove("show"); boot(); }
    else toast("token 无效", "error");
  }
}

/* ══════════════ 字段渲染 ══════════════ */

function fieldDisabled(path) {
  if (state.envShadowed[path]) return { disabled: true, reason: `由环境变量 ${state.envShadowed[path]} 生效` };
  if (state.readonly.includes(path)) return { disabled: true, reason: "密钥字段（请通过 docker/.env 环境变量管理）" };
  return { disabled: false };
}

function fieldBadges(path) {
  let html = "";
  if (state.envShadowed[path]) html += `<span class="badge env">env: ${escapeHtml(state.envShadowed[path])}</span>`;
  if (state.readonly.includes(path)) html += `<span class="badge secret">密钥</span>`;
  return html;
}

function renderField(f) {
  const value = getPath(state.data, f.path);
  const gate = fieldDisabled(f.path);
  const badges = fieldBadges(f.path);
  const hint = f.hint ? `<div class="f-hint">${f.hint}</div>` : "";

  let control = "";
  if (f.type === "bool") {
    control = `<label class="toggle"><input type="checkbox" data-path="${f.path}" ${value ? "checked" : ""} ${gate.disabled ? "disabled" : ""}><span class="slider"></span></label>`;
  } else if (f.type === "select") {
    const opts = f.options.map(o =>
      `<option value="${escapeHtml(o[0])}" ${String(value) === String(o[0]) ? "selected" : ""}>${escapeHtml(o[1])}</option>`).join("");
    control = `<select data-path="${f.path}" ${gate.disabled ? "disabled" : ""}>${opts}</select>`;
  } else if (f.type === "textarea") {
    control = `<textarea data-path="${f.path}" rows="2" ${gate.disabled ? "disabled" : ""}>${escapeHtml(value ?? "")}</textarea>`;
  } else if (f.type === "csvlist") {
    const text = Array.isArray(value) ? value.join(", ") : (value ?? "");
    control = `<input type="text" data-path="${f.path}" data-csv="1" value="${escapeHtml(text)}" ${gate.disabled ? "disabled" : ""} placeholder="多个用英文逗号分隔，留空表示无">`;
  } else if (f.type === "credential") {
    if (state.secretsEditable) {
      // 可编辑模式：密码框 + 显示切换
      control = `<div class="cred-row">
        <input type="password" data-path="${f.path}" value="${escapeHtml(value ?? "")}" placeholder="">
        <button type="button" class="btn small cred-toggle">显示</button>
      </div>`;
    } else {
      const configured = (value && value !== "") || state.envShadowed[f.path];
      control = `<input type="password" data-path="${f.path}" value="${escapeHtml(value ?? "")}" disabled
        title="${escapeHtml(gate.reason || "")}" placeholder="${configured ? "已配置（脱敏显示）" : "未配置，请在 docker/.env 中设置"}">`;
    }
  } else {
    const type = f.type === "number" ? "number" : "text";
    control = `<input type="${type}" data-path="${f.path}" value="${escapeHtml(value ?? "")}" ${gate.disabled ? "disabled" : ""}>`;
  }

  return `<div class="field">
    <div class="f-label"><span class="f-name">${escapeHtml(f.label)} ${badges}</span>${hint}</div>
    <div class="f-control">${control}</div>
  </div>`;
}

function renderFields(containerId, cards) {
  const container = $(containerId);
  container.innerHTML = cards.map(card => `
    <div class="card">
      <h2>${card.title}</h2>
      ${card.hint ? `<p class="card-hint">${card.hint}</p>` : ""}
      ${card.fields.map(renderField).join("")}
    </div>`).join("") + saveBar();

  bindFieldEvents(container);
}

function saveBar() {
  return `<div class="save-bar"><button class="btn primary btn-save-config">💾 保存配置</button>
    <span style="color:var(--text-muted);font-size:12.5px">保存前会校验并自动备份；改动在下次运行时生效，可点击顶部「立即运行」马上应用</span></div>`;
}

function bindFieldEvents(container) {
  container.querySelectorAll("input[data-path], select[data-path], textarea[data-path]").forEach(el => {
    const handler = () => {
      const path = el.dataset.path;
      if (el.dataset.csv !== undefined) {
        const text = el.value.trim();
        setPath(state.data, path, text ? text.split(",").map(s => s.trim()).filter(Boolean) : []);
      } else if (el.type === "checkbox") {
        setPath(state.data, path, el.checked);
      } else if (el.type === "number") {
        const num = parseFloat(el.value);
        setPath(state.data, path, isNaN(num) ? el.value : num);
      } else {
        setPath(state.data, path, el.value);
      }
    };
    el.addEventListener(el.type === "checkbox" ? "change" : "input", handler);
  });

  container.querySelectorAll(".cred-toggle").forEach(btn => {
    btn.addEventListener("click", () => {
      const input = btn.parentElement.querySelector("input");
      const hidden = input.type === "password";
      input.type = hidden ? "text" : "password";
      btn.textContent = hidden ? "隐藏" : "显示";
    });
  });

  const saveBtn = container.querySelector(".btn-save-config");
  if (saveBtn) saveBtn.addEventListener("click", () => saveConfig());
}

/* ══════════════ 各分区定义 ══════════════ */

const SECTIONS = [
  {
    id: "overview", title: "📊 概览", render: renderOverview,
  },
  {
    id: "basic", title: "⚙️ 基础", render: () => renderFields("panel-basic", [{
      title: "应用设置",
      fields: [
        { path: "app.timezone", label: "时区", hint: "影响所有时间显示、调度判断、数据存储，如 Asia/Shanghai" },
        { path: "app.show_version_update", label: "显示版本更新提示", type: "bool" },
        { path: "advanced.debug", label: "调试模式", type: "bool", hint: "开启后输出详细日志" },
      ],
    }]),
  },
  {
    id: "schedule", title: "⏰ 调度", render: () => renderFields("panel-schedule", [
      {
        title: "调度系统 —— 什么时间做什么事",
        hint: "通过 timeline.yaml 定义的时间段自动决定推送时机、AI 分析时机与报告模式；详细时间段请到「文本文件」标签编辑 timeline.yaml",
        fields: [
          { path: "schedule.enabled", label: "启用调度系统", type: "bool", hint: "关闭时按 report.mode 默认行为运行" },
          {
            path: "schedule.preset", label: "预设模板", type: "select", options: [
              ["always_on", "always_on —— 全天候，有新增即推送"],
              ["morning_evening", "morning_evening —— 全天推送 + 晚间汇总（推荐）"],
              ["office_hours", "office_hours —— 工作日三段式"],
              ["night_owl", "night_owl —— 午后速览 + 深夜汇总"],
              ["custom", "custom —— 完全自定义（需编辑 timeline.yaml）"],
            ],
          },
        ],
      },
    ]),
  },
  {
    id: "sources", title: "📡 数据源", render: renderSources,
  },
  {
    id: "report", title: "📰 报告与筛选", render: () => renderFields("panel-report", [
      {
        title: "报告模式",
        fields: [
          {
            path: "report.mode", label: "报告模式", type: "select", options: [
              ["daily", "daily —— 当日汇总（包含之前推过的）"],
              ["current", "current —— 当前榜单（持续在榜的每次出现）"],
              ["incremental", "incremental —— 增量监控，零重复"],
            ], hint: "开启调度后会被 timeline 时段覆盖",
          },
          {
            path: "report.display_mode", label: "分组维度", type: "select", options: [
              ["keyword", "keyword —— 按关键词分组"],
              ["platform", "platform —— 按平台/来源分组"],
            ],
          },
          { path: "report.sort_by_position_first", label: "按关键词定义顺序排序", type: "bool", hint: "仅 display_mode=keyword 时生效；关闭则按命中条数排序" },
          { path: "report.rank_threshold", label: "排名高亮阈值", type: "number" },
          { path: "report.max_news_per_keyword", label: "每关键词最大显示数", type: "number", hint: "0 = 不限制" },
        ],
      },
      {
        title: "筛选策略",
        fields: [
          {
            path: "filter.method", label: "筛选方式", type: "select", options: [
              ["keyword", "keyword —— 关键词匹配（不耗 token）"],
              ["ai", "ai —— AI 智能分类（消耗 token）"],
            ], hint: "keyword 模式的词组在「热点关键词」编辑；ai 模式的兴趣描述在 ai_interests.txt",
          },
          { path: "filter.priority_sort_enabled", label: "AI 模式按兴趣定义顺序排序", type: "bool" },
        ],
      },
      {
        title: "AI 筛选参数（仅 filter.method=ai 时生效）",
        fields: [
          { path: "ai_filter.batch_size", label: "每批标题数", type: "number" },
          { path: "ai_filter.batch_interval", label: "分批间隔（秒）", type: "number" },
          { path: "ai_filter.min_score", label: "推送最低分阈值", type: "number", hint: "0.0~1.0，值越高越严格，0 = 不过滤" },
          { path: "ai_filter.reclassify_threshold", label: "全量重分类阈值", type: "number", hint: "越低越倾向全量重分类（更准但更耗 token）" },
          { path: "ai_filter.interests_file", label: "兴趣描述文件", hint: "留空使用默认 config/ai_interests.txt；自定义文件放 config/custom/ai/ 后填文件名" },
          { path: "ai_filter.prompt_file", label: "分类提示词文件", hint: "一般无需修改" },
          { path: "ai_filter.extract_prompt_file", label: "标签提取提示词文件", hint: "一般无需修改" },
          { path: "ai_filter.update_tags_prompt_file", label: "标签更新提示词文件", hint: "一般无需修改" },
        ],
      },
    ]),
  },
  {
    id: "display", title: "🖥 推送内容", render: renderDisplay,
  },
  {
    id: "notification", title: "🔔 通知", render: renderNotification,
  },
  {
    id: "ai", title: "🤖 AI", render: renderAI,
  },
  {
    id: "storage", title: "💾 存储", render: () => renderFields("panel-storage", [
      {
        title: "存储后端",
        hint: "S3 访问密钥可通过环境变量 S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY 管理，开启 EDIT_SECRETS 后也可在此修改",
        fields: [
          {
            path: "storage.backend", label: "存储后端", type: "select", options: [
              ["auto", "auto —— 自动选择"],
              ["local", "local —— 本地 SQLite + 文件"],
              ["remote", "remote —— 远程云存储（S3 兼容）"],
            ],
          },
          { path: "storage.formats.sqlite", label: "SQLite 主存储", type: "bool", hint: "⚠️ 必须保持开启" },
          { path: "storage.formats.txt", label: "生成 TXT 快照", type: "bool" },
          { path: "storage.formats.html", label: "生成 HTML 报告", type: "bool", hint: "邮件推送或网页版报告需开启" },
        ],
      },
      {
        title: "本地存储",
        fields: [
          { path: "storage.local.data_dir", label: "数据目录", hint: "相对路径，默认 output" },
          { path: "storage.local.retention_days", label: "保留天数", type: "number", hint: "0 = 永久保留" },
        ],
      },
      {
        title: "远程存储（S3 兼容：R2/OSS/COS 等）",
        hint: "endpoint_url 为空时优先使用环境变量 S3_ENDPOINT_URL 等",
        fields: [
          { path: "storage.remote.endpoint_url", label: "服务端点", type: "url" },
          { path: "storage.remote.bucket_name", label: "存储桶名称" },
          { path: "storage.remote.region", label: "区域（可选）" },
          { path: "storage.remote.access_key_id", label: "访问密钥 ID", type: "credential" },
          { path: "storage.remote.secret_access_key", label: "访问密钥", type: "credential" },
          { path: "storage.remote.retention_days", label: "远程保留天数", type: "number", hint: "0 = 永久保留" },
        ],
      },
      {
        title: "数据拉取（远程 → 本地）",
        fields: [
          { path: "storage.pull.enabled", label: "启动时自动拉取", type: "bool" },
          { path: "storage.pull.days", label: "拉取最近天数", type: "number" },
        ],
      },
    ]),
  },
  {
    id: "advanced", title: "🛠 高级", render: () => renderFields("panel-advanced", [
      {
        title: "爬虫技术参数",
        fields: [
          { path: "advanced.crawler.request_interval", label: "请求间隔（毫秒）", type: "number" },
          { path: "advanced.crawler.use_proxy", label: "启用代理", type: "bool" },
          { path: "advanced.crawler.default_proxy", label: "默认代理地址" },
        ],
      },
      {
        title: "RSS 技术参数",
        fields: [
          { path: "advanced.rss.request_interval", label: "请求间隔（毫秒）", type: "number" },
          { path: "advanced.rss.timeout", label: "请求超时（秒）", type: "number" },
          { path: "advanced.rss.use_proxy", label: "使用代理", type: "bool" },
          { path: "advanced.rss.proxy_url", label: "RSS 专属代理", hint: "留空使用 crawler.default_proxy" },
        ],
      },
      {
        title: "排序权重（三项建议合计为 1）",
        fields: [
          { path: "advanced.weight.rank", label: "排名权重", type: "number" },
          { path: "advanced.weight.frequency", label: "频次权重", type: "number" },
          { path: "advanced.weight.hotness", label: "热度权重", type: "number" },
        ],
      },
      {
        title: "推送内部参数",
        fields: [
          { path: "advanced.max_accounts_per_channel", label: "每渠道最大账号数", type: "number" },
          { path: "advanced.batch_send_interval", label: "批次发送间隔（秒）", type: "number" },
        ],
      },
    ]),
  },
  {
    id: "textedit", title: "📝 文本文件", render: renderTextEditor,
  },
  {
    id: "raw", title: "🛠 原始 YAML", render: renderRawEditor,
  },
  {
    id: "logs", title: "📋 运行日志", render: renderLogsPanel,
  },
];

/* ══════════════ 数据源分区 ══════════════ */

function renderSources() {
  const container = $("panel-sources");
  container.innerHTML = `
    <div class="card">
      <h2>热榜平台</h2>
      <p class="card-hint">数据来源于 newsnow 开源项目；自部署时填写 api_url，否则建议为每个平台配置 expected_domain</p>
      <div id="platformFields"></div>
      <div style="margin-top:10px"><strong>平台列表</strong>
        <div class="list-editor" id="sourceList"></div>
        <button class="btn small" id="btnAddSource" style="margin-top:6px">＋ 添加平台</button>
      </div>
    </div>
    <div class="card">
      <h2>RSS 订阅</h2>
      <p class="card-hint">每个源可单独配置 enabled 与 max_age_days（覆盖全局新鲜度过滤）；支持从其他阅读器导入 OPML</p>
      <div id="rssFields"></div>
      <div style="margin-top:10px"><strong>RSS 源列表</strong>
        <div class="list-editor" id="feedList"></div>
        <button class="btn small" id="btnAddFeed" style="margin-top:6px">＋ 添加 RSS 源</button>
      </div>
    </div>
    ${saveBar()}`;

  // 标量字段
  const scalars = [
    { path: "platforms.enabled", label: "启用热榜抓取", type: "bool" },
    { path: "platforms.api_url", label: "热榜数据 API 地址", hint: "留空使用默认地址；自部署填你的实例地址" },
    { path: "rss.enabled", label: "启用 RSS 抓取", type: "bool" },
    { path: "rss.summary.enabled", label: "显示文章摘要", type: "bool", hint: "在推送和网页报告中展示 RSS 摘要，看完摘要可不点原文" },
    { path: "rss.summary.max_length", label: "摘要最大长度（字符）", type: "number", hint: "0=不截断" },
    { path: "rss.summary.fulltext_fallback", label: "无摘要时抓取原文提取", type: "bool", hint: "有网络开销，每次运行最多 20 篇、超时 10 秒" },
    { path: "rss.freshness_filter.enabled", label: "启用新鲜度过滤", type: "bool" },
    { path: "rss.freshness_filter.max_age_days", label: "最大文章年龄（天）", type: "number", hint: "0 = 推送所有文章" },
  ];
  $("platformFields").innerHTML = renderField(scalars[0]) + renderField(scalars[1]);
  $("rssFields").innerHTML = scalars.slice(2).map(renderField).join("");
  bindFieldEvents(container);

  renderSourceList();
  renderFeedList();

  $("btnAddSource").addEventListener("click", () => {
    state.data.platforms.sources.push({ id: "new-platform", name: "新平台", expected_domain: "" });
    renderSourceList();
  });
  $("btnAddFeed").addEventListener("click", () => {
    state.data.rss.feeds.push({ id: "new-feed", name: "新源", url: "https://", enabled: true });
    renderFeedList();
  });

  // OPML 导入按钮（追加到 RSS 卡片标题行）
  const rssCard = $("feedList").closest(".card");
  if (rssCard && !rssCard.querySelector(".btn-opml")) {
    const opmlBtn = document.createElement("button");
    opmlBtn.className = "btn small btn-opml";
    opmlBtn.textContent = "📥 导入 OPML";
    opmlBtn.style.marginLeft = "10px";
    opmlBtn.addEventListener("click", openOpmlModal);
    const heading = rssCard.querySelector("strong");
    if (heading && heading.parentElement) heading.parentElement.insertBefore(opmlBtn, heading.nextSibling);
  }

  container.querySelector(".btn-save-config").addEventListener("click", () => saveConfig());
}

function renderSourceList() {
  const wrap = $("sourceList");
  const sources = state.data.platforms.sources || [];
  wrap.innerHTML = sources.map((s, i) => `
    <div class="list-item" data-index="${i}">
      <input type="text" style="max-width:150px" placeholder="id" value="${escapeHtml(s.id || "")}" data-k="id" title="平台唯一标识（勿改已有 id）">
      <input type="text" style="max-width:160px" placeholder="显示名称" value="${escapeHtml(s.name || "")}" data-k="name">
      <input type="text" placeholder="expected_domain（域名安全校验，如 baidu.com）" value="${escapeHtml(s.expected_domain || "")}" data-k="expected_domain">
      <button class="btn small danger" data-del="${i}">删</button>
    </div>`).join("");

  wrap.querySelectorAll(".list-item input").forEach(inp => {
    inp.addEventListener("input", () => {
      const idx = +inp.closest(".list-item").dataset.index;
      state.data.platforms.sources[idx][inp.dataset.k] = inp.value;
    });
  });
  wrap.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => {
      state.data.platforms.sources.splice(+btn.dataset.del, 1);
      renderSourceList();
    });
  });
}

function renderFeedList() {
  const wrap = $("feedList");
  const feeds = state.data.rss.feeds || [];
  wrap.innerHTML = feeds.map((f, i) => `
    <div class="list-item" data-index="${i}">
      <input type="text" style="max-width:140px" placeholder="id" value="${escapeHtml(f.id || "")}" data-k="id">
      <input type="text" style="max-width:150px" placeholder="名称" value="${escapeHtml(f.name || "")}" data-k="name">
      <input type="text" placeholder="订阅地址 https://" value="${escapeHtml(f.url || "")}" data-k="url">
      <label class="row-check"><input type="checkbox" data-k="enabled" ${f.enabled !== false ? "checked" : ""}>启用</label>
      <input type="number" style="max-width:80px" placeholder="天数" value="${f.max_age_days ?? ""}" data-k="max_age_days" title="最大文章年龄（天，覆盖全局）">
      <button class="btn small danger" data-del="${i}">删</button>
    </div>`).join("");

  wrap.querySelectorAll(".list-item input").forEach(inp => {
    inp.addEventListener(inp.type === "checkbox" ? "change" : "input", () => {
      const idx = +inp.closest(".list-item").dataset.index;
      if (inp.type === "checkbox") {
        state.data.rss.feeds[idx].enabled = inp.checked;
      } else if (inp.dataset.k === "max_age_days") {
        const num = parseInt(inp.value, 10);
        if (!isNaN(num)) state.data.rss.feeds[idx].max_age_days = num;
        else delete state.data.rss.feeds[idx].max_age_days;
      } else {
        state.data.rss.feeds[idx][inp.dataset.k] = inp.value;
      }
    });
  });
  wrap.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => {
      state.data.rss.feeds.splice(+btn.dataset.del, 1);
      renderFeedList();
    });
  });
}

/* ══════════════ 推送内容分区 ══════════════ */

const REGION_LABELS = {
  new_items: "🆕 新增热点",
  hotlist: "🔥 热榜（关键词/AI 筛选）",
  rss: "📰 RSS 订阅",
  standalone: "📌 独立展示区",
  ai_analysis: "🤖 AI 分析",
};

function renderDisplay() {
  const container = $("panel-display");
  const order = state.data.display.region_order || [];
  const regions = state.data.display.regions || {};

  container.innerHTML = `
    <div class="card">
      <h2>推送长度控制</h2>
      <div id="capFields"></div>
    </div>
    <div class="card">
      <h2>跨平台同事件聚合</h2>
      <div id="clusterFields"></div>
    </div>
    <div class="card">
      <h2>区域显示顺序与开关</h2>
      <p class="card-hint">从上到下 = 推送中从上到下；勾选才会显示该区域（同时写入 display.regions 开关）。用 ↑↓ 调整顺序。</p>
      <div id="regionList"></div>
    </div>
    <div class="card">
      <h2>独立展示区（不受关键词过滤）</h2>
      <p class="card-hint">推送展示由上方「独立展示区」开关控制；是否纳入 AI 分析由 ai_analysis.include_standalone 控制</p>
      <div id="standaloneFields"></div>
      <div style="margin-top:8px"><strong>完整展示的热榜平台</strong> <div id="saPlatforms"></div></div>
      <div style="margin-top:8px"><strong>完整展示的 RSS 源</strong> <div id="saFeeds"></div></div>
    </div>
    ${saveBar()}`;

  $("capFields").innerHTML =
    renderField({ path: "display.max_items_per_message", label: "每条消息最大条目数", type: "number", hint: "推送中所有区域合计的展示上限，超出部分显示提示行；0=不限制。开启摘要后建议 30~50" });
  $("clusterFields").innerHTML =
    renderField({ path: "display.event_clustering.enabled", label: "启用同事件聚合", type: "bool", hint: "同一事件在多个平台上榜时合并为一条，标注'同见: 其他平台'" }) +
    renderField({ path: "display.event_clustering.similarity_threshold", label: "标题相似度阈值", type: "number", hint: "0~1，越大越严格；0.6 适合多数场景" });
  bindFieldEvents(container);

  const renderRegions = () => {
    const list = $("regionList");
    list.innerHTML = order.map((name, i) => `
      <div class="region-item">
        <label class="row-check"><input type="checkbox" data-region="${escapeHtml(name)}" ${(regions[name] ?? false) ? "checked" : ""}></label>
        <span class="region-name">${REGION_LABELS[name] || escapeHtml(name)} <code style="color:var(--text-muted);font-size:11px">${escapeHtml(name)}</code></span>
        <button class="btn small" data-up="${i}" ${i === 0 ? "disabled" : ""}>↑</button>
        <button class="btn small" data-down="${i}" ${i === order.length - 1 ? "disabled" : ""}>↓</button>
      </div>`).join("");

    list.querySelectorAll("input[data-region]").forEach(inp => {
      inp.addEventListener("change", () => {
        state.data.display.regions[inp.dataset.region] = inp.checked;
      });
    });
    list.querySelectorAll("button[data-up]").forEach(btn => {
      btn.addEventListener("click", () => {
        const i = +btn.dataset.up;
        [order[i - 1], order[i]] = [order[i], order[i - 1]];
        renderRegions();
      });
    });
    list.querySelectorAll("button[data-down]").forEach(btn => {
      btn.addEventListener("click", () => {
        const i = +btn.dataset.down;
        [order[i + 1], order[i]] = [order[i], order[i + 1]];
        renderRegions();
      });
    });
  };
  renderRegions();

  $("standaloneFields").innerHTML = renderField({ path: "display.standalone.max_items", label: "每源最多展示条数", type: "number", hint: "0 = 不限制" });
  bindFieldEvents(container);

  // 平台/RSS 复选列表
  const renderChecks = (wrapId, items, idKey, path) => {
    const selected = state.data.display.standalone[path] || [];
    const wrap = $(wrapId);
    wrap.innerHTML = (items || []).map(item => `
      <label class="row-check" style="margin-right:14px;padding:4px 0">
        <input type="checkbox" value="${escapeHtml(item[idKey])}" ${selected.includes(item[idKey]) ? "checked" : ""}>
        ${escapeHtml(item.name || item[idKey])}
      </label>`).join("") || `<span style="color:var(--text-muted);font-size:12px">无可用项</span>`;

    wrap.querySelectorAll("input[type=checkbox]").forEach(inp => {
      inp.addEventListener("change", () => {
        const arr = state.data.display.standalone[path];
        if (inp.checked) { if (!arr.includes(inp.value)) arr.push(inp.value); }
        else state.data.display.standalone[path] = arr.filter(v => v !== inp.value);
      });
    });
  };
  renderChecks("saPlatforms", state.data.platforms.sources, "id", "platforms");
  renderChecks("saFeeds", state.data.rss.feeds, "id", "rss_feeds");

  container.querySelector(".btn-save-config").addEventListener("click", () => saveConfig());
}

/* ══════════════ 通知分区 ══════════════ */

const CHANNEL_DEFS = [
  { key: "feishu", name: "飞书", creds: ["webhook_url"], activate: ["webhook_url"] },
  { key: "dingtalk", name: "钉钉", creds: ["webhook_url"], activate: ["webhook_url"] },
  { key: "wework", name: "企业微信", creds: ["webhook_url"], activate: ["webhook_url"], extra: [
    { path: "notification.channels.wework.msg_type", label: "消息类型", type: "select", options: [["markdown", "markdown（群机器人）"], ["text", "text（个人微信应用）"]] },
  ] },
  { key: "telegram", name: "Telegram", creds: ["bot_token", "chat_id"], activate: ["bot_token", "chat_id"] },
  { key: "email", name: "邮件", creds: ["password"], activate: ["from", "password", "to"], extra: [
    { path: "notification.channels.email.from", label: "发件人邮箱" },
    { path: "notification.channels.email.to", label: "收件人邮箱", hint: "多个用逗号分隔" },
    { path: "notification.channels.email.smtp_server", label: "SMTP 服务器", hint: "留空自动识别" },
    { path: "notification.channels.email.smtp_port", label: "SMTP 端口", hint: "留空自动识别" },
  ] },
  { key: "ntfy", name: "ntfy", creds: ["token"], activate: ["topic"], extra: [
    { path: "notification.channels.ntfy.server_url", label: "服务器地址", hint: "可改为自托管" },
    { path: "notification.channels.ntfy.topic", label: "主题" },
  ] },
  { key: "bark", name: "Bark", creds: ["url"], activate: ["url"] },
  { key: "slack", name: "Slack", creds: ["webhook_url"], activate: ["webhook_url"] },
  { key: "generic_webhook", name: "通用 Webhook", creds: ["webhook_url"], activate: ["webhook_url"], extra: [
    { path: "notification.channels.generic_webhook.payload_template", label: "Payload 模板", type: "textarea", hint: "支持 {title} 和 {content} 占位符，如 {\"content\": \"{content}\"}" },
  ] },
];

const CRED_PATHS_SET = new Set();

function renderNotification() {
  const container = $("panel-notification");
  CRED_PATHS_SET.clear();

  const credNote = (envName, configured) => {
    if (envName) return `✅ 由环境变量 ${envName} 生效（修改此项无效）`;
    if (state.secretsEditable) return configured ? "已写入 config.yaml，保存后下次运行生效" : "未填写，填写并保存即可启用";
    return configured ? "已写入 config.yaml（建议迁移到 docker/.env 环境变量）" : "未配置";
  };

  const cards = CHANNEL_DEFS.map(ch => {
    const credFields = ch.creds.map(k => {
      const path = `notification.channels.${ch.key}.${k}`;
      CRED_PATHS_SET.add(path);
      const envName = state.envShadowed[path];
      const configured = envName || (getPath(state.data, path) || "").trim();
      return renderField({ path, label: k, type: "credential" }) +
        `<div style="font-size:11.5px;color:var(--text-muted);margin:-2px 0 6px 232px">
          ${credNote(envName, configured)}
        </div>`;
    }).join("");
    const extraFields = (ch.extra || []).map(renderField).join("");
    return `<div class="channel-card">
      <h3>${ch.name}<span class="ch-status ${channelOn(ch) ? "on" : "off"}">${channelOn(ch) ? "生效中" : "未配置"}</span></h3>
      ${credFields}${extraFields}
    </div>`;
  }).join("");

  const tip = state.secretsEditable
    ? `🔓 凭据编辑已开启（EDIT_SECRETS=true）：下方凭据可直接修改并保存到 config.yaml，点「立即运行」生效。
       带 <span class="badge env">env</span> 徽标的字段仍以环境变量为准（需清空 docker/.env 中对应变量后才能改为页面管理）。`
    : `🔒 安全设计：各渠道凭据（webhook/token/密码）通过 <strong>docker/.env 环境变量</strong>管理，页面仅脱敏展示、不可修改。
       如需在页面直接编辑凭据，请在 docker/.env 中设置 <code>EDIT_SECRETS=true</code> 后重启容器。多账号用英文分号(;)分隔。`;

  container.innerHTML = `
    <div class="welcome-tip">${tip}</div>
    <div class="card">
      <h2>通知总开关</h2>
      <div id="notifEnabled"></div>
    </div>
    <div class="channel-grid">${cards}</div>
    ${saveBar()}`;

  $("notifEnabled").innerHTML = renderField({ path: "notification.enabled", label: "启用通知", type: "bool", hint: "总开关：false 时永不推送" });
  bindFieldEvents(container);
  container.querySelector(".btn-save-config").addEventListener("click", () => saveConfig());
}

function channelOn(ch) {
  // 渠道是否生效：激活字段（与主程序 loader 判定一致）全部有值
  // —— 环境变量覆盖视为有值；config.yaml 非空（含掩码占位）视为有值
  const base = `notification.channels.${ch.key}.`;
  return ch.activate.every(k => {
    const path = base + k;
    if (state.envShadowed[path]) return true;
    const v = getPath(state.data, path);
    return typeof v === "string" && v.trim() !== "";
  });
}

/* ══════════════ AI 分区 ══════════════ */

function renderAI() {
  renderFields("panel-ai", [
    {
      title: "AI 模型（ai_analysis / ai_translation / ai_filter 共用）",
      hint: "LiteLLM 格式，如 deepseek/deepseek-v4-flash、openai/gpt-4o、gemini/gemini-2.5-flash；API Key 可通过环境变量 AI_API_KEY 管理，开启 EDIT_SECRETS 后也可在此修改",
      fields: [
        { path: "ai.model", label: "模型" },
        { path: "ai.api_key", label: "API Key", type: "credential" },
        { path: "ai.api_base", label: "自定义 API 地址", type: "url", hint: "主流服务商留空；自定义中转站时 model 需加 openai/ 前缀" },
        { path: "ai.timeout", label: "请求超时（秒）", type: "number" },
        { path: "ai.temperature", label: "采样温度", type: "number", hint: "0.0~2.0；部分模型要求必须 1.0" },
        { path: "ai.max_tokens", label: "最大生成 token 数", type: "number", hint: "API 报 HTTP 400 时设为 0 禁用" },
        { path: "ai.num_retries", label: "失败重试次数", type: "number" },
        { path: "ai.fallback_models", label: "备用模型", type: "csvlist" },
      ],
    },
    {
      title: "热榜条目 AI 摘要",
      hint: "为进入推送的热榜条目生成一句话摘要（🤖 行），让推送\"看完就懂\"；消耗少量 token",
      fields: [
        { path: "ai_summary.enabled", label: "启用条目摘要", type: "bool" },
        { path: "ai_summary.max_items", label: "每次运行最多条数", type: "number", hint: "按显示顺序取前 N 条，上限 100" },
        { path: "ai_summary.max_chars", label: "单条摘要最大长度", type: "number" },
        { path: "ai_summary.batch_size", label: "每批标题数", type: "number" },
        { path: "ai_summary.batch_interval", label: "分批间隔（秒）", type: "number" },
      ],
    },
    {
      title: "AI 分析",
      fields: [
        { path: "ai_analysis.enabled", label: "启用 AI 分析", type: "bool", hint: "总开关；开启调度后由调度控制" },
        { path: "ai_analysis.language", label: "分析报告语言" },
        {
          path: "ai_analysis.mode", label: "分析模式", type: "select", options: [
            ["follow_report", "follow_report —— 跟随报告模式"],
            ["daily", "daily"], ["current", "current"], ["incremental", "incremental"],
          ],
        },
        { path: "ai_analysis.max_news_for_analysis", label: "参与分析的新闻上限", type: "number", hint: "控制 token 成本的关键项" },
        { path: "ai_analysis.include_rss", label: "包含 RSS 内容", type: "bool" },
        { path: "ai_analysis.include_standalone", label: "纳入独立展示区数据", type: "bool" },
        { path: "ai_analysis.include_rank_timeline", label: "完整排名轨迹", type: "bool", hint: "更准但多耗 0.5~1 倍 token" },
        { path: "ai_analysis.prompt_file", label: "分析提示词文件", hint: "内容在「文本文件」标签编辑" },
      ],
    },
    {
      title: "AI 翻译",
      fields: [
        { path: "ai_translation.enabled", label: "启用翻译", type: "bool" },
        { path: "ai_translation.language", label: "目标语言" },
        { path: "ai_translation.batch_size", label: "每批标题数", type: "number" },
        { path: "ai_translation.batch_interval", label: "分批间隔（秒）", type: "number" },
        { path: "ai_translation.scope.hotlist", label: "翻译热榜标题", type: "bool" },
        { path: "ai_translation.scope.rss", label: "翻译 RSS 标题", type: "bool" },
        { path: "ai_translation.scope.standalone", label: "翻译独立展示区标题", type: "bool" },
      ],
    },
  ]);
}

/* ══════════════ 词组结构化编辑器 ══════════════ */

function parseEntryInput(input, forceKind) {
  // 输入解析：/pattern/flags 或 pattern => display 或普通词
  const raw = input.trim();
  if (!raw) return null;
  const arrowIdx = raw.indexOf("=>");
  let text = raw;
  let display = "";
  if (arrowIdx > 0) {
    text = raw.slice(0, arrowIdx).trim();
    display = raw.slice(arrowIdx + 2).trim();
  }
  const regexMatch = text.match(/^\/(.+)\/([a-z]*)$/);
  if (regexMatch && forceKind !== "word") {
    return { kind: "regex", text: regexMatch[1], flags: regexMatch[2] || "", display };
  }
  if (forceKind === "regex" && !regexMatch) {
    return { kind: "regex", text, flags: "", display };
  }
  return { kind: "word", text, display };
}

function entryLabel(entry) {
  let text = entry.kind === "regex" ? `/${entry.text}/${entry.flags || ""}` : entry.text;
  if (entry.display) text += ` => ${entry.display}`;
  return text;
}

function renderChips(containerId, list, chipClass, namePrefix) {
  // namePrefix: "gfilters" | "filters" | "gnormal:组下标" | "grequired:组下标"
  const wrap = $(containerId);
  wrap.innerHTML = (list || []).map((entry, i) => `
    <span class="chip ${chipClass}">${escapeHtml(entryLabel(entry))}
      <button data-remove="${namePrefix}:${i}" title="删除">×</button>
    </span>`).join("") || `<span style="color:var(--text-muted);font-size:12px">（无）</span>`;

  wrap.querySelectorAll("button[data-remove]").forEach(btn => {
    btn.addEventListener("click", () => chipRemoveHandler(btn.dataset.remove));
  });
}

function renderFreqEditor() {
  const model = state.freqModel;
  const container = $("freqStructured");

  const groupHtml = (model.groups || []).map((g, gi) => `
    <div class="freq-group" data-gi="${gi}">
      <div class="freq-group-header">
        <span class="freq-group-label">组别名</span>
        <input type="text" data-galias="${gi}" value="${escapeHtml(g.alias || "")}" placeholder="可选，如：华为">
        <span class="freq-group-label">条数上限</span>
        <input type="number" data-gmax="${gi}" value="${g.max_count || 0}" style="max-width:90px" title="0=不限制">
        <button class="btn small danger" data-gdel="${gi}">删除词组</button>
      </div>
      <div class="freq-section-title">普通词（命中任意一个即匹配）</div>
      <div class="chip-list" id="gnormal-${gi}"></div>
      <div class="chip-add">
        <input type="text" data-gadd="${gi}" placeholder="输入关键词或 /正则/ 或 词 => 别名，回车添加">
      </div>
      <div class="freq-section-title">必须词（全部命中才匹配，可空）</div>
      <div class="chip-list" id="grequired-${gi}"></div>
    </div>`).join("");

  container.innerHTML = `
    <div class="freq-section-title">全局过滤词（[GLOBAL_FILTER] 区，包含即排除）</div>
    <div class="chip-list" id="gfilterList"></div>
    <div class="chip-add"><input type="text" id="addGFilter" placeholder="输入要排除的词或 /正则/，回车添加"></div>

    <div class="freq-section-title">过滤词（! 开头，全局生效，主程序实现如此）</div>
    <div class="chip-list" id="filterList"></div>
    <div class="chip-add"><input type="text" id="addFilter" placeholder="输入要过滤的词或 /正则/，回车添加"></div>

    <div class="freq-section-title" style="display:flex;align-items:center;gap:10px">
      词组（${(model.groups || []).length} 组）
      <button class="btn small" id="btnAddGroup">＋ 新增词组</button>
    </div>
    ${groupHtml}
    <div class="btn-row">
      <button class="btn primary" id="btnSaveFreq">💾 保存关键词</button>
      <span style="color:var(--text-muted);font-size:12.5px">保存前自动校验并备份；改动在下次运行时生效</span>
    </div>`;

  renderChips("gfilterList", model.global_filters, "", "gfilters");
  renderChips("filterList", model.filters, "chip-filter", "filters");

  (model.groups || []).forEach((g, gi) => {
    renderChips(`gnormal-${gi}`, g.normal, "", `gnormal:${gi}`);
    renderChips(`grequired-${gi}`, g.required, "chip-required", `grequired:${gi}`);
  });

  // 事件：组别名/条数
  container.querySelectorAll("input[data-galias]").forEach(inp => {
    inp.addEventListener("input", () => { model.groups[+inp.dataset.galias].alias = inp.value; });
  });
  container.querySelectorAll("input[data-gmax]").forEach(inp => {
    inp.addEventListener("change", () => {
      const num = parseInt(inp.value, 10) || 0;
      model.groups[+inp.dataset.gmax].max_count = num;
    });
  });
  container.querySelectorAll("button[data-gdel]").forEach(btn => {
    btn.addEventListener("click", () => {
      model.groups.splice(+btn.dataset.gdel, 1);
      renderFreqEditor();
    });
  });
  // 事件：词条输入框回车添加
  container.querySelectorAll("input[data-gadd]").forEach(inp => {
    inp.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const gi = +inp.dataset.gadd;
      const entry = parseEntryInput(inp.value);
      if (!entry) return;
      model.groups[gi].normal.push(entry);
      inp.value = "";
      renderFreqEditor();
    });
  });
  const addGFilter = $("addGFilter");
  addGFilter.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const entry = parseEntryInput(addGFilter.value);
    if (!entry) return;
    model.global_filters.push(entry);
    addGFilter.value = "";
    renderFreqEditor();
  });
  const addFilter = $("addFilter");
  addFilter.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const entry = parseEntryInput(addFilter.value);
    if (!entry) return;
    model.filters.push(entry);
    addFilter.value = "";
    renderFreqEditor();
  });
  $("btnAddGroup").addEventListener("click", () => {
    model.groups.push({ alias: "", required: [], normal: [], max_count: 0 });
    renderFreqEditor();
  });
  $("btnSaveFreq").addEventListener("click", saveFreqStructured);
}

function chipRemoveHandler(name, idx) {
  // 形如 "gnormal:2:3" → 列表定位
  const parts = name.split(":");
  if (parts[0] === "gfilters") { state.freqModel.global_filters.splice(+parts[1], 1); }
  else if (parts[0] === "filters") { state.freqModel.filters.splice(+parts[1], 1); }
  else if (parts[0] === "gnormal") { state.freqModel.groups[+parts[1]].normal.splice(+parts[2], 1); }
  else if (parts[0] === "grequired") { state.freqModel.groups[+parts[1]].required.splice(+parts[2], 1); }
  renderFreqEditor();
}

async function loadFreqStructured() {
  try {
    state.freqModel = await api("/api/frequency");
  } catch (e) {
    toast("词组解析失败：" + e.message, "error");
    state.freqModel = { header: "", global_filters: [], filters: [], groups: [] };
  }
}

async function saveFreqStructured() {
  try {
    const result = await api("/api/frequency", { method: "PUT", body: JSON.stringify({ model: state.freqModel }) });
    toast(result.message || "关键词已保存", "success");
    $("textEditorStatus").textContent = `已保存（${result.groups} 个词组）${result.backup ? "，已备份" : ""}`;
    // 刷新原始文本缓存
    const data = await api("/api/file/frequency_words.txt");
    state.activeFileText = data.text;
    $("textEditor").value = data.text;
  } catch (e) {
    const detail = (e.data && e.data.errors) ? e.data.errors : [e.message];
    detail.forEach(d => toast(d, "error"));
  }
}

function setFreqMode(mode) {
  state.freqMode = mode;
  const isStructured = mode === "structured";
  $("freqStructured").style.display = isStructured ? "block" : "none";
  $("textEditor").style.display = isStructured ? "none" : "block";
  $("btnSaveText").style.display = isStructured ? "none" : "inline-block";
  $("btnFreqStructured").classList.toggle("active", isStructured);
  $("btnFreqRaw").classList.toggle("active", !isStructured);
  if (isStructured) renderFreqEditor();
}

/* ══════════════ 文本文件编辑 ══════════════ */

async function renderTextEditor() {
  const tabs = $("fileTabs");
  tabs.innerHTML = state.textFiles.map(f =>
    `<button data-file="${escapeHtml(f.name)}">${escapeHtml(f.label)}</button>`).join("")
    || `<span style="color:var(--text-muted)">未找到可编辑文件</span>`;

  tabs.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => loadTextFile(btn.dataset.file));
  });

  // 默认选中 frequency_words.txt
  const preferred = state.textFiles.find(f => f.name === "frequency_words.txt") || state.textFiles[0];
  if (preferred) await loadTextFile(preferred.name);
  $("freqSyntaxHelp").style.display = state.activeFile === "frequency_words.txt" ? "block" : "none";
}

async function loadTextFile(name) {
  try {
    const data = await api(`/api/file/${encodeURIComponent(name)}`);
    state.activeFile = name;
    state.activeFileText = data.text;
    $("textEditor").value = data.text;
    $("textEditorStatus").textContent = "";
    $("fileTabs").querySelectorAll("button").forEach(b =>
      b.classList.toggle("active", b.dataset.file === name));
    $("freqSyntaxHelp").style.display = name === "frequency_words.txt" ? "block" : "none";

    // 关键词文件：提供结构化/原始双模式
    const isFreq = name === "frequency_words.txt";
    $("freqModeToggle").style.display = isFreq ? "block" : "none";
    if (isFreq) {
      await loadFreqStructured();
      setFreqMode("structured");
    } else {
      setFreqMode("raw");
      $("freqStructured").style.display = "none";
    }
  } catch (e) {
    toast(e.message, "error");
  }
}

async function saveTextFile() {
  if (!state.activeFile) return;
  try {
    const result = await api(`/api/file/${encodeURIComponent(state.activeFile)}`, {
      method: "PUT",
      body: JSON.stringify({ text: $("textEditor").value }),
    });
    state.activeFileText = $("textEditor").value;
    $("textEditorStatus").textContent = `已保存 ${new Date().toLocaleTimeString()}${result.backup ? "（已备份）" : ""}`;
    toast(result.message || "文件已保存", "success");
    if (result.warnings && result.warnings.length) toast(result.warnings.join("；"), "warn");
  } catch (e) {
    toast(e.message, "error");
  }
}

/* ══════════════ 原始 YAML 编辑 ══════════════ */

async function renderRawEditor() {
  $("rawEditorHint").innerHTML = state.secretsEditable
    ? '兜底编辑器，可修改所有配置项（含凭据，当前 EDIT_SECRETS 已开启）。保存前进行语法与结构校验。'
    : '兜底编辑器，可修改所有配置项（密钥字段已脱敏，无法通过此处修改；开启 EDIT_SECRETS 后可编辑）。保存前进行语法与结构校验。';
  try {
    const data = await api("/api/config/raw");
    $("rawEditor").value = data.text;
  } catch (e) {
    toast(e.message, "error");
  }
}

async function saveRawEditor() {
  try {
    const result = await api("/api/config/raw", {
      method: "PUT",
      body: JSON.stringify({ text: $("rawEditor").value }),
    });
    toast(result.message || "已保存", "success");
    if (result.warnings && result.warnings.length) toast(result.warnings.join("；"), "warn");
    await loadStructuredConfig(); // 重新加载表单
  } catch (e) {
    const detail = (e.data && e.data.errors) ? e.data.errors : [e.message];
    toast(detail.join("；"), "error");
  }
}

/* ══════════════ 运行日志 ══════════════ */

async function renderLogsPanel() {
  try {
    const data = await api("/api/run/logs");
    const tabs = $("logTabs");
    tabs.innerHTML = (data.logs || []).map(l =>
      `<button data-log="${escapeHtml(l.name)}" title="${escapeHtml(l.mtime)} · ${fmtBytes(l.size)}">${escapeHtml(l.name)}</button>`).join("")
      || `<span style="color:var(--text-muted)">暂无运行日志</span>`;

    tabs.querySelectorAll("button").forEach(btn => {
      btn.addEventListener("click", () => viewRunLog(btn.dataset.log));
    });
    if (data.logs && data.logs.length && !state.activeLog) viewRunLog(data.logs[0].name);
  } catch (e) {
    toast(e.message, "error");
  }
}

async function viewRunLog(name) {
  state.activeLog = name;
  $("logTabs").querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.log === name));
  try {
    const data = await api(`/api/run/logs/${encodeURIComponent(name)}`);
    $("logView").textContent = data.text || "（空日志）";
    $("logView").scrollTop = 0;
  } catch (e) {
    $("logView").textContent = e.message;
  }
}

/* ══════════════ 概览 ══════════════ */

function renderOverview() {
  const s = state.status;
  $("version").textContent = `v${s.version}`;

  const authBadge = $("authBadge");
  if (s.admin_auth_enabled) {
    authBadge.textContent = "🔐 已启用访问控制";
    authBadge.className = "auth-badge ok";
  } else {
    authBadge.textContent = "⚠️ 未设置 ADMIN_TOKEN（只读模式）";
    authBadge.className = "auth-badge";
    $("welcomeTip").innerHTML = "⚠️ <strong>当前未设置 ADMIN_TOKEN，页面处于只读模式</strong>。请在 docker/.env 中设置 <code>ADMIN_TOKEN=你的令牌</code> 后重启容器以启用编辑与运行功能。";
  }

  const cronDesc = s.cron_schedule || "未设置";
  $("statusGrid").innerHTML = `
    <div class="status-card"><div class="s-label">定时调度</div><div class="s-value">${escapeHtml(cronDesc)}</div></div>
    <div class="status-card"><div class="s-label">运行模式</div><div class="s-value">${escapeHtml(s.run_mode)}${s.supercronic_pid1 ? " · supercronic 正常" : ""}</div></div>
    <div class="status-card"><div class="s-label">任务状态</div>
      <div class="s-value ${s.running ? "warn" : "ok"}">${s.running ? "运行中…" : "空闲"}</div></div>
    <div class="status-card"><div class="s-label">上次运行</div><div class="s-value" style="font-size:13px">${escapeHtml(s.last_run_time || "无记录")}</div></div>
    ${s.config_files.map(f => `
      <div class="status-card"><div class="s-label">${escapeHtml(f.label)}（${escapeHtml(f.name)}）</div>
      <div class="s-value" style="font-size:13px">更新于 ${escapeHtml(f.mtime)}</div></div>`).join("")}
  `;

  refreshOverviewLog();
  renderDataServiceCard(s);

  if (!s.admin_auth_enabled) {
    document.querySelectorAll(".btn-save-config, #btnSaveText, #btnSaveRaw, #btnSaveFreq").forEach(b => b.disabled = true);
  }
}

function renderDataServiceCard(s) {
  const base = location.origin;
  const token = state.newsToken || "";
  const t = token ? `?token=${encodeURIComponent(token)}` : "";
  const rows = [
    ["RSS 全部（热榜+RSS）", `${base}/feed/all.xml${t}`],
    ["RSS 热榜", `${base}/feed/hotlist.xml${t}`],
    ["RSS 订阅源文章", `${base}/feed/rss.xml${t}`],
    ["JSON 热榜", `${base}/api/news/hotlist?days=1${token ? `&token=${encodeURIComponent(token)}` : ""}`],
    ["JSON 搜索", `${base}/api/news/search?q=关键词&days=7`],
  ];
  const kwNote = state.newsGroups
    ? state.newsGroups.map(g => `${base}/feed/kw-${encodeURIComponent(g)}.xml${t}`).join("<br>")
    : "";

  $("dataServiceCard").innerHTML = `
    <table style="width:100%;font-size:12.5px;border-collapse:collapse">
      ${rows.map(([label, url]) => `
        <tr>
          <td style="padding:5px 8px 5px 0;white-space:nowrap;color:var(--text-muted)">${label}</td>
          <td style="padding:5px 0"><code style="word-break:break-all">${escapeHtml(url)}</code></td>
          <td style="padding:5px;white-space:nowrap"><button class="btn small" data-copy="${escapeHtml(url)}">复制</button></td>
        </tr>`).join("")}
    </table>
    <div style="font-size:12px;color:var(--text-muted);margin-top:6px">
      ${s.news_token_enabled ? "🔒 已启用 NEWS_API_TOKEN（URL 需带 ?token=）" : "未设置 NEWS_API_TOKEN（本地绑定下可直接访问；对外暴露时建议设置）"}
      <br>按关键词词组订阅：${kwNote || "见「文本文件」页词组，地址格式 /feed/kw-{词组名}.xml"}
    </div>`;

  $("dataServiceCard").querySelectorAll("button[data-copy]").forEach(btn => {
    btn.addEventListener("click", () => {
      navigator.clipboard.writeText(btn.dataset.copy).then(() => toast("已复制", "success"));
    });
  });
}

async function refreshOverviewLog() {
  try {
    const data = await api("/api/run/status");
    $("overviewLog").textContent = data.tail || "暂无运行日志";
    $("overviewLog").scrollTop = $("overviewLog").scrollHeight;
  } catch (e) {
    $("overviewLog").textContent = "日志加载失败：" + e.message;
  }
}

/* ══════════════ 运行控制 ══════════════ */

async function runNow() {
  try {
    const result = await api("/api/run", { method: "POST", body: JSON.stringify({}) });
    toast(result.message, "success");
    startPolling(result.log_name);
  } catch (e) {
    toast(e.message, e.status === 403 ? "warn" : "error");
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      const data = await api("/api/run/status");
      $("overviewLog").textContent = data.tail || "";
      $("overviewLog").scrollTop = $("overviewLog").scrollHeight;
      if (!data.running) {
        stopPolling();
        toast("任务运行结束，可在「运行日志」查看结果", "success");
        renderOverview();
      }
    } catch { /* 轮询失败忽略 */ }
  }, 3000);
}

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

async function testNotification() {
  const btn = $("btnTestNotify");
  btn.disabled = true;
  btn.textContent = "发送中…";
  toast("正在发送测试通知（最多等待 90 秒）…");
  try {
    const result = await api("/api/test-notification", { method: "POST", body: JSON.stringify({}) });
    if (result.ok) {
      toast("测试通知已发送，请检查你的消息渠道", "success");
      console.log(result.output);
    } else {
      toast(result.message || "发送失败", "error");
      if (result.output) console.log(result.output);
    }
  } catch (e) {
    toast(e.message, e.status === 403 ? "warn" : "error");
    if (e.data && e.data.output) console.log(e.data.output);
  } finally {
    btn.disabled = false;
    btn.textContent = "🔔 测试通知";
  }
}

/* ══════════════ 配置保存 ══════════════ */

function computeDiff() {
  const changes = [];
  const walk = (oldV, newV, path) => {
    if (typeof oldV === "object" && oldV !== null && typeof newV === "object" && newV !== null) {
      const keys = new Set([...Object.keys(oldV), ...Object.keys(newV)]);
      for (const k of keys) walk(oldV[k], newV[k], path ? `${path}.${k}` : k);
    } else if (JSON.stringify(oldV) !== JSON.stringify(newV)) {
      const brief = v => {
        if (v === undefined) return "(删除)";
        const s = typeof v === "object" ? JSON.stringify(v) : String(v);
        return s.length > 50 ? s.slice(0, 50) + "…" : s;
      };
      changes.push(`${path}: ${brief(oldV)} → ${brief(newV)}`);
    }
  };
  walk(state.loaded, state.data, "");
  return changes;
}

function saveConfig() {
  const changes = computeDiff();
  const body = changes.length
    ? `<ul class="diff-list">${changes.map(c => `<li>${escapeHtml(c)}</li>`).join("")}</ul>`
    : `<p style="color:var(--text-muted)">没有检测到配置变化，仍可保存（例如刷新格式）。</p>`;
  $("confirmBody").innerHTML = `<p>以下配置将被写入 <code>config/config.yaml</code>（自动备份，下次运行生效）：</p>${body}`;
  $("confirmMask").classList.add("show");
}

async function confirmSave() {
  $("confirmMask").classList.remove("show");
  try {
    const result = await api("/api/config/structured", {
      method: "PUT",
      body: JSON.stringify({ data: state.data }),
    });
    toast(result.message || "配置已保存", "success");
    if (result.warnings && result.warnings.length) {
      result.warnings.forEach(w => toast(w, "warn"));
    }
    await loadStructuredConfig();
    await boot(false); // 刷新状态（配置文件 mtime 等）
  } catch (e) {
    const detail = (e.data && e.data.errors) ? e.data.errors : [e.message];
    detail.forEach(d => toast(d, "error"));
  }
}

/* ══════════════ 导航 ══════════════ */

function renderNav() {
  const nav = $("nav");
  nav.innerHTML = SECTIONS.map(s => `<button data-sec="${s.id}">${s.title}</button>`).join("");
  nav.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => switchSection(btn.dataset.sec));
  });
}

function switchSection(id) {
  $("nav").querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.sec === id));
  document.querySelectorAll("section.panel").forEach(p => p.classList.remove("active"));
  $(`panel-${id}`).classList.add("active");
  const section = SECTIONS.find(s => s.id === id);
  if (section && section.render) section.render();
}

/* ══════════════ 启动 ══════════════ */

async function loadStructuredConfig() {
  const data = await api("/api/config/structured");
  state.data = data.data;
  state.loaded = deepClone(data.data);
  state.envShadowed = data.env_shadowed || {};
  state.readonly = data.readonly_fields || [];
  state.secretsEditable = !!data.secrets_editable;
}

async function boot(rerender = true) {
  try {
    state.status = await api("/api/status");
    state.textFiles = state.status.text_files || [];
    state.newsToken = state.status.news_token || "";
    state.newsGroups = [];
    try {
      const groupsData = await api("/api/news/groups");
      state.newsGroups = (groupsData.groups || []).map(g => g.name);
    } catch { /* 词组未配置时忽略 */ }
    if (state.status.admin_auth_enabled && !state.token) { showLogin(); return; }
    await loadStructuredConfig();

    renderNav();
    switchSection("overview");
  } catch (e) {
    if (e.status !== 401) toast("加载失败：" + e.message, "error");
  }
}

/* ══════════════ OPML 导入 ══════════════ */

function openOpmlModal() {
  $("opmlMask").classList.add("show");
}

async function importOpml() {
  const content = $("opmlContent").value.trim();
  if (!content) { toast("请先粘贴 OPML 内容或选择文件", "warn"); return; }
  try {
    const result = await api("/api/rss/opml", { method: "POST", body: JSON.stringify({ content }) });
    toast(result.message || "导入完成", result.added > 0 ? "success" : "warn");
    if (result.warnings && result.warnings.length) toast(result.warnings.join("；"), "warn");
    $("opmlMask").classList.remove("show");
    $("opmlContent").value = "";
    // 重新加载配置并刷新数据源页
    await loadStructuredConfig();
    switchSection("sources");
  } catch (e) {
    toast(e.message, "error");
  }
}

function bindOpmlEvents() {
  $("btnOpmlImport").addEventListener("click", importOpml);
  $("btnOpmlCancel").addEventListener("click", () => $("opmlMask").classList.remove("show"));
  $("opmlFile").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { $("opmlContent").value = reader.result; };
    reader.readAsText(file);
  });
}

/* 事件绑定 */
$("btnRunNow").addEventListener("click", runNow);
$("btnTestNotify").addEventListener("click", testNotification);
$("btnLogin").addEventListener("click", doLogin);
$("loginToken").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
$("btnSaveText").addEventListener("click", saveTextFile);
$("btnSaveRaw").addEventListener("click", saveRawEditor);
$("btnConfirmSave").addEventListener("click", confirmSave);
$("btnCancelSave").addEventListener("click", () => $("confirmMask").classList.remove("show"));
$("btnFreqStructured").addEventListener("click", () => setFreqMode("structured"));
$("btnFreqRaw").addEventListener("click", () => setFreqMode("raw"));
// 结构化模式的保存按钮在 renderFreqEditor 中动态绑定
bindOpmlEvents();

boot();
