---
name: agent-workbench-builder
description: Use when a coding AI is asked to build, adapt, or review a focused Agent or multi-capability workbench for a real work or life scenario on external DeepSeek Harness, including domain fit, approval safety, idempotency, and reproducible handoff.
---

# Agent 工作台构建器

## 目标与证据边界

帮助使用者做出属于自己的 Agent 产品，不复刻小蛇名称、界面、能力数量或业务流程。“同完成度”指工程闭环可比较：领域结果可观察、危险动作可拒绝、任务可重跑、失败可诊断、DSH 集成可验证、项目可交接。

始终区分四条证据轴：

- `machine`：测试、领域夹具、真实 DSH 运行和交接复验。
- `ai-assisted`：AI 按本 Skill 在新目录完成另一项目的隔离复现。
- `human`：未参与制作的人只读 README 完成盲测；没人执行时写 `NOT-RUN`。
- `external`：真实模型账号、设备、签名或分发；未验证时写 `PENDING-EXTERNAL`。

自动化和作者自测不能代签真人盲测。

## 先选择产品形态

把选择写进 `agent_project.json`：

- 默认推荐 `focused-agent`：一个主要场景、1 项能力、1 条主验收场景。AI 辅助编程新手先把一件事做深。
- 只有同一产品目的确实需要至少 2 项能力，并能写出至少 3 条覆盖全部能力的代表场景时，才选 `workbench`。

如果材料足以判断，说明依据并记录选择；若两种形态会明显改变产品且无法判断，再暂停脚手架让用户选择。完整蓝图见 [通用工作台蓝图](references/workbench-blueprint.md)。

## 开始前必须读取

1. [质量合同](references/quality-contract.md)：v4 生命周期、硬门和证据格式。
2. [构建工作流](references/build-workflow.md)：新建、升级、评审和不可信项目的分流。
3. DSH 项目读取 [DSH 产品工作流](references/dsh-product-workflow.md)。
4. 只有明确接受 standalone 降级时才读取 [standalone 回退](references/standalone-workflow.md)。

先冻结来源 HEAD、dirty/untracked、授权边界和验收命令。保留用户未提交改动；不要复制模板覆盖既有项目。发布、外部发送、付费调用、删除和覆盖仍需明确授权。

## 固定 DSH 兼容边界

DSH 是外部依赖，Builder 不下载、不复制、不打包 DSH。当前经 XS/Builder 实测的兼容边界是 DSH `0.1.0-rc.8`、pnpm `11.7.0`、Node `22.x（>=22.19.0）` 或 `>=24`。它不是 DSH 当前最新版本。

让使用者从官方仓库固定标签安装。首次依赖安装和构建可能需要数分钟并占用数 GB 空间；DSH 的依赖安装和构建命令必须在 DSH 根目录执行。Windows PowerShell：

```powershell
git clone --branch dsh-v0.1.0-rc.8 --depth 1 https://github.com/deepseek-ai/deepseek-harness.git C:\path\to\deepseek-harness
Set-Location 'C:\path\to\deepseek-harness'
node --version
corepack --version
corepack enable
corepack prepare pnpm@11.7.0 --activate
pnpm --version
pnpm install --frozen-lockfile
pnpm run build
$DshRoot = (Get-Location).Path
Set-Location 'C:\path\to\agent-workbench-builder'
python scripts/dsh_doctor.py --dsh-root "$DshRoot" --pretty
```

macOS / Linux：

```bash
git clone --branch dsh-v0.1.0-rc.8 --depth 1 https://github.com/deepseek-ai/deepseek-harness.git /path/to/deepseek-harness
cd /path/to/deepseek-harness
node --version
corepack --version
corepack enable
corepack prepare pnpm@11.7.0 --activate
pnpm --version
pnpm install --frozen-lockfile
pnpm run build
DSH_ROOT="$(pwd)"
cd /path/to/agent-workbench-builder
python3 scripts/dsh_doctor.py --dsh-root "$DSH_ROOT" --pretty
```

