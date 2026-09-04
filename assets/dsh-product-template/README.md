# __PROJECT_TITLE_TEXT__

这是一个以外部 DeepSeek Harness（DSH）为底座的 `__PROJECT_PRODUCT_KIND_TEXT__` 产品工程。DSH 不在本项目、Builder 或交接包中；请从[官方仓库](https://github.com/deepseek-ai/deepseek-harness)单独安装。

## 产品合同

- 产品类型：`__PROJECT_PRODUCT_KIND_TEXT__`
- 统一目标：__PROJECT_PURPOSE_TEXT__
- 主要使用者：__PROJECT_PRIMARY_USER_TEXT__ 等
- 能力模块、代表性场景及每条场景的触发、输入、可观察结果：见 `agent_project.json`
- 受控写动作：__PROJECT_DANGEROUS_WRITE_TEXT__

`focused-agent` 可以只有一个能力和一条主场景；`workbench` 必须有多个能力模块以及至少三条代表性场景。代表性场景用于验收，不限制用户只能提出这些任务；真正可处理的任务仍受已注册能力、权限与现场约束。

## 第一次运行

安装完成后，在本项目目录也运行一次 `pnpm --version`，必须是 `11.7.0`。DSH 目录中的版本正确，不一定表示当前终端 PATH 的默认版本正确。若不一致，先核对命令来源、让当前终端 PATH 指向正确版本，再从同一终端重跑 doctor 和验收；不要关闭检查或贸然修改全局配置。

1. 在项目之外固定克隆官方 tag `dsh-v0.1.0-rc.8`。它对应 Builder 验证的 commit `141eb6fef83422698aef7a981029e843e8161534`，不是“自动使用最新 DSH”。
2. 在 DSH 根目录确认 Node `22.x（>=22.19.0）` 或 `>=24`，再依次运行 `corepack enable`、`corepack prepare pnpm@11.7.0 --activate`、`pnpm --version`、`pnpm install --frozen-lockfile`、`pnpm run build`。版本输出必须是 `11.7.0`；首次安装与构建通常需要数分钟和数 GB 空间。
3. Builder 是验收工具，不在交接 ZIP 内。若接收方没有它，运行 `git clone --branch v4.0.3 --depth 1 https://github.com/jackygilbert498-debug/agent-workbench-builder.git builder-verification`，进入 `builder-verification`，确认 `scripts/scaffold_project.py` 中版本为 `4.0.3`，再运行 `scripts/dsh_doctor.py`。只有官方 origin、固定 tag/commit、完整追踪树字节、clean checkout 与 live config 全部为 `PASS` 才继续。
4. 回到本项目，运行本地测试与外部运行时验收。Windows PowerShell：

```powershell
$DshRoot = 'C:\path\to\deepseek-harness'
$ProjectRoot = (Get-Location).Path
python tools/test_project.py
python tools/acceptance.py --dsh-root "$DshRoot" --pretty
python tools/run_dsh.py --dsh-root "$DshRoot" web --dump-config
python tools/run_dsh.py --dsh-root "$DshRoot" web --no-open
```

macOS / Linux：

```bash
DSH_ROOT="/path/to/deepseek-harness"
PROJECT_ROOT="$(pwd)"
python3 tools/test_project.py
python3 tools/acceptance.py --dsh-root "$DSH_ROOT" --pretty
python3 tools/run_dsh.py --dsh-root "$DSH_ROOT" web --dump-config
python3 tools/run_dsh.py --dsh-root "$DSH_ROOT" web --no-open
```

`tools/run_dsh.py` 是正式日常启动入口，不只是验收辅助。它把 `.runtime/dsh-home` 固定在原项目；若 Windows 项目路径含中文或空格，则在安全 ASCII 目录维护一份不含 `work`、`.runtime`、证据和依赖的代码 stage，并把持久业务输出明确指回原项目 `work/`。同一项目只允许一个 launcher 占用 stage，源码变化时原子刷新，进程停止后 stage 可作为可重建缓存清理。若系统临时目录本身含中文或空格，先设置 `$env:AGENT_WORKBENCH_STAGE_ROOT = 'C:\awb-runtime'`；应选你控制的专用普通目录，路径本身和现有父级不能是链接或 junction。

`.runtime/dsh-home` 可能包含 Profile、会话和只写 Provider 凭据，`work/` 包含业务账本、收据与输出；两者都属于敏感本机状态，故被排除在 Git 与交接 ZIP 外，但绝不能因此视为可随意丢弃。launcher 内部的 `plugin add` 只把本项目 Bundle 接入外部 DSH，不下载或复制 DSH 源码。不要绕过 launcher 手工执行 `dsh plugin add`；DSH rc8 的 Windows shell 路径限制会让“验收通过、日常启动失败”重新出现。

## 产品边界

- DSH 负责 Agent 循环、会话、模型、工具流水线、审批、沙箱和 Web。
- `agent_project.json` 是产品目标、能力和代表性场景的事实源。
- `src/domain.mjs` 校验统一任务输入并选择场景；`src/capabilities.mjs` 承载能力适配。
- `src/plugin.mjs` 为每个能力注册只读 plan 工具，向模型返回实际业务预览；只有标为 `approval-required` 的能力才有 commit 工具，先保留 DSH 管线后续拒绝或审批，再要求一次性批准。
- `src/workflow.mjs` 负责默认拒绝、原子写、幂等账本、冲突拒绝和可追踪收据。
- `cordis.patch.yml` 是 Product Bundle 层，不修改 DSH 内核。

当前阶段以 `agent_project.json#development.stage` 为准。脚手架刚生成时为 `starter`，模板夹具只能验证工程框架，毕业评估预期为 `PARTIAL`。必须把 `src/capabilities.mjs`、`fixtures/domain-cases.json` 和 `tests/domain-fixtures.test.mjs` 换成目标领域的真实行为，覆盖每条场景、每项能力和至少一个拒绝边界；全部通过后才改为 `domain-adapted`。交付前同步 README、产品约束和验收说明，区分当前状态与过去测试记录。接入真实模型、账号或第三方工具后，另做真实账号验收。

## 打包与回退

```text
python tools/package_handoff.py --pretty
```

macOS / Linux 若没有 `python` 命令，把上行改为 `python3`。

Windows 下项目目录尽量短。若报 `PACKAGE_PATH_TOO_LONG`，表示完整交接文件路径超出支持范围；按下述备份与恢复说明换到更短的新位置，再重新验证。不要关闭检查，也不要直接覆盖旧目录。

交接 ZIP 带逐文件与归档 SHA-256，并在 manifest 中把 DSH 与 Builder `4.0.3` 固定 tag 都声明为 `bundled=false`；它排除 DSH、`node_modules`、`.runtime`，并检查已知秘密和机器路径模式。这不是所有秘密都不存在的证明，发布前还需人工复核。回退时先停止 DSH，把现有 `.runtime/` 与 `work/` 在同卷原子改名为带时间戳的隔离备份并记录哈希清单，再把上一份已验证交接包恢复到另一路径。复验通过后才切换入口；在所有者明确确认凭据、会话和业务数据均不再需要之前保留备份。外部 DSH 不参与回退写入。
