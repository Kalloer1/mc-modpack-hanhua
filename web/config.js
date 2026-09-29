/**
 * 前端可配置项 —— 只改本文件,不用动 main.js。
 * 安全约定:这里不放任何 Token;提交通道 = 打开预填好的 GitHub Issue 页,
 * 真正的翻译由 .github/workflows/on-issue.yml 在服务端跑。
 */
window.MC_WEB_CONFIG = {
  /** 仓库 "owner/repo"(提交 issue、拼 raw 下载链接都靠它) */
  GITHUB_REPO: "Kalloer1/mc-modpack-hanhua",

  /** 默认分支(raw 链接用) */
  BRANCH: "master",

  /** 提交 issue 时自动带上的标签 */
  SUBMIT_LABEL: "submit",

  /**
   * index.json 地址。
   * 留空 = 自动用 raw 根 index.json(生产环境);
   * 本地(localhost / file://)会自动改读本地 ../index.json,读不到再退到 sample/index.json。
   */
  INDEX_URL: "",

  /** 可选:自定义 raw 文件基址(如 jsDelivr CDN);留空 = raw.githubusercontent.com */
  RAW_BASE: "",
};
