# 通用工作台蓝图

## 什么时候选择 workbench

选择 `workbench`，应同时满足：

- 多项任务服务同一个产品目的与主要用户群；
- 至少两项能力有不同的输入、结果、领域规则或风险边界；
- 使用者需要从同一个产品入口调用这些相关能力。

若当前只确定一件高价值工作，先选 `focused-agent`。不要为了看起来“通用”堆空能力。

## 输入与结果如何描述

工作台没有一个模糊的万能输入和万能结果。蓝图先声明产品级入口，再逐能力声明：

| 层级 | 输入 | 结果 |
|---|---|---|
| 产品入口 | 用户意图、所选能力或可判定的任务类型 | 路由到明确能力，或以稳定错误说明不支持 |
| 能力 | 结构化字段、文件、素材或领域对象 | 可观察的计划、报告、文件、状态变更或外部动作收据 |
| 代表场景 | 固定可重放样例 | 可哈希、可检查、可拒绝且可复现的具体结果 |

每项能力必须有明确边界。未匹配任何能力的请求应返回稳定“不支持”结果，不得假装成功。

## 蓝图最低合同

`agent-workbench-blueprint/v1` 包含：

```json
{
  "schema": "agent-workbench-blueprint/v1",
  "productKind": "workbench",
  "project": {
    "slug": "local-operations-workbench",
    "title": "本地事务工作台",
    "purpose": "把本地请求、收件箱和任务状态转成可执行信息",
    "primaryUsers": ["项目负责人"]
  },
  "capabilities": [
    {
      "id": "request-triage",
      "title": "请求分诊",
      "responsibility": "将请求整理为待办",
      "risk": "approval-required"
    },
    {
      "id": "task-audit",
      "title": "任务核对",
      "responsibility": "只读核对任务状态与证据",
      "risk": "read-only"
    }
  ],
  "scenarios": [
    {
      "id": "triage-request",
      "title": "把新请求分诊为待办",
      "trigger": "收到新的本地请求文件",
      "input": "固定请求样例",
      "observableOutput": "任务 JSON 与审计收据",
      "primary": true,
      "capabilityIds": ["request-triage"]
    },
    {
      "id": "audit-task",
      "title": "核对一项任务",
      "trigger": "需要确认任务是否完成",
      "input": "任务标识和完成凭证",
      "observableOutput": "只读状态与证据缺口",
      "primary": false,
      "capabilityIds": ["task-audit"]
    },
    {
      "id": "review-open-tasks",
      "title": "查看未完成任务",
      "trigger": "准备安排当天工作",
      "input": "当前任务集合",
      "observableOutput": "只读未完成任务摘要",
      "primary": false,
      "capabilityIds": ["task-audit"]
    }
  ],
  "dangerousWrites": ["在输出目录创建任务文件"]
}
```

实际 workbench 必须有 2–12 项能力、3–20 条代表场景、恰好一条 `primary=true`，并至少包含一项 `approval-required` 能力。`risk` 只允许 `read-only` 或 `approval-required`。每项能力至少被一条代表场景的 `capabilityIds` 覆盖。

可直接复制 `assets/workbench-blueprint.example.json` 作为结构起点，但必须替换其中的产品事实和参考规则。

## 验收边界

自动化必须跑完每条代表场景，并证明：能力覆盖完整、只读能力不写入、危险能力可批准也可拒绝、同一危险输入三次只有一次副作用、冲突拒绝覆盖、结果和证据可哈希。

这证明“声明的多能力产品闭环已完成”，不证明未来任意任务自动可用。新增能力时必须同时增加合同、适配器、工具 schema、代表场景、测试和证据。
