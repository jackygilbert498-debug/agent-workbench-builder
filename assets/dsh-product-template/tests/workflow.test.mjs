import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { CAPABILITIES, SCENARIOS } from '../src/project.mjs'
import { AgentProjectError } from '../src/domain.mjs'
import { capabilityToolToken } from '../src/capabilities.mjs'
import { commitCapability } from '../src/workflow.mjs'

const WRITE_CAPABILITY = CAPABILITIES.find(item => item.risk === 'approval-required')
const WRITE_SCENARIO = SCENARIOS.find(item => item.capabilityIds.includes(WRITE_CAPABILITY.id))

function task(identifier = 'task-001') {
  return {
    task_id: identifier,
    scenario_id: WRITE_SCENARIO.id,
    content: `Approved task for ${WRITE_CAPABILITY.title}`,
  }
}

async function withRoot(run) {
  const root = await mkdtemp(join(tmpdir(), 'agent-workbench-node-test-'))
  try { await run(root) } finally { await rm(root, { recursive: true, force: true }) }
}

test('denial is auditable and has no business output', () => withRoot(async root => {
  const result = await commitCapability(
    WRITE_CAPABILITY.id,
    task('deny-001'),
    { approved: false, runId: 'deny', workRoot: root },
  )
  assert.equal(result.status, 'denied')
  assert.equal(result.sideEffectWritten, false)
  const output = join(root, `output/deny-001-${capabilityToolToken(WRITE_CAPABILITY.id)}.json`)
  await assert.rejects(readFile(output), error => error.code === 'ENOENT')
  assert.equal(JSON.parse(await readFile(join(root, 'receipts/deny.json'), 'utf8')).status, 'denied')
}))

test('receipt is immutable across denied approved and different tasks', () => withRoot(async root => {
  const denied = await commitCapability(
    WRITE_CAPABILITY.id,
    task('immutable-001'),
    { approved: false, runId: 'immutable', workRoot: root },
  )
  const receiptPath = join(root, 'receipts/immutable.json')
  const before = await readFile(receiptPath)
  assert.deepEqual(
    await commitCapability(
      WRITE_CAPABILITY.id,
      task('immutable-001'),
      { approved: false, runId: 'immutable', workRoot: root },
    ),
    denied,
  )
  await assert.rejects(
    commitCapability(
      WRITE_CAPABILITY.id,
      task('immutable-001'),
      { approved: true, runId: 'immutable', workRoot: root },
    ),
    error => error instanceof AgentProjectError && error.code === 'RECEIPT_CONFLICT',
  )
  await assert.rejects(
    readFile(join(root, `output/immutable-001-${capabilityToolToken(WRITE_CAPABILITY.id)}.json`)),
    error => error.code === 'ENOENT',
  )
  await assert.rejects(readFile(join(root, 'state/idempotency.json')), error => error.code === 'ENOENT')
  await assert.rejects(
    commitCapability(
      WRITE_CAPABILITY.id,
      task('immutable-002'),
      { approved: false, runId: 'immutable', workRoot: root },
    ),
    error => error instanceof AgentProjectError && error.code === 'RECEIPT_CONFLICT',
  )
  assert.deepEqual(await readFile(receiptPath), before)
}))

test('oversized ledger is rejected without rewrite', () => withRoot(async root => {
  await mkdir(join(root, 'state'), { recursive: true })
  const ledger = join(root, 'state/idempotency.json')
  const before = Buffer.alloc(1024 * 1024 + 1, 0x20)
  await writeFile(ledger, before)
  await assert.rejects(
    commitCapability(
      WRITE_CAPABILITY.id,
      task('oversized-ledger'),
      { approved: true, runId: 'oversized-ledger', workRoot: root },
    ),
    error => error instanceof AgentProjectError && error.code === 'STATE_CORRUPT',
  )
  assert.deepEqual(await readFile(ledger), before)
}))

