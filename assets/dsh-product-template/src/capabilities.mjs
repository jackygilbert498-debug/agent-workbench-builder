import { CAPABILITIES, PROJECT, SCENARIOS } from './project.mjs'
import { AgentProjectError, createPlan, validateTask } from './domain.mjs'

function capabilityById(identifier) {
  return CAPABILITIES.find(item => item.id === identifier)
}

export function capabilityToolToken(identifier) {
  return identifier.replaceAll('-', '_')
}

export function listCapabilityCatalog() {
  return {
    schema: 'agent-workbench-capability-catalog/v3',
    productKind: PROJECT.productKind,
    purpose: PROJECT.purpose,
    capabilities: CAPABILITIES.map(item => ({ ...item })),
    scenarios: SCENARIOS.map(item => ({
      id: item.id,
      title: item.title,
      primary: item.primary,
      capabilityIds: [...item.capabilityIds],
    })),
  }
}

export function executeCapability(capabilityId, input) {
  const task = validateTask(input)
  const capability = capabilityById(capabilityId)
  if (capability === undefined) {
    throw new AgentProjectError(
      'UNKNOWN_CAPABILITY',
      `capability is not declared: ${capabilityId}`,
      'Choose a capability from the product catalog.',
    )
  }
  const scenario = SCENARIOS.find(item => item.id === task.scenario_id)
  if (!scenario.capabilityIds.includes(capabilityId)) {
    throw new AgentProjectError(
      'CAPABILITY_NOT_IN_SCENARIO',
      `${capabilityId} is not part of scenario ${scenario.id}`,
      'Use one of the capabilityIds declared by the selected scenario.',
    )
  }
  const plan = createPlan(task)
  const normalized = task.content.toLocaleLowerCase('en-US')
  const urgent = /\b(urgent|asap|blocker|today)\b|紧急|立即|今天/u.test(normalized)
  return Object.freeze({
    schema: 'agent-workbench-capability-result/v3',
    status: 'planned',
    taskId: task.task_id,
    scenarioId: scenario.id,
    capabilityId,
    capabilityTitle: capability.title,
    risk: capability.risk,
    urgency: urgent ? 'high' : 'normal',
    summary: plan.summary,
    observableOutput: plan.observable_output,
    sideEffectWritten: false,
    outcomeHash: plan.outcomeHash,
  })
}
