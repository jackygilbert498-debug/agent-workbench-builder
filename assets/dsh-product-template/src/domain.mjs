import { createHash } from 'node:crypto'

import { CAPABILITIES, SCENARIOS } from './project.mjs'

export class AgentProjectError extends Error {
  constructor(code, message, recovery) {
    super(message)
    this.name = 'AgentProjectError'
    this.code = code
    this.recovery = recovery
  }

  asObject() {
    return { code: this.code, message: this.message, recovery: this.recovery }
  }
}

function requiredText(value, field, maximum) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new AgentProjectError('INVALID_TASK', `${field} must be a non-empty string`, `Provide a non-empty ${field}.`)
  }
  const normalized = value.trim()
  if (normalized.length > maximum) {
    throw new AgentProjectError('INVALID_TASK', `${field} exceeds ${maximum} characters`, `Shorten ${field} and retry.`)
  }
  if (/\p{Cc}/u.test(normalized)) {
    throw new AgentProjectError('INVALID_TASK', `${field} contains control characters`, `Remove control characters from ${field}.`)
  }
  return normalized
}

function scenarioById(identifier) {
  return SCENARIOS.find(item => item.id === identifier)
}

export function validateTask(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new AgentProjectError('INVALID_TASK', 'task must be an object', 'Send task_id, scenario_id, and content.')
  }
  const allowed = new Set(['task_id', 'scenario_id', 'content'])
  const unknown = Object.keys(value).filter(key => !allowed.has(key))
  if (unknown.length > 0) {
    throw new AgentProjectError('INVALID_TASK', `unknown task fields: ${unknown.join(', ')}`, 'Remove fields outside task_id, scenario_id, and content.')
  }
  const taskId = requiredText(value.task_id, 'task_id', 80)
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/u.test(taskId)) {
    throw new AgentProjectError('INVALID_TASK', 'task_id has an unsafe format', 'Use letters, digits, dots, underscores, or hyphens.')
  }
  const scenarioId = requiredText(value.scenario_id, 'scenario_id', 80)
  const scenario = scenarioById(scenarioId)
  if (scenario === undefined) {
    throw new AgentProjectError(
      'UNKNOWN_SCENARIO',
      `scenario_id is not declared: ${scenarioId}`,
      `Choose one of: ${SCENARIOS.map(item => item.id).join(', ')}.`,
    )
  }
  return Object.freeze({
    task_id: taskId,
    scenario_id: scenarioId,
    content: requiredText(value.content, 'content', 8000),
  })
}

export function createPlan(input) {
  const task = validateTask(input)
  const scenario = scenarioById(task.scenario_id)
  const capabilityIds = [...scenario.capabilityIds]
  const unknownCapability = capabilityIds.find(
    identifier => !CAPABILITIES.some(capability => capability.id === identifier),
  )
  if (unknownCapability !== undefined) {
    throw new AgentProjectError(
      'BROKEN_BLUEPRINT',
      `scenario references an unknown capability: ${unknownCapability}`,
      'Repair agent_project.json before running this scenario.',
    )
  }
  const canonical = {
    task_id: task.task_id,
    scenario_id: scenario.id,
    scenario_title: scenario.title,
    capability_ids: capabilityIds,
    summary: task.content.slice(0, 240),
    observable_output: scenario.observableOutput,
  }
  const outcomeHash = createHash('sha256').update(JSON.stringify(canonical)).digest('hex')
  return Object.freeze({ ...canonical, outcomeHash })
}
