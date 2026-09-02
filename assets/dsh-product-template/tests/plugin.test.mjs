import assert from 'node:assert/strict'
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { apply, applyWithWorkRoot, executionRunId, PRODUCT_ROOT, resolveProductStateRoot, toolNamesForCapability } from '../src/plugin.mjs'
import { CAPABILITIES, PROJECT, SCENARIOS } from '../src/project.mjs'

function fixture(workRoot) {
  const definitions = new Map()
  const listeners = []
  const ctx = {
    tools: {
      register(definition) {
        assert.equal(definitions.has(definition.name), false)
        definitions.set(definition.name, definition)
        return () => definitions.delete(definition.name)
      },
    },
    on(event, listener) {
      assert.equal(event, 'tools/pre-execute')
      listeners.push(listener)
    },
  }
  if (workRoot === undefined) apply(ctx)
  else applyWithWorkRoot(ctx, workRoot)
  return { definitions, listeners }
}

test('the Bundle registers a catalog, every capability plan, and only declared write tools', async () => {
  const { definitions, listeners } = fixture()
  const expected = [`${PROJECT.slug.replaceAll('-', '_')}_catalog`]
  for (const capability of CAPABILITIES) {
    const names = toolNamesForCapability(capability)
    expected.push(names.plan)
    if (names.commit !== null) expected.push(names.commit)
  }
  assert.deepEqual([...definitions.keys()].sort(), expected.sort())
  assert.equal(listeners.length, 1)
  for (const capability of CAPABILITIES) {
    const names = toolNamesForCapability(capability)
    const planDecision = await listeners[0]({ name: names.plan }, async () => ({ kind: 'allow' }))
    assert.deepEqual(planDecision, { kind: 'allow' })
    if (names.commit !== null) {
      const writeDecision = await listeners[0]({ name: names.commit }, async () => ({ kind: 'allow' }))
      assert.equal(writeDecision.kind, 'ask')
      assert.match(writeDecision.reason, /.+/u)
    }
  }
})

test('every DSH plan tool executes its declared capability adapter', async () => {
  const { definitions } = fixture()
  for (const capability of CAPABILITIES) {
    const scenario = SCENARIOS.find(item => item.capabilityIds.includes(capability.id))
    const result = await definitions.get(toolNamesForCapability(capability).plan).execute({
      task_id: `tool-${capability.id}`,
      scenario_id: scenario.id,
      content: `Representative input for ${capability.title}`,
    })
    assert.equal(result.capabilityId, capability.id)
    assert.equal(result.scenarioId, scenario.id)
    assert.equal(result.sideEffectWritten, false)
  }
})

test('a commit launched from another cwd writes only under an injected isolated Product work root', async () => {
  const capability = CAPABILITIES.find(item => item.risk === 'approval-required')
  const scenario = SCENARIOS.find(item => item.capabilityIds.includes(capability.id))
  const outside = await mkdtemp(join(tmpdir(), 'agent-workbench-plugin-cwd-'))
  const isolated = await mkdtemp(join(tmpdir(), 'agent-workbench-plugin-state-'))
  const workRoot = join(isolated, 'work')
  const previous = process.cwd()
  try {
    process.chdir(outside)
    const { definitions } = fixture(workRoot)
    const result = await definitions.get(toolNamesForCapability(capability).commit).execute(
      {
        task_id: 'non-project-cwd-001',
        scenario_id: scenario.id,
        content: 'approved input from an external runtime cwd',
      },
      { callId: 'provider:session/non-project-cwd' },
    )
    assert.equal(result.status, 'committed')
    assert.equal(JSON.parse(await readFile(join(workRoot, result.output), 'utf8')).taskId, 'non-project-cwd-001')
    const receipts = await readdir(join(workRoot, 'receipts'))
    assert.deepEqual(receipts, [`${executionRunId('provider:session/non-project-cwd')}.json`])
    assert.match(receipts[0], /^dsh-[0-9a-f]{40}\.json$/u)
    assert.deepEqual(await readdir(outside), [])
  } finally {
    process.chdir(previous)
    await rm(isolated, { recursive: true, force: true })
    await rm(outside, { recursive: true, force: true })
  }
})

test('the runtime launcher can keep persistent state in the original project root', async () => {
  const original = await mkdtemp(join(tmpdir(), 'agent-workbench-original-state-'))
  try {
    await import('node:fs/promises').then(({ writeFile }) => writeFile(join(original, 'agent_project.json'), '{}\n', 'utf8'))
    assert.equal(resolveProductStateRoot(original), original)
    assert.equal(resolveProductStateRoot(), PRODUCT_ROOT)
    assert.throws(() => resolveProductStateRoot('relative/project'), /absolute path/u)
  } finally {
    await rm(original, { recursive: true, force: true })
  }
})
