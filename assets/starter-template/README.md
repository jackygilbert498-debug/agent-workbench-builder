# __PROJECT_TITLE_TEXT__

这是一个由 `agent-workbench-builder` 创建的可移植 Agent 工程基线。它实现的场景是：**__PROJECT_SCENARIO_TEXT__**。

## 场景合同

- 主要使用者：__PROJECT_PRIMARY_USER_TEXT__
- 触发条件：__PROJECT_TRIGGER_TEXT__
- 输入：__PROJECT_INPUT_DESCRIPTION_TEXT__
- 可观察输出：__PROJECT_OBSERVABLE_OUTPUT_TEXT__
- 受控写动作：__PROJECT_DANGEROUS_WRITE_TEXT__

项目内置的 `ReferenceProvider` 是确定性的离线适配器，用来证明审批、幂等、恢复、界面和交接链路。当前 `development.stage=starter`，因此毕业评估预期为 `PARTIAL`。它不是外部大模型，也不证明任何第三方账号已经就绪。把项目用于真实业务前，请替换 `agent_workbench/domain.py`、`fixtures/domain-cases.json` 和领域测试，覆盖正向与拒绝边界，最后才改为 `domain-adapted`。

## 运行基线

要求 Python 3.11–3.13；不依赖第三方包。

```text
python -m unittest discover -s tests -v
python tools/acceptance.py --output evidence/acceptance.json
python tools/package_handoff.py --output-dir dist
```

macOS / Linux 若没有 `python` 命令，把上面的 `python` 改为 `python3`。

Windows 下项目目录尽量短。若打包报 `PACKAGE_PATH_TOO_LONG`，按本页恢复说明保留旧项目与数据，选择更短的新位置后复验；不要用覆盖旧目录或关闭检查来绕过。

运行一次批准路径：

```text
python -m agent_workbench.cli --input demo/input/request.json --approve --run-id manual-approved
```

不传 `--approve` 时默认拒绝业务写入：

```text
python -m agent_workbench.cli --input demo/input/request.json --run-id manual-denied
```

启动只读状态页：

```text
python -m agent_workbench.server --host 127.0.0.1 --port 8765
```

然后打开 `http://127.0.0.1:8765/`。健康接口为 `/api/health`，状态接口为 `/api/status`。

## 安全与恢复

- 业务写入只发生在显式 `--approve` 后；默认路径为拒绝。
- 同一请求的幂等键使用跨线程、跨进程文件锁保护；串行或并发重放都只返回一个 committed，其余为 replayed，不重复创建业务产物。
- 原子写入失败不会留下半个 JSON；账本与产物不一致时返回 `IDEMPOTENCY_CONFLICT`，不会盲目覆盖。
- 状态、输出和收据路径中的符号链接或 Windows junction 会以 `UNSAFE_PATH` 拒绝，不跟随到工作目录之外。
- 本地运行数据位于 `work/`，验收使用临时目录，不触碰真实运行数据。
- 回退：停止本地进程，把当前项目目录在同卷原子改名为带时间戳的隔离备份并记录哈希清单；将上一份已验证交接包恢复到另一路径并复验，通过后才切换入口。在所有者明确确认业务数据不再需要之前保留备份。模板不安装服务、不修改系统配置。

## 毕业

模板生成不是毕业。Builder 不在交接 ZIP 内；接收方可运行 `git clone --branch v4.0.2 --depth 1 https://github.com/jackygilbert498-debug/agent-workbench-builder.git builder-verification`，进入 `builder-verification` 后使用版本 `4.0.2` 的 `scripts/evaluate_project.py`。以 `evidence/graduation.json` 的 `PASS`、七个硬门、20 分维度和交接 ZIP 哈希为准。自动 PASS 仍不等于独立真人盲测或真实供应商验收。
