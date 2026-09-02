import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { AgentProjectError } from '../src/domain.mjs'
import { executeCapability } from '../src/capabilities.mjs'

function assertSubset(actual, expected) {
  for (const [key, value] of Object.entries(expected)) assert.deepEqual(actual[key], value)
}

test('domain fixtures execute with declared expectations', async () => {
  const fixtures = JSON.parse(await readFile(new URL('../fixtures/domain-cases.json', import.meta.url), 'utf8'))
  assert.equal(fixtures.schema, 'agent-workbench-domain-fixtures/v1')
  assert.ok(fixtures.cases.some(item => item.kind === 'positive'))
  assert.ok(fixtures.cases.some(item => item.kind === 'boundary'))
  for (const item of fixtures.cases) {
    if (item.kind === 'positive') {
      assertSubset(executeCapability(item.capabilityId, item.input), item.expected)
    } else {
      assert.throws(
        () => executeCapability(item.capabilityId, item.input),
        error => error instanceof AgentProjectError && error.code === item.expectedError,
      )
    }
  }
})