`pnpm --version` 必须显示 `11.7.0`。若 `corepack enable` 因权限失败，先修复 Node/Corepack 安装权限，不要用 `npx` 或随机全局 pnpm 绕过固定版本。只有 doctor 对官方 origin、固定 tag、固定 commit、干净工作树、全量受跟踪文件字节和 live config 全部给出 `PASS` 才继续；`--static`、dirty/fork/main 或任意 `PARTIAL` 都不能冒充已兼容。

## 新建 starter

### focused-agent

```text
python scripts/scaffold_project.py --product-kind focused-agent --destination "/path/to/project" --slug "request-triage-agent" --title "请求分诊 Agent" --scenario "把收件箱请求分诊为待办" --primary-user "项目负责人" --trigger "收到新的本地请求文件" --input-description "包含 request_id 与 content 的 JSON" --observable-output "经批准后生成任务 JSON" --dangerous-write "在输出目录创建任务文件"
```

### workbench

复制并改写 `assets/workbench-blueprint.example.json`，再运行：

```text
python scripts/scaffold_project.py --product-kind workbench --blueprint "/path/to/workbench-blueprint.json" --destination "/path/to/project"
```

脚手架结果只是 `starter`。即使模板测试、DSH 和交接命令都成功，毕业评估也必须返回 `PARTIAL`；若 fresh scaffold 得到项目 `PASS`，立即停止并报告评估器缺陷。

Windows 项目目录尽量靠近磁盘根目录，不要套很多层。打包若报 `PACKAGE_PATH_TOO_LONG`，选择更短的新项目路径并按回退说明迁移、复验；不要关闭安全检查或覆盖已有目录。

在 Builder 或新项目目录再运行 `pnpm --version`，也必须为 `11.7.0`。DSH 的 `packageManager` 可能让其目录里的版本正确，却掩盖当前 PATH 上另一个默认版本。用 `Get-Command pnpm`（Windows）或 `command -v pnpm` 找到命令来源；让当前终端 PATH 指向正确版本，再从同一终端重跑 doctor 与评估。评估器有意过滤额外环境变量，仅设置 `AGENT_WORKBENCH_PNPM` 不能替代正确 PATH；不要放宽环境白名单或贸然改全局配置。

## 从 starter 到 domain-adapted

不要只改名称或提示词。完成下面四项：

1. 改写合同声明的领域适配器，让不同能力返回本场景特有的结构化结果。
2. 把 `fixtures/domain-cases.json` 换成真实代表案例：每条场景、每项能力至少一个正向案例，且至少一个边界/拒绝案例。
3. 更新合同声明的领域测试，实际执行夹具并核对预期字段、错误码和恢复建议。
4. 全部行为和测试完成后，才把 `development.stage` 从 `starter` 改为 `domain-adapted`。

`builder-provenance.json#starterFileSha256` 保存生成基线。评估器要求所有 critical files 已变化、夹具覆盖完整、夹具实际通过、独立领域收据与 acceptance 一致。只翻转 stage、加注释或改一个哈希都不能毕业。

## 项目验收

先运行项目 README 的测试和 acceptance，再运行 Builder 评估器：

```text
python scripts/evaluate_project.py --project "/path/to/project" --dsh-root "/path/to/deepseek-harness" --output "/path/to/project/evidence/graduation.json" --pretty
```

退出码：`0=PASS`、`2=PARTIAL`、`3=FAIL/无效输入`。v4 有 7 个机器硬门：非小蛇品牌身份声明、领域适配、代表场景、审批拒绝、三次幂等与恢复、干净交接、主张追溯；20 分只是辅助量表，不能覆盖硬门失败。“不是换皮”还需要 AI 辅助或真人语义复核，机器门不冒充这项判断。

v4 的 `commands` 必须保持 Builder 发布版的固定 argv；`tools/acceptance.py`、打包器以及 DSH 运行适配工具按发布字节验签。业务核心、领域适配器、产品插件和自定义 UI 不在这组字节锁内，可以按场景修改。若确需修改受保护工具，应发布新的 Builder 版本并重建证据，不能在单个项目里改完仍沿用旧版本的毕业声明。这个门能阻止误改和直接伪造收据，但不是针对恶意项目代码的安全沙箱。

