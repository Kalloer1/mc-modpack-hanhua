/*
 * MC 整合包汉化共享库 —— 前端逻辑(纯静态,无框架、无 Token)。
 *
 * 数据源:index.json,结构由 src/index_store.py 定稿:
 *   { version, updatedAt, entries: [ { source, projectId, fileId, name,
 *     mcVersion, status, path, translatedAt, stats } ] }
 *   status ∈ pending | done | failed | no_ftbq | already_localized
 *   成品文件 = {path}/zh_cn.snbt
 *   生产环境读仓库根的 raw index.json;本地(localhost/file://)优先本地文件,
 *   读不到退到 sample/index.json;?demo=1 强制示例数据。
 *
 * 提交:打开预填好的 GitHub Issue 页(.github/workflows/on-issue.yml 在服务端处理),
 * 浏览器不需要任何 Token。可配置项都在 config.js。
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

  const HISTORY_KEY = "mc_web.pending_submissions.v1";
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
  const historyWrap = $("history-wrap");
  const historyList = $("history-list");

  const params = new URLSearchParams(location.search);
  const forceDemo = params.get("demo") === "1";
  let usedSample = false;

  // ── index.json 加载 ────────────────────────────────────────────────────────
  function isLocalPage() {
    return (
      location.protocol === "file:" ||
      ["localhost", "127.0.0.1", "::1", ""].includes(location.hostname)
    );
  }

  function rawBase() {
    if (cfg.RAW_BASE) return String(cfg.RAW_BASE).replace(/\/+$/, "");
    if (cfg.GITHUB_REPO) {
      return `https://raw.githubusercontent.com/${cfg.GITHUB_REPO}/${cfg.BRANCH || "main"}`;
    }
    return "";
  }

  const candidates = (forceDemo
    ? ["sample/index.json"]
    : isLocalPage()
      ? ["../index.json", "index.json", "sample/index.json"]
      : [`${rawBase()}/index.json`, "../index.json", "index.json", "sample/index.json"]
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

  /**
   * done 条目 → 下载信息 {url, filename}。
   * 优先 patch.zip(整包补丁,lang/硬编码统一,解压到整合包根即覆盖);
   * 老条目无 archive 时回退到单文件 zh_cn.snbt。示例数据/未配置仓库返回 null。
   */
  function resolveDownload(entry) {
    if (usedSample) return null;
    const path = String(entry.path || "").replace(/^\/+|\/+$/g, "");
    const base = rawBase();
    if (!path || !base) return null;
    const archive = entry.stats && entry.stats.archive ? String(entry.stats.archive) : "";
    if (archive) {
      return { url: `${base}/${path}/${archive}`, filename: archive };
    }
    return { url: `${base}/${path}/${SNBT_FILE}`, filename: SNBT_FILE };
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
      const dl = resolveDownload(entry);
      if (dl) {
        const a = document.createElement("a");
        a.className = "btn btn-download";
        a.href = dl.url;
        a.setAttribute("download", dl.filename);
        a.textContent = `下载 ${dl.filename}`;
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
        "当前展示的是示例数据(web/sample/index.json):未读到真实 index.json,或页面被 ?demo=1 强制指定。";
    }
  }

  function renderError(err) {
    errorState.hidden = false;
    errorState.textContent =
      `读取 index.json 失败:${err && err.message ? err.message : err}。` +
      "若直接双击打开本页,浏览器会禁止 fetch 本地文件;" +
      "请在仓库根目录运行 python -m http.server 8000 后访问 http://localhost:8000/web/。";
  }

  // ── 本机提交记录(localStorage,只做回查,不参与提交)──────────────────────
  function readHistory() {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      const list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list : [];
    } catch {
      return [];
    }
  }

  function writeHistory(list) {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
    } catch {
      /* 隐私模式等场景写不进去,忽略 */
    }
  }

  function historyAdd(url) {
    const list = readHistory();
    if (list.some((it) => it.url === url)) return false;
    list.unshift({ url, at: new Date().toISOString() });
    writeHistory(list);
    return true;
  }

  function historyRemove(url) {
    writeHistory(readHistory().filter((it) => it.url !== url));
    renderHistory();
  }

  function renderHistory() {
    const list = readHistory();
    historyWrap.hidden = list.length === 0;
    historyList.textContent = "";
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
      del.addEventListener("click", () => historyRemove(item.url));
      li.append(a, time, del);
      historyList.appendChild(li);
    }
  }

  // ── 提交:打开预填好的 GitHub Issue 页 ────────────────────────────────────
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

  function buildIssueUrl(cfUrl) {
    const title = `[translate] ${cfUrl}`;
    const body = [
      "整合包链接:",
      cfUrl,
      "",
      "---",
      "由静态页提交表单生成,CI 会在本 issue 下回复翻译结果(成功给下载路径,失败给原因)。",
      "链接有误可直接编辑本 issue 标题/正文后重新加 submit 标签重试。",
    ].join("\n");
    const qs = new URLSearchParams({
      title,
      labels: cfg.SUBMIT_LABEL || "submit",
      body,
    });
    return `https://github.com/${cfg.GITHUB_REPO}/issues/new?${qs.toString()}`;
  }

  function onSubmit(event) {
    event.preventDefault();
    if (!cfg.GITHUB_REPO) {
      setHint("前端未配置仓库地址(web/config.js 的 GITHUB_REPO),暂时无法提交。", "err");
      return;
    }
    const parsed = parseCfUrl(input.value);
    if (!parsed) {
      setHint("只接受 CurseForge 整合包链接,例如 https://www.curseforge.com/minecraft/modpacks/包名", "err");
      return;
    }
    if (readHistory().some((it) => it.url === parsed.url)) {
      setHint("这个链接已经提交过了,可在下方记录里回查;重复提交请直接去 issue 页面。", "err");
      return;
    }

    // 同步 window.open,避免被浏览器当弹窗拦截
    window.open(buildIssueUrl(parsed.url), "_blank", "noopener");
    historyAdd(parsed.url);
    renderHistory();
    setHint("已打开 GitHub 提交页:点『Submit new issue』即排队;CI 处理后会回到该 issue 回复结果。", "ok");
    input.value = "";
    input.focus();
  }

  // ── 启动 ──────────────────────────────────────────────────────────────────
  form.addEventListener("submit", onSubmit);
  renderHistory();

  loadIndex()
    .then(renderList)
    .catch(renderError);
})();
