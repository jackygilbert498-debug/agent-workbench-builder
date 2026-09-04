# 构建工作流

## 1. 先选产品形态

- **`focused-agent`**：一项能力、一条主要场景。它是 AI 辅助编程新手的默认路线，先把一件事做到产品级。
- **`workbench`**：同一产品目的下的多项能力、至少三条代表场景。适合处理多类相关输入并产生不同类型结果。

不要按界面大小判断。只有同一产品目的确实需要多项独立能力、不同结果或风险边界，并能写出三条代表场景时才选 `workbench`。选择必须明确记录；通用工作台不是“什么都做”，详见 [通用工作台蓝图](workbench-blueprint.md)。

## 2. 路由

- **DSH 全新产品（默认）**：先选择产品形态，再 doctor、脚手架、领域定制和真实 DSH 验收。
- **DSH 既有产品**：保留现有结构，识别实际产品形态，将职责映射到 Profile、Bundle、产品插件、能力适配器、领域状态和证据，不复制模板覆盖。
- **standalone 回退**：仅支持用户明确选择的 `focused-agent`；读取 [standalone 回退工作流](standalone-workflow.md) 并传 `--runtime standalone`。
- **不可信项目**：人工审查后只运行 `evaluate_project.py --no-run`，不要执行合同声明的命令。
- **只做评审**：只读扫描和报告，不实施修改，除非用户明确要求修复。

## 3. 冻结基线

记录产品形态、当前提交或文件清单、已有测试、已知失败、允许写入目录、外部 DSH 版本和禁止动作。工作树非干净时保留用户改动。不要让 Builder 自动提交、发布、发送、付费调用、删除数据或下载 DSH。

## 4. 职责映射

| 职责 | DSH 默认归属 | 产品归属 |
|---|---|---|
| Agent 循环、会话、模型路由 | 外部 DSH | 只声明兼容边界 |
| 工具注册与执行流水线 | DSH `tools` | Product Bundle 中的能力工具 |
| 审批与沙箱 | DSH approval/permission | 危险能力返回 `ask`，收紧写入范围 |
| 产品身份、能力与场景规则 | 不属于 DSH 内核 | `AGENTS.md`、Bundle patch、能力适配器 |
| 幂等业务状态 | 不属于通用会话日志 | 产品工作流与业务账本 |
| Web 宿主 | DSH web Profile | Product Bundle 或独立产品 UI 层 |
| 验收与交接 | DSH 只提供运行事实 | 代表场景收据、哈希、ZIP、回退说明 |

每项职责只给一个事实源。不要在验收脚本中重写另一份业务逻辑来制造通过。

## 5. 建造顺序

1. 用户明确产品形态；`workbench` 先完成并校验蓝图。
2. doctor 通过后生成 Product Bundle；目标目录必须不存在。
3. 先运行一次评估，确认 fresh starter 为 `PARTIAL`；逐项替换领域适配器、夹具和领域测试，最后才把 stage 改为 `domain-adapted`。
4. 为危险 commit 工具接入 DSH `tools/pre-execute`：先执行 next 并保留后续 deny/ask，再补产品审批；真实运行拒绝路径。原生 plan 结果须展示真实草稿，不只返回状态。
5. 加入原子写、幂等键、完整输入识别、既有产物校验、稳定错误码和恢复提示；同编号只改长文本尾部也要检查冲突。
6. 用隔离 `DSH_HOME` 安装本地 Bundle，运行最终 Profile dump。
7. 启动 DSH Web，验证 loopback HTTP 和 stdin 哨兵触发的干净停止；fallback terminate/kill 不能记为 clean。
8. 生成验收收据、确定性交接 ZIP、manifest、逐文件/归档 SHA-256 和回退说明。
9. 从 ZIP 解压后用同一外部 DSH 再验一次；两次毕业摘要必须一致。

## 6. 既有项目适配

可以调整架构文件映射，但 v4 毕业项目的 `commands` 必须等于 Builder 固定 argv，并保留发布版受保护的 acceptance、打包和 DSH 运行工具。业务核心、领域适配器、Bundle/插件与自定义 UI 可以继续演进；若已有 UI，保留它并证明其真实入口。若只需 DSH 通用 Web，不为了评分另建空壳页面。产品功能放在自己的 Bundle/插件中，不 fork 或修改 DSH 内核。确需修改受保护工具时应发布新的 Builder 版本并重建证据，不能复用旧版签名。

外部模型、MCP 或第三方账号不可用时，可以用明确标注的离线 reference provider 验本地工程链；毕业报告必须把真实 provider 就绪度列为限制。配置 dump、Web 200 与工具单测的组合能证明本地集成，不能证明模型已正确使用工具。

## 7. 停止条件

出现以下情况停止相应动作并说明：

- 用户尚未选择产品形态，或 workbench 蓝图未满足能力与代表场景覆盖；
- fresh scaffold 得到项目毕业 `PASS`，或只翻转 stage/改哈希就能通过领域门；
- doctor 版本、许可证、Node、pnpm 或关键配置能力不匹配；
- 需要下载/复制 DSH 到 Builder 或项目才能继续；
- 需要新的付费提交、对外发送、生产发布或权限扩张；
- 目标目录已存在，脚手架会覆盖文件；
- 项目合同声明任意 shell、越界路径或不可信可执行文件；
- 证据含秘密、绝对路径、DSH checkout 或与源码不一致的哈希；
- ZIP 含 `.runtime`、`node_modules`、DSH、符号链接、路径穿越、加密成员或 manifest 不匹配。

失败时保留稳定错误码与最小恢复动作，不把阻塞项改写成通过。