对不可信项目先人工审查，再用 `--no-run`；它最多得到 `PARTIAL`。评估器只执行合同声明的本地 Python argv，不使用 shell。

## 干净室复现

对所选路线运行：

```text
python scripts/verify_reproduction.py --product-kind focused-agent --runtime dsh --dsh-root "/path/to/deepseek-harness" --pretty
```

或把产品形态换成 `workbench`。报告必须同时证明：

- Unicode/空格路径中的 fresh starter 为 `PARTIAL`；
- 示例领域适配完成后为 `PASS`；
- 外部 DSH 完成 Profile dump、Bundle 加载、HTTP 200 与受控干净停止；
- 交接 ZIP 不含 DSH，解压后再次 `PASS`；
- 两次毕业摘要一致。

同一个外部 DSH checkout 上的 live 复验必须串行执行；不要同时启动 focused-agent 与 workbench 两条复现。每条复现仍使用隔离 Profile、随机 loopback 端口和临时目录，但 rc.8 的进程收尾并发会形成无意义的生命周期争用。上一条命令退出后再启动下一条。

Windows 下 Builder 会把 Product Bundle 精确暂存到 ASCII 无空格目录以绕开 DSH rc.8 的 shell 路径限制，记录源/暂存树摘要，并通过 stdin 哨兵进入 DSH 清理路径。它不会修改 DSH checkout。

## 既有项目

先盘点实际产品形态、运行底座、能力、状态、审批、测试和证据。把缺口映射到 v4 合同；逐项补齐，保留现有业务架构、自定义 UI 和未提交改动。要取得 v4 机器毕业，需引入该 Builder 版本的固定命令合同与受保护验收工具；不要为了套模板重建产品代码，也不要修改 DSH 内核承载产品逻辑。

## 必须保留的安全属性

- DSH `bundled=false`；项目、证据和 ZIP 均不得含 DSH checkout。
- 危险工具先调用 `tools/pre-execute` 的 `next()`，保留后续 `deny` 或 `ask`；没有更严格决定时再要求 `ask`，没有审批通道时关闭式拒绝。
- 工具的原生可读结果必须包含真实目录或业务预览，不只写“已准备”。超限时明确失败，不能截断草稿后仍让用户确认保存。
- 输入识别覆盖完整规范化内容，不能只对显示摘要计算哈希。相同编号的长文本尾部发生变化，也必须按内容冲突处理。
- 同一输入串行或并发重放都只产生一次业务副作用，结果哈希一致；每个幂等键使用跨进程原子占位/锁，冲突拒绝覆盖。
- Windows 短暂锁占用只在检查路径后有界重试，实际取得独占锁才继续。持续 `LOCK_UNAVAILABLE` 要检查并发进程、目录权限与崩溃现场，不删除账本或绕过锁；写入和关闭文件错误不能伪装成锁竞争。
- 错误使用稳定错误码和恢复提示，失败收据只保留脱敏输出尾部。
- ZIP 只含安全相对路径、逐文件哈希、归档哈希、外部依赖和回退说明；拒绝盘符、设备名、链接、规范化碰撞、超量成员和展开量。
- 不写入密钥、令牌、机器绝对路径、`.runtime`、`node_modules`、`work`、`dist` 或缓存。
- 已知模式扫描不是秘密不存在的证明；发布前还须人工复核业务资料、日志和归档成员。
- 离线 provider 不证明真实模型可用；真实账号保持独立验收。

从旧 Builder 升级既有项目时保留业务代码与运行数据，按新版本更新受保护工具及基线后重新验证。旧账本中的结果可能因完整输入哈希变化而报告冲突；检查旧结果并决定是否创建新任务，不能清空账本或覆盖原文件来取得通过。

## 返回格式

先给 `PASS`、`PARTIAL` 或 `FAIL`，再依次列：产品形态、生命周期阶段、能力/场景覆盖、7 个硬门、20 分、DSH 外部边界、真实运行证据、交接哈希、`HUMAN-BLIND-TEST`、`PENDING-EXTERNAL` 项。不要用测试数量、页面打开、配置 dump 或脚手架生成代替业务结果。
