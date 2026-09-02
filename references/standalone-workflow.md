# standalone 回退工作流

standalone 是明确降级路径，不是默认底座。当前只支持用户明确选择的 `focused-agent`；若用户选择 `workbench`，必须使用外部 DSH 或停止并说明能力缺口。

只有用户选择，或当前环境无法提供外部 DSH 且用户接受缺少 DSH 会话、模型路由、工具流水线、审批与 Web 宿主时使用：

```text
python scripts/scaffold_project.py --product-kind focused-agent <focused-agent arguments> --runtime standalone
```

回退模板仍要求 starter `PARTIAL`、真实领域夹具、默认拒绝、三次重跑一次副作用、稳定错误码、loopback 可观察入口、确定性 ZIP 和哈希证据。只有 `domain-adapted` 后才可通过自身工程门；它仍只能证明 Python 工作流，不得声称具备 DSH 的 Agent 生命周期或产品扩展能力。

当项目以后迁移到 DSH：保留领域适配器与幂等业务状态，把 Agent 循环、会话、模型、工具执行、审批和 Web 交给外部 DSH；不要把 standalone 内核嵌进 Product Bundle 形成两个事实源。要升级为 workbench 时，先建立能力清单和代表场景蓝图，再切换到 DSH 多能力合同。
