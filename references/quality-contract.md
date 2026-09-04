# 可比工程完成度质量合同 v4

## 定义

“同完成度”比较工程闭环，不比较功能数量、代码行数或视觉相似度。项目必须用自己的身份和真实工作流完成所选产品形态；不得复制小蛇身份、界面或业务来冒充迁移能力。机器报告只能检查声明、行为与证据的一致性；是否仍是语义换皮，需要 AI 辅助或真人复核。

- `focused-agent`：恰好 1 项能力、1 条主要验收场景。
- `workbench`：同一产品目的下至少 2 项能力、3 条代表场景；全部能力有覆盖，恰好 1 条为主要场景。

DSH 是默认外部底座。产品只交付自己的 Bundle、领域逻辑、测试和证据；DSH 不进入项目或交接包。

## 生命周期

1. `discovery`：冻结使用者、触发、输入、结果、危险动作和非目标。
2. `starter`：脚手架可运行，但项目毕业固定为 `PARTIAL`。
3. `domain-adapted`：关键适配器、真实夹具和领域测试已脱离生成基线。
4. `engineering-closed`：自动硬门、真实 DSH 和交接复验通过。
5. `human-graduated`：独立使用者盲测通过；未执行为 `NOT-RUN`。
6. `external-ready`：真实账号/设备/分发逐项通过；未完成为 `PENDING-EXTERNAL`。

## 七个硬门

1. **非小蛇品牌身份声明**：合同中的 slug、标题、原创声明和场景标题不使用 `xiaoshe` 或“小蛇”身份。此机器门不证明语义原创，也不代替真人判断流程、界面和价值是否只是换皮。
2. **领域适配**：`development.stage=domain-adapted`；全部 critical files 与 starter 哈希不同；每条场景、每项能力有正向夹具，至少一个边界夹具；夹具实际运行并生成与 acceptance 一致的领域收据。
3. **代表场景覆盖**：focused 主场景或 workbench 全部能力/场景走到业务结果；DSH Profile、Bundle、Web HTTP 和受控停止真实通过。
4. **危险动作可拒绝**：批准与拒绝都真实运行，拒绝无业务副作用；DSH 危险工具走 `tools/pre-execute`，保留后续拒绝和审批要求，不得用提前返回的 ask 跳过它们。
5. **可靠可重跑**：同一输入三次结果哈希一致、业务副作用恰好一次；同一编号的完整输入改变，包括长文本尾部改变，必须报告冲突且不覆盖旧产物；失败有稳定错误码和恢复提示。
6. **交付干净**：源码与 ZIP 未命中已知密钥、令牌、个人目录或机器绝对路径模式；ZIP 无 DSH、`.runtime`、`node_modules`、生成状态、符号链接或危险成员。模式扫描不能证明所有秘密都不存在，发布前仍须人工复核。
7. **主张可追溯**：交付主张定位到夹具、测试、运行收据、逐文件哈希、归档哈希或 DSH 运行字段。

硬门不能由总分抵消。starter 即使其他六门通过，也因领域门为 `partial` 而不得毕业。

## 20 分维度

| 维度 | 4 分证据 |
|---|---|
| 声明与场景证据一致 | 使用者、目的、能力、代表场景、领域结果和危险动作在当前合同与夹具中一致 |
| 架构边界 | 外部 DSH、Bundle、能力适配器、界面、业务状态与事实源归属清楚 |
| 安全可控 | 审批门、明确拒绝、最小写入范围和审计收据通过 |
| 可靠可重跑 | 三次重跑、一次副作用、稳定哈希、冲突拒绝和结构化错误通过 |
| 可交接 | 测试、验收、ZIP、manifest、哈希、外部依赖与解压复验齐全 |

最低总分 16/20，但仍须通过全部七门。

## 项目合同 v4

项目根的 `agent_project.json` 使用 `agent-workbench-project/v4`：

- `project`：slug、title、kind、originalityStatement。
- `product`：purpose、primaryUsers。
- `capabilities` 与 `acceptanceScenarios`：能力责任、风险和代表覆盖。
- `runtime`：`external-dsh` 或明确降级的 `standalone`；外部 DSH 记录官方仓库、实测版本和 `bundled=false`。
- `development.stage`：`starter` 或 `domain-adapted`。
- `development.domainEvidence`：fixtures、report、test 的安全相对路径。
- `development.criticalFiles`：必须脱离 starter 哈希的关键文件。
- `architecture`、`risk`、`commands`、`evidence`、`requiredFiles`、`rollback`：架构归属、审批合同、受控 argv、证据和恢复。

所有合同路径相对项目根且不能穿越。v4 命令必须使用 Builder 发布版固定 argv；评估器不用 shell，只运行本地 Python 入口或 `python -m unittest`。受保护的验收、打包和 DSH 运行工具必须与 Builder 发布字节一致，业务核心、领域适配器、插件和 UI 不受该字节锁限制。DSH checkout 路径只从命令传入，不写进合同或证据。

## starter 基线

`builder-provenance.json` 使用 `agent-workbench-builder-provenance/v3`，至少含：

- Builder 版本、模板、产品形态、蓝图摘要；
- `starterStage=starter`；
- `starterFileSha256`，键集合与 `development.criticalFiles` 完全一致。

修改哈希只是必要条件。评估器还会验证夹具结构、覆盖、实际结果和领域收据，防止只加空格或翻转 stage。

## 证据 v4

`agent-workbench-domain-adaptation/v1` 证明 stage、夹具哈希、正向/边界数量、场景/能力覆盖和逐案例结果。starter 可以有 `fixturesPassed=true`，但只有 stage 为 `domain-adapted` 时 `status=PASS`。

`agent-workbench-acceptance/v4` 证明：

- 产品、能力、场景合同一致；
- 领域夹具实际执行；
- 审批、拒绝、三次幂等、恢复和本地界面通过；
- 外部 DSH 的版本、能力配置、Profile、Bundle、HTTP 与受控停止通过；
- required files 的 SHA-256 与当前文件一致。

`agent-workbench-handoff/v4` 与 manifest v4 证明：

- 产品形态、stage、能力数和场景数匹配合同；
- ZIP/sidecar、逐成员大小和 SHA-256 匹配；
- 无路径穿越、符号链接、加密或越界成员；
- DSH 记录官方链接、实测版本和 `bundled=false`，且不进入 ZIP。

`agent-workbench-graduation/v4` 汇总命令、静态扫描、领域门、代表覆盖、七个硬门、20 分、外部 DSH 和限制。报告不保存工作目录、临时目录、环境变量或秘密。

`agent-workbench-reproduction/v4` 必须同时记录 starter `PARTIAL`、领域项目 `PASS`、交接解压 `PASS` 与稳定摘要。

## 自动化边界

自动化能证明当前仓库按合同覆盖已声明场景，并能发现受保护工具被误改或用直接伪造脚本替换；它不是运行恶意项目代码的安全沙箱，也不能证明业务代码没有针对测试取巧。它还不能证明语义原创、未来所有请求、长期真实需求、任何陌生人都无需帮助、真实模型一定选对工具、外部账号已就绪或未来 DSH 版本兼容。语义复核、真人盲测、真实账号、设备和发布签名单独取证。
