/** Exercise the generated product through the pinned, real DSH registry.
 * Approval responses and the agent session are synthetic; no model/account is used.
 * Run only against a trusted generated project, with both root variables set.
 */
import assert from 'node:assert/strict'
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { isAbsolute, join, resolve } from 'node:path'
import test from 'node:test'
import { pathToFileURL } from 'node:url'

const projectRoot = process.env.AWB_PRODUCT_TEST_ROOT
const dshRoot = process.env.AWB_DSH_TEST_ROOT
if (!projectRoot || !dshRoot || !isAbsolute(projectRoot) || !isAbsolute(dshRoot)) {
  throw new Error('Set absolute AWB_PRODUCT_TEST_ROOT and AWB_DSH_TEST_ROOT for this opt-in integration test.')
}
process.env.AGENT_WORKBENCH_PRODUCT_ROOT = projectRoot
const productModule = relative => import(pathToFileURL(resolve(projectRoot, relative)).href)
const { applyWithWorkRoot, toolNamesForCapability } = await productModule('src/plugin.mjs')
const { CAPABILITIES, SCENARIOS, PROJECT } = await productModule('src/project.mjs')
const { commitCapability } = await productModule('src/workflow.mjs')
const requireDsh = createRequire(resolve(dshRoot, 'packages/core/tools/package.json'))
const dshModule = name => import(pathToFileURL(requireDsh.resolve(name)).href)
const { Context } = await dshModule('@deepseek-ai/cordis')
const { default: ToolRuntime } = await dshModule('@deepseek-ai/dsh-tools')
const { default: SystemPrompt } = await dshModule('@deepseek-ai/dsh-system-prompt')
const { default: ApprovalService } = await dshModule('@deepseek-ai/dsh-user-approval')
const capability = CAPABILITIES.find(item => item.risk === 'approval-required')
const scenario = SCENARIOS.find(item => item.capabilityIds.includes(capability.id))
const args = { task_id: 'native-check', scenario_id: scenario.id, content: 'Login shows 403; needed today.' }

async function withRuntime(run) {
  const workRoot = await mkdtemp(join(tmpdir(), 'awb-native-contract-'))
  const ctx = new Context()
  try {
    await ctx.plugin(SystemPrompt)
    await ctx.plugin(ToolRuntime)
    await ctx.plugin(ApprovalService)
    applyWithWorkRoot(ctx, workRoot)
    await run(ctx, workRoot)
  } finally {
    try { await ctx.fiber.dispose() } finally { await rm(workRoot, { recursive: true, force: true }) }
  }
}

function call(name, argumentsValue = args) {
  return {
    callId: `native-${name}`, name, arguments: argumentsValue,
    signal: new AbortController().signal,
    agent: { session: { events: [{ type: 'turn/start' }], append: () => ({}) } },
  }
}

test('a downstream refusal blocks the actual write and never asks for permission', () => withRuntime(async (ctx, root) => {
  ctx.on('tools/pre-execute', async () => ({ kind: 'deny', reason: 'Business writes are disabled.' }))
  let asked = false
  ctx.on('approval/request', async () => { asked = true; return 'allowed-once' })
  const result = await ctx.tools.execute(call(toolNamesForCapability(capability).commit))
  assert.equal(result.isError, true)
  assert.equal(asked, false)
  assert.deepEqual(await readdir(join(root, 'output')).catch(() => []), [])
}))

test('the monotonic guard also blocks an otherwise approved write', () => withRuntime(async (ctx, root) => {
  ctx.tools.guard(() => 'Deployment guard disabled writes.')
  ctx.on('approval/request', async () => 'allowed-once')
  const result = await ctx.tools.execute(call(toolNamesForCapability(capability).commit))
  assert.equal(result.isError, true)
  assert.deepEqual(await readdir(join(root, 'output')).catch(() => []), [])
}))

test('native catalog content exposes the real scenario identifiers', () => withRuntime(async ctx => {
  const result = await ctx.tools.execute(call(`${PROJECT.slug.replaceAll('-', '_')}_catalog`, {}))
  assert.equal(result.isError, false)
  const text = result.content.map(item => item.text ?? '').join('\n')
  assert.match(text, /"scenarios"/u)
  assert.equal(JSON.parse(text).scenarios[0].id, SCENARIOS[0].id)
}))

test('native plan content contains the draft before any write', () => withRuntime(async (ctx, root) => {
  const result = await ctx.tools.execute(call(toolNamesForCapability(capability).plan))
  assert.equal(result.isError, false)
  const text = result.content.map(item => item.text ?? '').join('\n')
  assert.match(text, /Login shows 403; needed today\./u)
  assert.equal(JSON.parse(text).sideEffectWritten, false)
  assert.deepEqual(await readdir(join(root, 'output')).catch(() => []), [])
}))

test('an explicitly approved native write still produces its declared local artifact', () => withRuntime(async (ctx, root) => {
  ctx.on('approval/request', async () => 'allowed-once')
  const result = await ctx.tools.execute(call(toolNamesForCapability(capability).commit))
  assert.equal(result.isError, false)
  assert.equal(result.value.status, 'committed')
  assert.equal(JSON.parse(await readFile(join(root, result.value.output), 'utf8')).taskId, 'native-check')
}))

test('a changed long-input tail conflicts without altering the existing artifact', () => withRuntime(async (_ctx, root) => {
  const first = { ...args, content: 'Ordinary detail. '.repeat(20) + 'attachment A' }
  const saved = await commitCapability(capability.id, first, { approved: true, runId: 'tail-first', workRoot: root })
  const before = await readFile(join(root, saved.output))
  await assert.rejects(commitCapability(capability.id, {
    ...first, content: 'Ordinary detail. '.repeat(20) + 'attachment B',
  }, { approved: true, runId: 'tail-second', workRoot: root }), error => error.code === 'IDEMPOTENCY_CONFLICT')
  assert.deepEqual(await readFile(join(root, saved.output)), before)
}))
