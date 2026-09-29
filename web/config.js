/**
 * 前端可配置项 —— 全部是占位值,接线时只改本文件,不用动 main.js。
 *
 * 注意:纯静态页无法安全持有 GitHub Token,"repository_dispatch" 模式
 * 必须经由自建代理(Cloudflare Worker / Serverless)转发,见 DISPATCH_PROXY。
 */
window.MC_WEB_CONFIG = {
  /** "owner/repo" —— 仓库建好后填入(例如 "yourname/mc-web")。留空 = 提交只记本地待办 */
  GITHUB_REPO: "",

  /** raw 链接使用的分支 */
  BRANCH: "main",

  /**
   * index.json 地址(相对本页)。
   * GitHub Pages 以仓库根为站点根、页面在 /web/ 时,保持 "../index.json";
   * 若把 web/ 内容整体发布,改成 "index.json" 或填绝对 URL。
   */
  INDEX_URL: "../index.json",

  /**
   * 可选:自定义 raw 文件基址(如 jsDelivr CDN)。
   * 留空时:填了 GITHUB_REPO 用 raw.githubusercontent.com,否则用站点相对路径。
   */
  RAW_BASE: "",

  /**
   * 提交触发方式(三选一):
   *   "queue"                默认。只写入本页的"本地待办",不动网络(仓库未配置时的行为)
   *   "issue"                打开预填好的 GitHub Issue 页面(纯静态可用,无需 Token)
   *   "repository_dispatch"  走 DISPATCH_PROXY 转发到 GitHub API(需自建代理,见 main.js)
   */
  DISPATCH_MODE: "queue",

  /**
   * 自建代理地址(DISPATCH_MODE = "repository_dispatch" 时必填)。
   * 约定:前端 POST { repo, cf_url },由代理携带 Token 调
   *       POST https://api.github.com/repos/{repo}/dispatches
   *       body: {"event_type": "translate", "client_payload": {"cf_url": "..."}}
   */
  DISPATCH_PROXY: "",
};
