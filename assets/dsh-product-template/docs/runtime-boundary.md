# 运行时与产品边界

本项目以外部 DeepSeek Harness 为 Agent 内核。项目只交付 Product Bundle、产品目标、能力模块、代表性场景、配置和证据；不复制、不下载、不打包 DSH 源码或依赖目录。

- 官方仓库：https://github.com/deepseek-ai/deepseek-harness
- 经本项目验证的 DSH 版本：`0.1.0-rc.8`
- DSH 负责：Agent 循环、持久会话、模型路由、工具流水线、审批、权限沙箱与 Web 宿主。
- 本项目负责：产品身份、统一目标、能力边界、场景适配、危险动作声明、幂等业务输出与验收。
- `focused-agent` 与 `workbench` 是用户选择的产品范围，不是 DSH 的两份运行时。

DSH 仍处于开发者预览，升级前必须先运行 doctor、配置 dump、启动/停止和本项目验收，不能只改版本号。
