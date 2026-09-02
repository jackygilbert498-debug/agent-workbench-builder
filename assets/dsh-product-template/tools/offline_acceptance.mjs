import { createHash } from 'node:crypto'
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { CAPABILITIES, PROJECT, SCENARIOS } from '../src/project.mjs'
import { AgentProjectError } from '../src/domain.mjs'
import { executeCapability } from '../src/capabilities.mjs'
import { commitCapability } from '../src/workflow.mjs'

const root = await mkdtemp(join(tmpdir(), 'agent-workbench-offline-'))
try {
  const contract = JSON.parse(await readFile(new URL('../agent_project.json', import.meta.url), 'utf8'))
  const fixtureBytes = await readFile(new URL('../fixtures/domain-cases.json', import.meta.url))
  const fixtureSet = JSON.parse(fixtureBytes.toString('utf8'))
  if (fixtureSet.schema !== 'agent-workbench-domain-fixtures/v1' || !Array.isArray(fixtureSet.cases)) {
    throw new Error('domain fixture schema is invalid')
  }
  const fixtureRuns = fixtureSet.cases.map(item => {
    let passed = false
    if (item.kind === 'positive') {
      const actual = executeCapability(item.capabilityId, item.input)
      passed = Object.entries(item.expected ?? {}).every(([key, value]) => (
        JSON.stringify(actual[key]) === JSON.stringify(value)
      ))
    } else if (item.kind === 'boundary') {
      try {
        executeCapability(item.capabilityId, item.input)
      } catch (error) {
        passed = error instanceof AgentProjectError && error.code === item.expectedError
      }
    }
    return {
      id: item.id,
      kind: item.kind,
      scenarioId: item.scenarioId,
      capabilityId: item.capabilityId,
      passed,
    }
  })
  const positiveFixtures = fixtureRuns.filter(item => item.kind === 'positive')
  const boundaryFixtures = fixtureRuns.filter(item => item.kind === 'boundary')
  const fixtureScenarioCoverage = new Set(positiveFixtures.map(item => item.scenarioId))
  const fixtureCapabilityCoverage = new Set(positiveFixtures.map(item => item.capabilityId))
  const fixturesPassed = positiveFixtures.length >= SCENARIOS.length
    && boundaryFixtures.length >= 1
    && fixtureScenarioCoverage.size === SCENARIOS.length
    && fixtureCapabilityCoverage.size === CAPABILITIES.length
    && fixtureRuns.every(item => item.passed)
  const domainAdaptation = {
    schema: 'agent-workbench-domain-adaptation/v1',
    status: fixturesPassed && contract.development.stage === 'domain-adapted' ? 'PASS' : 'PARTIAL',
    passed: fixturesPassed && contract.development.stage === 'domain-adapted',
    fixturesPassed,
    stage: contract.development.stage,
    fixtureSha256: createHash('sha256').update(fixtureBytes).digest('hex'),
    fixtureCount: fixtureRuns.length,
    positiveCases: positiveFixtures.length,
    boundaryCases: boundaryFixtures.length,
    coveredScenarios: [...fixtureScenarioCoverage].sort(),
    coveredCapabilities: [...fixtureCapabilityCoverage].sort(),
    cases: fixtureRuns,
  }
  const scenarioRuns = SCENARIOS.map((scenario, index) => {
    const capabilityResults = scenario.capabilityIds.map(capabilityId => executeCapability(
      capabilityId,
      {
        task_id: `scenario-${index + 1}-${capabilityId}`,
        scenario_id: scenario.id,
        content: `Representative local input for ${scenario.title}`,
      },
    ))
    return {
      id: scenario.id,
      status: capabilityResults.every(item => item.status === 'planned') ? 'PASS' : 'FAIL',
      capabilityIds: scenario.capabilityIds,
      outcomeHashes: capabilityResults.map(item => item.outcomeHash),
    }
  })
  const coveredCapabilities = new Set(scenarioRuns.flatMap(item => item.capabilityIds))
  const multiScenario = {
    passed: scenarioRuns.every(item => item.status === 'PASS')
      && coveredCapabilities.size === CAPABILITIES.length,
    productKind: PROJECT.productKind,
    declaredScenarios: SCENARIOS.length,
    passedScenarios: scenarioRuns.filter(item => item.status === 'PASS').length,
    declaredCapabilities: CAPABILITIES.length,
    coveredCapabilities: coveredCapabilities.size,
    scenarios: scenarioRuns,
  }

  const writeCapability = CAPABILITIES.find(item => item.risk === 'approval-required')
  const writeScenario = SCENARIOS.find(item => item.capabilityIds.includes(writeCapability.id))
  const request = {
    task_id: 'approved-001',
    scenario_id: writeScenario.id,
    content: `Approved representative task for ${writeCapability.title}`,
  }
  const approved = []
  for (let index = 1; index <= 3; index += 1) {
    approved.push(await commitCapability(
      writeCapability.id,
      request,
      { approved: true, runId: `approved-${index}`, workRoot: root },
    ))
  }
  const denied = await commitCapability(
    writeCapability.id,
    { ...request, task_id: 'denied-001' },
    { approved: false, runId: 'denied', workRoot: root },
  )
  let recovery
  try {
    await commitCapability(
      writeCapability.id,
      { ...request, task_id: 'invalid-001', content: '' },
      { approved: true, runId: 'invalid', workRoot: root },
    )
  } catch (error) {
    if (!(error instanceof AgentProjectError)) throw error
    recovery = error.asObject()
  }
  const outputFiles = await readdir(join(root, 'output'))
  const hashes = new Set(approved.map(item => item.outcomeHash))
  const sideEffectWrites = approved.filter(item => item.sideEffectWritten).length
  const endToEndPassed = multiScenario.passed && outputFiles.length === 1
  const payload = {
    status: endToEndPassed
      && fixturesPassed
      && hashes.size === 1
      && sideEffectWrites === 1
      && denied.status === 'denied'
      && recovery?.code === 'INVALID_TASK'
      ? 'PASS'
      : 'FAIL',
    multiScenario,
    domainAdaptation,
    endToEnd: {
      passed: endToEndPassed,
      businessOutputs: outputFiles.length,
      representativeScenarios: SCENARIOS.length,
    },
    approval: {
      passed: denied.status === 'denied' && denied.sideEffectWritten === false,
      approvedStatus: approved[0].status,
      deniedStatus: denied.status,
      deniedSideEffectWritten: denied.sideEffectWritten,
    },
    idempotency: {
      passed: hashes.size === 1 && sideEffectWrites === 1,
      runs: 3,
      statuses: approved.map(item => item.status),
      distinctOutcomeHashes: hashes.size,
      sideEffectWrites,
      outcomeHash: approved[0].outcomeHash,
    },
    recovery: {
      passed: recovery?.code === 'INVALID_TASK' && recovery.recovery.length > 0,
      error: recovery,
    },
  }
  payload.digest = createHash('sha256').update(JSON.stringify(payload)).digest('hex')
  process.stdout.write(`${JSON.stringify(payload)}\n`)
} finally {
  await rm(root, { recursive: true, force: true })
}
