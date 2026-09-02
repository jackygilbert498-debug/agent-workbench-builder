# Agent Workbench Builder

一套帮助你与编程 AI 合作，把真实工作或生活需求做成可验收 Agent 产品的 Skill。

这个仓库只放 Builder：说明、脚手架、模板和测试。不包含小蛇项目、DSH 源码、项目历史 Skill 或课程。目标是做出自己的场景，而不是给小蛇换名字。

## 开始使用

先准备 Git、Python 3.12 或 3.13，以及编程 AI。建议把 Builder 和你的新项目放在两个独立目录。

```powershell
git clone --branch v4.0.1 --depth 1 https://github.com/jackygilbert498-debug/agent-workbench-builder.git
Set-Location agent-workbench-builder
python -B scripts/run_tests.py
```

如果 Windows 没有 `python` 命令，可换成 `py -3.12`；macOS / Linux 通常使用 `python3`，并用 `cd agent-workbench-builder` 进入目录。

然后把下面这段交给 Codex 或其他能够读取本地文件、执行命令的编程 AI，并补充方括号中的内容：

```text
请完整读取这个目录的 SKILL.md 及其要求的 references。
我的使用场景是：[谁，在什么情况下，输入什么，需要得到什么结果]。
可以执行的写操作是：[范围]；未经我批准不能做的事是：[边界]。
在新的独立目录 [项目路径] 帮我搭建，不覆盖已有项目。
先确定场景与验收标准，再执行。不要复制示例业务，也不要修改 DSH 内核。
每一步给我可运行的命令和实际结果；未验证的部分明确标记。
```

不要求先把它安装进某个 AI 产品的 Skill 管理界面；直接让 AI 读取本仓库即可。具体 AI 产品是否支持原生 Skill 安装，应以其当前文档为准。

## DSH 单独安装

默认路线依赖外部 DeepSeek Harness。固定兼容边界是 DSH `dsh-v0.1.0-rc.8`、pnpm `11.7.0`，Node `22.x（>=22.19.0）` 或 `>=24`，不是宣称 DSH 的最新版本。安装、构建与 doctor 的完整命令在 [SKILL.md](SKILL.md) 和 [DSH 工作流](references/dsh-product-workflow.md)。

DSH 的源码、依赖和构建产物留在它自己的目录。Builder 不自动下载 DSH，也不把它塞进你的项目或交接包。首次构建需要网络、时间和足够磁盘空间；doctor 未通过时先解决具体原因。

## 选择适合自己的范围

- `focused-agent`：先把一个主要场景做深，适合第一次搭建。
- `workbench`：同一产品确实需要多项能力时再使用；至少两项能力、三条覆盖所有能力的代表场景。

脚手架生成后是 `starter`，评估结果应为 `PARTIAL`。必须实现自己的领域逻辑、正向与拒绝案例、测试与证据，进入 `domain-adapted`，才可能毕业。不要只改名称、提示词或状态字段。

## 验收和交接

先运行生成项目 README 中的测试和验收命令，再用 `scripts/evaluate_project.py` 评估。`0=PASS`、`2=PARTIAL`、`3=FAIL`。命令不会把未完成项目自动判成毕业。

`scripts/verify_reproduction.py` 可执行中文/空格路径、领域适配及 ZIP 解压复验。对同一个 DSH checkout，两条产品路线必须串行运行。详细流程见 [质量合同](references/quality-contract.md)。

七个机器门覆盖领域适配、代表场景、审批、幂等恢复和交接等工程属性；20 分只是辅助。它们不代表你已经实现了小蛇所有功能，也不能证明任意新手都能独立完成任意项目。是否符合自己的需求，需要真实业务案例和使用者反馈。

发布前有自动化复现与代码审查；独立真人盲测仍为 `NOT-RUN`。真实模型账号、外部服务、其他设备、签名和分发要分别验收，未做的写 `PENDING-EXTERNAL`。秘密扫描是已知模式检查，发布前还要人工核对数据。不要直接运行不可信项目，评估器不是安全沙箱。

业务逻辑、领域测试与 UI 可以修改；Builder 自身的受保护验收工具按版本核对字节，不应为取得 PASS 而改写。保留 `.gitattributes`，避免 Git 的换行转换使跨机器验收失效。
