# MC 整合包 FTB Quests 汉化众包共享库 — 任务看板

> 总管(Orchestrator)= Claude(本窗口)。实现类任务派发给 `qodercli` 协作 agent。
> 完成一项就把 `- [ ]` 改成 `- [x]`。

## 角色分工

| 角色代号 | 职责 | 执行者 |
|---|---|---|
| **R0 总管** | 拆解任务、派发、集成、评审、更新本看板 | Claude(w3:pK) |
| **R1 下载检测** | CF API 解析链接、modpacks.ch/CDN 抽取、Range 检测 FTBQ | qodercli |
| **R2 SNBT 引擎** | SNBT 真解析、占位符 mask/unmask、译后校验 | qodercli |
| **R3 翻译** | Gemini 接入、CFPA 术语注入、批处理与重试 | qodercli |
| **R4 平台/CI** | 仓库结构、index.json、GitHub Actions worker | qodercli |
| **R5 前端** | GitHub Pages 静态页、提交表单、列表与下载 | qodercli |

---

## Milestone M1 — 本地打通链路(最高优先级,验证核心风险)

- [x] **[R1]** 写 `src/fetch.py`:输入 CF 整合包链接 → 解析出 projectID/fileID ✅ qoder2(9 种链接形态)
- [x] **[R1]** 实现 Range 请求读前 512KB manifest,检测是否含 FTB Quests(project id `289412`/`438496`)✅ qoder2(StoneBlock4 真机验证,只读 524288 字节命中)
- [x] **[R1]** 实现 `downloadUrl` 为 null 时重建 `edge.forgecdn.net` 直链 / 走 modpacks.ch,抽取 quests SNBT ✅ qoder2(离线51+联网19 全通过)
- [x] **[R2]** 写 `src/snbt.py`:用 `ftb-snbt-lib` 解析 en_us.snbt,遍历 title/subtitle/description/text ✅ qoder1
- [x] **[R2]** 实现占位符 mask/unmask(抄 `reference_repos/MineAI-Modpack-Translator` 的 Titanium Shield) ✅ qoder1
- [x] **[R2]** 实现译后校验器(括号/引号/颜色码序列变化则回退原文,抄 ftb-quests-translator `validate_translation`) ✅ qoder1
  - ⚠️ 备注(R4 注意):项目 Python 由 uv 托管,已建 `.venv`,依赖 `ftb-snbt-lib==0.4.1`,CI 需安装。
- [x] **[R3]** 写 `src/translate.py`:Gemini 免费档接入,50 行/批 + 429/503 指数退避 ✅ qoder1(含 mock 降级)
- [x] **[R3]** 跳过已中文行、item ID、图片、`{@pagebreak}` 等 ✅ qoder1(snbt.py 内处理)
  - ⚠️ 备注:配错 key/未装 SDK 会抛 `TranslateConfigError`(不再静默产出"没翻的成品");无 key 自动走 mock。`requirements.txt` 用 `google-genai`(非旧版 `google-generativeai`)。
- [x] **[R0]** 集成为 `main.py`:链接 → zh_cn.snbt,拿一个真实冷门包端到端跑通 ✅ qoder1(The CUBE 硬编码 55 文件 + FTB Evolution lang 双模式端到端通)

## Milestone M2 — GitHub Actions worker

- [x] **[R4]** 设计仓库结构 `packs/cf/{projectID}/{fileID}/` + `meta.json` ✅ qoder3
- [x] **[R4]** 写 `index.json` 读写模块(状态:pending/done/failed/no_ftbq/already_localized)✅ qoder3(`src/index_store.py`,30/30 自测)
- [x] **[R4]** 写 `.github/workflows/translate.yml`,`workflow_dispatch` 手动触发跑 M1 脚本 ✅ qoder3(main.py 调用暂占位)
- [x] **[R4]** 产物 commit 回仓库 + 更新 index.json;密钥用 GitHub Secrets ✅ qoder3(env 注入防注入 + concurrency 防并发)
- [ ] **[R0]** 手动触发验证一次完整 CI 产出 ⏳ **需用户在 Actions 页点 Run workflow**(本机 api.github.com HTTPS 超时,无法脚本触发)

## Milestone M3 — 静态前端

- [x] **[R5]** 搭 GitHub Pages 静态站,读取 index.json 渲染整合包列表 ✅ qoder3(headless Edge 三态验证)
- [x] **[R5]** 提交表单(只收 CF 链接)→ 触发 Action(预填 GitHub Issue,无浏览器 token)✅ qoder3(on-issue.yml 24/24)
- [x] **[R5]** 命中已翻译直接给 raw 下载链接;未命中显示 pending ✅ qoder3(公开仓库 raw 200)
- [ ] **[R5]** 加简单去重 + 提交频率限制(防刷爆额度)

## Milestone M4 — 质量强化

- [ ] **[R3]** 接入 CFPA 术语表:先查词典命中直接用,未命中才喂 Gemini
- [ ] **[R2]** 完善已汉化探测(包内已带 zh_cn 标 already_localized)
- [x] **[R1]** 处理 FTBQ 新旧格式差异(独立 lang 文件 vs 硬编码 chapters)✅ qoder2(`extract_quest_sources` 返回 mode lang/hardcoded/none)
- [ ] **[R3]** 断点续翻(避免大包超时白跑)

## Milestone M5 — 社区与扩展(二期)

- [ ] **[R0]** 文档:说明玩家可对 zh_cn.snbt 直接提 PR 纠错
- [ ] **[R4]** 增加 Modrinth 来源支持

---

## 前置事项(阻塞项)

- [ ] **[R0/用户]** CurseForge API Key 已泄露一次 → **吊销并重新生成**,放入 `.env` / GitHub Secrets
- [ ] **[R0]** 上线前人工核对一次 CurseForge API 开发者条款(自动化取用合规性)