test('receipt run ids reject traversal before denied or approved writes', () => withRoot(async root => {
  await mkdir(join(root, 'state'), { recursive: true })
  const ledger = join(root, 'state/idempotency.json')
  await writeFile(ledger, '{"sentinel":true}\n', 'utf8')
  for (const approved of [false, true]) {
    for (const runId of ['../state/idempotency', '../output/known', 'NUL', 'bad/name']) {
      await assert.rejects(
        commitCapability(
          WRITE_CAPABILITY.id,
          task(`traversal-${approved ? 'approved' : 'denied'}`),
          { approved, runId, workRoot: root },
        ),
        error => error instanceof AgentProjectError && error.code === 'INVALID_RUN_ID',
      )
    }
  }
  assert.equal(await readFile(ledger, 'utf8'), '{"sentinel":true}\n')
  await assert.rejects(readFile(join(root, 'output/known.json')), error => error.code === 'ENOENT')
}))

test('three approved runs produce one side effect and one result hash', () => withRoot(async root => {
  const results = []
  for (let index = 1; index <= 3; index += 1) {
    results.push(await commitCapability(
      WRITE_CAPABILITY.id,
      task('run-001'),
      { approved: true, runId: `run-${index}`, workRoot: root },
    ))
  }
  assert.deepEqual(results.map(item => item.status), ['committed', 'replayed', 'replayed'])
  assert.equal(results.filter(item => item.sideEffectWritten).length, 1)
  assert.equal(new Set(results.map(item => item.outcomeHash)).size, 1)
}))

test('concurrent approved runs reserve one idempotency key atomically', () => withRoot(async root => {
  const results = await Promise.all([
    commitCapability(
      WRITE_CAPABILITY.id,
      task('concurrent-001'),
      { approved: true, runId: 'concurrent-a', workRoot: root },
    ),
    commitCapability(
      WRITE_CAPABILITY.id,
      task('concurrent-001'),
      { approved: true, runId: 'concurrent-b', workRoot: root },
    ),
  ])
  assert.deepEqual(results.map(item => item.status).sort(), ['committed', 'replayed'])
  assert.equal(results.filter(item => item.sideEffectWritten).length, 1)
  assert.equal(new Set(results.map(item => item.outcomeHash)).size, 1)
}))

test('concurrent distinct keys preserve both ledger entries and replay', () => withRoot(async root => {
  const identifiers = ['distinct-001', 'distinct-002']
  const results = await Promise.all(identifiers.map((identifier, index) => commitCapability(
    WRITE_CAPABILITY.id,
    task(identifier),
    { approved: true, runId: `distinct-${index}`, workRoot: root },
  )))
  assert.deepEqual(results.map(item => item.status).sort(), ['committed', 'committed'])
  const ledger = JSON.parse(await readFile(join(root, 'state/idempotency.json'), 'utf8'))
  assert.equal(Object.keys(ledger).length, 2)
  const replays = await Promise.all(identifiers.map((identifier, index) => commitCapability(
    WRITE_CAPABILITY.id,
    task(identifier),
    { approved: true, runId: `distinct-replay-${index}`, workRoot: root },
  )))
  assert.deepEqual(replays.map(item => item.status).sort(), ['replayed', 'replayed'])
}))

