# __PROJECT_TITLE_TEXT__ 产品约束

- 使用者已选择产品类型：`__PROJECT_PRODUCT_KIND_TEXT__`；不得在未确认时改成另一种。
- 统一产品目标：__PROJECT_PURPOSE_TEXT__。
- 主要使用者：__PROJECT_PRIMARY_USER_TEXT__ 等。
- 能力模块与代表性场景以 `agent_project.json` 为事实源；新增能力必须同时补场景覆盖和测试。
- 当前阶段以 `agent_project.json#development.stage` 为准；脚手架刚生成时为 `starter`。先改领域适配器、`fixtures/domain-cases.json` 和领域测试，全部通过后才把 stage 改为 `domain-adapted`；不得把模板测试通过写成项目毕业。交付前核对 README、产品约束和验收说明，不能留下互相矛盾的当前状态。
- 输入是用户任务及获准访问的现场；结果必须是可观察工作成果，不是配置、toast 或页面打开。
- 危险写动作：__PROJECT_DANGEROUS_WRITE_TEXT__ 等，必须经过 DSH 一次性审批。
- 先调用能力 plan 工具；仅在用户批准后调用对应 commit 工具。
- plan 的可读结果包含实际草稿，不能只有“已准备”；审批保留更严格的拒绝。输入识别使用完整内容，相同编号的文本尾部改变也不能当作重复任务。
- 最终返回实际输出相对路径、结果哈希、验证证据和仍未覆盖的能力边界。
