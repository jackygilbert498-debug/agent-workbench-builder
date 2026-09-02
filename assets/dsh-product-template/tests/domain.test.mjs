import assert from 'node:assert/strict'
import test from 'node:test'

import { CAPABILITIES, PROJECT, SCENARIOS } from '../src/project.mjs'
import { AgentProjectError, createPlan, validateTask } from '../src/domain.mjs'

test('the selected product kind has the required capability and scenario shape', () => {
  if (PROJECT.productKind === 'workbench') {
    assert.ok(CAPABILITIES.length >= 2)
    assert.ok(SCENARIOS.length >= 3)
  } else {
    assert.equal(PROJECT.productKind, 'focused-agent')
    assert.equal(CAPABILITIES.length, 1)
    assert.equal(SCENARIOS.length, 1)
  }
  assert.equal(SCENARIOS.filter(item => item.primary).length, 1)
  const covered = new Set(SCENARIOS.flatMap(item => item.capabilityIds))
  assert.deepEqual([...covered].sort(), CAPABILITIES.map(item => item.id).sort())
})

test('every representative scenario produces a stable plan', () => {
  for (const scenario of SCENARIOS) {
    const input = {
      task_id: `task-${scenario.id}`,
      scenario_id: scenario.id,
      content: `Urgent representative task for ${scenario.title} today`,
    }
    const first = createPlan(input)
    const second = createPlan(input)
    assert.deepEqual(first, second)
    assert.deepEqual(first.capability_ids, scenario.capabilityIds)
    assert.match(first.outcomeHash, /^[a-f0-9]{64}$/u)
  }
})

test('invalid, extended, and unknown-scenario inputs have stable recovery codes', () => {
  assert.throws(
    () => validateTask({ task_id: 'task-001', scenario_id: 'missing', content: '', extra: true }),
    error => error instanceof AgentProjectError && error.code === 'INVALID_TASK' && error.recovery.length > 0,
  )
  assert.throws(
    () => validateTask({ task_id: 'task-001', scenario_id: 'missing', content: 'value' }),
    error => error instanceof AgentProjectError && error.code === 'UNKNOWN_SCENARIO' && error.recovery.length > 0,
  )
})