test('separate Node processes commit one idempotency key once', () => withRoot(async root => {
  const start = join(root, 'process-start')
  const childSource = `
import { access } from 'node:fs/promises'
import { setTimeout as delay } from 'node:timers/promises'
const config = JSON.parse(process.env.AGENT_WORKBENCH_PROCESS_TEST)
const deadline = Date.now() + 10000
while (true) {
  try { await access(config.start); break } catch {}
  if (Date.now() >= deadline) throw new Error('start timeout')
  await delay(10)
}
const { commitCapability } = await import(config.moduleUrl)
const result = await commitCapability(config.capabilityId, config.task, {
  approved: true, runId: config.runId, workRoot: config.root,
})
process.stdout.write(JSON.stringify(result))
`
  const launch = runId => {
    const config = {
      moduleUrl: new URL('../src/workflow.mjs', import.meta.url).href,
      root,
      start,
      runId,
      capabilityId: WRITE_CAPABILITY.id,
      task: task('process-001'),
    }
    return spawn(process.execPath, ['--input-type=module', '--eval', childSource], {
      env: { ...process.env, AGENT_WORKBENCH_PROCESS_TEST: JSON.stringify(config) },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
  }
  const workers = [launch('process-a'), launch('process-b')]
  await writeFile(start, 'go\n', 'utf8')
  const results = await Promise.all(workers.map(worker => new Promise((resolve, reject) => {
    let stdout = ''
    let stderr = ''
    worker.stdout.setEncoding('utf8')
    worker.stderr.setEncoding('utf8')
    worker.stdout.on('data', chunk => { stdout += chunk })
    worker.stderr.on('data', chunk => { stderr += chunk })
    worker.on('error', reject)
    worker.on('close', code => {
      if (code !== 0) reject(new Error(`child exited ${code}: ${stderr}`))
      else resolve(JSON.parse(stdout))
    })
  })))
  assert.deepEqual(results.map(item => item.status).sort(), ['committed', 'replayed'])
  assert.equal(results.filter(item => item.sideEffectWritten).length, 1)
}))

test('separate Node processes preserve distinct ledger keys', () => withRoot(async root => {
  const start = join(root, 'distinct-process-start')
  const childSource = `
import { access } from 'node:fs/promises'
import { setTimeout as delay } from 'node:timers/promises'
const config = JSON.parse(process.env.AGENT_WORKBENCH_PROCESS_TEST)
const deadline = Date.now() + 10000
while (true) {
  try { await access(config.start); break } catch {}
  if (Date.now() >= deadline) throw new Error('start timeout')
  await delay(10)
}
const { commitCapability } = await import(config.moduleUrl)
const result = await commitCapability(config.capabilityId, config.task, {
  approved: true, runId: config.runId, workRoot: config.root,
})
process.stdout.write(JSON.stringify(result))
`
  const launch = index => spawn(process.execPath, ['--input-type=module', '--eval', childSource], {
    env: {
      ...process.env,
      AGENT_WORKBENCH_PROCESS_TEST: JSON.stringify({
        moduleUrl: new URL('../src/workflow.mjs', import.meta.url).href,
        root, start, runId: `distinct-process-${index}`,
        capabilityId: WRITE_CAPABILITY.id,
        task: task(`distinct-process-${index}`),
      }),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const workers = [launch(1), launch(2)]
  await writeFile(start, 'go\n', 'utf8')
  const results = await Promise.all(workers.map(worker => new Promise((resolve, reject) => {
    let stdout = ''
    let stderr = ''
    worker.stdout.setEncoding('utf8')
    worker.stderr.setEncoding('utf8')
    worker.stdout.on('data', chunk => { stdout += chunk })
    worker.stderr.on('data', chunk => { stderr += chunk })
    worker.on('error', reject)
    worker.on('close', code => {
      if (code !== 0) reject(new Error(`child exited ${code}: ${stderr}`))
      else resolve(JSON.parse(stdout))
    })
  })))
  assert.deepEqual(results.map(item => item.status).sort(), ['committed', 'committed'])
  const ledger = JSON.parse(await readFile(join(root, 'state/idempotency.json'), 'utf8'))
  assert.equal(Object.keys(ledger).length, 2)
}))

test('a linked output directory is rejected before any external write', () => withRoot(async root => {
  const outside = await mkdtemp(join(tmpdir(), 'agent-workbench-outside-'))
  try {
    await mkdir(join(root, 'state'), { recursive: true })
    await symlink(outside, join(root, 'output'), process.platform === 'win32' ? 'junction' : 'dir')
    await assert.rejects(
      commitCapability(
        WRITE_CAPABILITY.id,
        task('linked-001'),
        { approved: true, runId: 'linked', workRoot: root },
      ),
      error => error instanceof AgentProjectError && error.code === 'UNSAFE_PATH',
    )
    assert.deepEqual(await import('node:fs/promises').then(fs => fs.readdir(outside)), [])
  } finally {
    await rm(outside, { recursive: true, force: true })
  }
}))

test('a changed tracked output is never overwritten', () => withRoot(async root => {
  await commitCapability(
    WRITE_CAPABILITY.id,
    task('conflict-001'),
    { approved: true, runId: 'first', workRoot: root },
  )
  const target = join(root, `output/conflict-001-${capabilityToolToken(WRITE_CAPABILITY.id)}.json`)
  await writeFile(target, '{}\n', 'utf8')
  await assert.rejects(
    commitCapability(
      WRITE_CAPABILITY.id,
      task('conflict-001'),
      { approved: true, runId: 'retry', workRoot: root },
    ),
    error => error instanceof AgentProjectError && error.code === 'IDEMPOTENCY_CONFLICT',
  )
  assert.equal(await readFile(target, 'utf8'), '{}\n')
}))
