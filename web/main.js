/*
 * MC 整合包汉化共享库 —— 前端逻辑(纯静态,无框架)。
 *
 * 数据源:仓库根 index.json,结构由 src/index_store.py 定稿:
 *   { version, updatedAt, entries: [ { source, projectId, fileId, name,
 *     mcVersion, status, path, translatedAt, stats } ] }
 *   status ∈ pending | done | failed | no_ftbq | already_localized
 *   成品文件 = {path}/zh_cn.snbt
 *
 * 所有可配置项在 config.js;本文件不硬编码任何真实仓库地址。
 */
(() => {
  "use strict";

  const cfg = window.MC_WEB_CONFIG || {};

  const STATUS_META = {
    pending:           { label: "待翻译",   cls: "s-pending"   },
    done:              { label: "已汉化",   cls: "s-done"      },
    failed:            { label: "翻译失败", cls: "s-failed"    },
    no_ftbq:           { label: "无 FTBQ",  cls: "s-no-ftbq"   },
    already_localized: { label: "自带汉化", cls: "s-localized" },
  };

  const QUEUE_KEY = "mc_web.pending_submissions.v1";
  const SNBT_FILE = "zh_cn.snbt";
  const CF_URL_RE =
    /^https?:\/\/(?:www\.)?curseforge\.com\/minecraft\/modpacks\/([A-Za-z0-9._-]+)(?:\/files\/(\d+))?\/?(?:[?#].*)?$/i;

  const $ = (id) => document.getElementById(id);
  const grid = $("grid");
  const listMeta = $("list-meta");
  const demoBanner = $("demo-banner");
  const emptyState = $("empty-state");
  const errorState = $("error-state");
  const form = $("submit-form");
  const input = $("cf-url");
  const hint = $("form-hint");
  const queueWrap = $("queue-wrap");
  const queueList = $("queue-list");

  const params = new URLSearchParams(location.search);
  const forceDemo = params.get("demo") === "1";
  let usedSample = false;

  // ── index.json 加载 ────────────────────────────────────────────────────────
  // 依次尝试:配置地址 → 同目录 → 示例数据;?demo=1 时直接用示例。
  const candidates = (forceDemo
    ? ["sample/index.json"]
    : [cfg.INDEX_URL || "../index.json", "index.json", "sample/index.json"]
  ).filter(Boolean);

  async function loadIndex() {
    let lastErr = null;
    for (const url of candidates) {
      try {
        const resp = await fetch(url, { cache: "no-store" });
        if (!resp.ok) {
          lastErr = new Error(`${url} → HTTP ${resp.status}`);
          continue;
        }
        const data = await resp.json();
        if (!data || !Array.isArray(data.entries)) {
          lastErr = new Error(`${url} 结构异常:缺 entries 数组`);
          continue;
        }
        usedSample = /sample\//.test(url);
        return data;
      } catch (err) {
        lastErr = err;
      }
    }
    throw lastErr || new Error("找不到 index.json");
  }

  // ── 列表渲染 ──────────────────────────────────────────────────────────────
  function setHint(text, type) {
    hint.textContent = text || "";
    hint.className = "form-hint" + (type ? " " + type : "");
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }

  /** done 条目 → zh_cn.snbt 的 raw 链接;示例数据返回空串(按钮置灰)。 */
  function resolveRawUrl(entry) {
    const path = String(entry.path || "").replace(/^\/+|\/+$/g, "");
    if (!path || usedSample) return "";
    const file = `${path}/${SNBT_FILE}`;
    if (cfg.RAW_BASE) return `${String(cfg.RAW_BASE).replace(/\/+$/, "")}/${file}`;
    if (cfg.GITHUB_REPO) {
      return `https://raw.githubusercontent.com/${cfg.GITHUB_REPO}/${cfg.BRANCH || "main"}/${file}`;
    }
    // 未配置仓库:用站点相对路径(Pages 以仓库根为站点根、页面在 /web/ 时成立)
    return `../${file}`;
  }

  function row(label, value) {
    const div = document.createElement("div");
    div.className = "card-row";
    const k = document.createElement("span");
    k.className = "card-key";
    k.textContent = label;
    const v = document.createElement("span");
    v.className = "card-val";
    v.textContent = value;
    div.append(k, v);
    return div;
  }

  function renderCard(entry) {
    const card = document.createElement("article");
    card.className = "card";

    const head = document.createElement("div");
    head.className = "card-head";
    const title = document.createElement("h3");
    title.className = "card-title";
    title.textContent = entry.name || `${entry.source}/${entry.projectId}`;
    title.title = title.textContent;
    const badge = document.createElement("span");
    const meta = STATUS_META[entry.status] || { label: entry.status || "未知", cls: "s-unknown" };
    badge.className = `badge ${meta.cls}`;
    badge.textContent = meta.label;
    head.append(title, badge);

    const body = document.createElement("div");
    body.className = "card-body";
    body.append(
      row("MC 版本", entry.mcVersion || "—"),
      row("来源", `${entry.source || "cf"} / ${entry.projectId || "—"}` + (entry.fileId ? ` / ${entry.fileId}` : "")),
      row("更新时间", fmtTime(entry.translatedAt)),
    );

    const foot = document.createElement("div");
    foot.className = "card-foot";
    if (entry.status === "done") {
      const url = resolveRawUrl(entry);
      if (url) {
        const a = document.createElement("a");
        a.className = "btn btn-download";
        a.href = url;
        a.setAttribute("download", SNBT_FILE);
        a.textContent = `下载 ${SNBT_FILE}`;
        foot.appendChild(a);
      } else {
        const s = document.createElement("span");
        s.className = "btn btn-disabled";
        s.title = "示例数据或 path 缺失,无实际文件";
        s.textContent = "示例数据 · 无文件";
        foot.appendChild(s);
      }
    } else {
      const s = document.createElement("span");
      s.className = "muted small";
      s.textContent = entry.status === "pending"
        ? "翻译任务排队中,完成后这里会出现下载按钮"
        : "暂无成品文件";
      foot.appendChild(s);
    }

    card.append(head, body, foot);
    return card;
  }

  function renderList(index) {
    grid.textContent = "";
    const entries = Array.isArray(index.entries) ? index.entries : [];

    if (!entries.length) {
      emptyState.hidden = false;
      listMeta.textContent = "";
      return;
    }
    emptyState.hidden = true;

    const doneCount = entries.filter((e) => e.status === "done").length;
    listMeta.textContent = `共 ${entries.length} 个 · 已汉化 ${doneCount} 个`;

    const frag = document.createDocumentFragment();
    for (const entry of entries) frag.appendChild(renderCard(entry));
    grid.appendChild(frag);

    if (usedSample) {
      demoBanner.hidden = false;
      demoBanner.textContent =
        "当前展示的是示例数据(web/sample/index.json):仓库根还没有 index.json,或页面被 ?demo=1 强制指定。";
    }
  }

  function renderError(err) {
    errorState.hidden = false;
    errorState.textContent =
      `读取 index.json 失败:${err && err.message ? err.message : err}。` +
      "若直接双击打开本页,浏览器会禁止 fetch 本地文件;" +
      "请在仓库根目录运行 python -m http.server 8000 后访问 http://localhost:8000/web/。";
  }

  // ── 本地待办队列(localStorage)────────────────────────────────────────────
  function readQueue() {
    try {
      const raw = localStorage.getItem(QUEUE_KEY);
      const list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list : [];
    } catch {
      return [];
    }
  }

  function writeQueue(list) {
    try {
      localStorage.setItem(QUEUE_KEY, JSON.stringify(list));
    } catch {
      /* 隐私模式等场景下写不进去,忽略 */
    }
  }

  function queueAdd(url) {
    const list = readQueue();
    if (list.some((it) => it.url === url)) return false;
    list.unshift({ url, at: new Date().toISOString() });
    writeQueue(list);
    return true;
  }

  function queueRemove(url) {
    writeQueue(readQueue().filter((it) => it.url !== url));
    renderQueue();
  }

  function renderQueue() {
    const list = readQueue();
    queueWrap.hidden = list.length === 0;
    queueList.textContent = "";
    for (const item of list) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = item.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = item.url;
      const time = document.createElement("span");
      time.className = "muted small";
      time.textContent = fmtTime(item.at);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn-remove";
      del.textContent = "移除";
      del.addEventListener("click", () => queueRemove(item.url));
      li.append(a, time, del);
      queueList.appendChild(li);
    }
  }

  // ── 提交触发(接线点)──────────────────────────────────────────────────────
  function effectiveMode() {
    const mode = cfg.DISPATCH_MODE || "queue";
    if (mode === "issue" && cfg.GITHUB_REPO) return "issue";
    if (mode === "repository_dispatch" && cfg.DISPATCH_PROXY && cfg.GITHUB_REPO) {
      return "repository_dispatch";
    }
    return "queue";
  }

  /**
   * 触发一次翻译任务。返回 { ok, queue, note };
   * ok = 提交动作是否成功,queue = 是否要同时写入本地待办。
   *
   * 接线点(仓库建好后):
   *   - issue 模式:填 config.GITHUB_REPO 即可,无需其它改动
   *   - repository_dispatch 模式:部署代理(转发到
   *       POST https://api.github.com/repos/{repo}/dispatches
   *       {"event_type":"translate","client_payload":{"cf_url":url}}),
   *     然后把地址填进 config.DISPATCH_PROXY
   */
  async function triggerSubmit(url) {
    const mode = effectiveMode();

    if (mode === "issue") {
      const title = encodeURIComponent(`[translate] ${url}`);
      const body = encodeURIComponent(`整合包链接:\n${url}\n\n(由静态页提交表单生成)`);
      window.open(
        `https://github.com/${cfg.GITHUB_REPO}/issues/new?title=${title}&body=${body}`,
        "_blank",
        "noopener",
      );
      return { ok: true, queue: true, note: "已打开 GitHub 提交页,请在那里确认;同时记入本地待办。" };
    }

    if (mode === "repository_dispatch") {
      try {
        const resp = await fetch(cfg.DISPATCH_PROXY, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: cfg.GITHUB_REPO, cf_url: url }),
        });
        if (resp.ok) return { ok: true, queue: false, note: "已提交,稍后回列表查看。" };
        return { ok: false, queue: true, note: `代理返回 HTTP ${resp.status},已记入本地待办。` };
      } catch (err) {
        return { ok: false, queue: true, note: `提交失败(${err.message}),已记入本地待办。` };
      }
    }

    return { ok: true, queue: true, note: "已记录,稍后回列表查看。" };
  }

  function parseCfUrl(raw) {
    const m = String(raw).trim().match(CF_URL_RE);
    if (!m) return null;
    const slug = m[1];
    const fileId = m[2] || "";
    return {
      slug,
      fileId,
      url: `https://www.curseforge.com/minecraft/modpacks/${slug}` + (fileId ? `/files/${fileId}` : ""),
    };
  }

  async function onSubmit(event) {
    event.preventDefault();
    const parsed = parseCfUrl(input.value);
    if (!parsed) {
      setHint("只接受 CurseForge 整合包链接,例如 https://www.curseforge.com/minecraft/modpacks/包名", "err");
      return;
    }
    if (readQueue().some((it) => it.url === parsed.url)) {
      setHint("这个链接已经在本地待办里了。", "err");
      return;
    }

    setHint("提交中…");
    const result = await triggerSubmit(parsed.url);
    if (result.queue) queueAdd(parsed.url);
    setHint(result.note, result.ok ? "ok" : "err");
    renderQueue();
    input.value = "";
    input.focus();
  }

  // ── 启动 ──────────────────────────────────────────────────────────────────
  form.addEventListener("submit", onSubmit);
  renderQueue();

  loadIndex()
    .then(renderList)
    .catch((err) => {
      renderError(err);
      // index.json 读不到时仍允许用表单把链接记进本地待办
    });
})();
