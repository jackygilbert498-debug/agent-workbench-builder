import { createHash, randomUUID } from 'node:crypto'
import { existsSync, realpathSync } from 'node:fs'
import { dirname, isAbsolute, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { CAPABILITIES, PROJECT } from './project.mjs'
import { AgentProjectError } from './domain.mjs'
import { capabilityToolToken, executeCapability, listCapabilityCatalog } from './capabilities.mjs'
import { commitCapability } from './workflow.mjs'

export const name = `${PROJECT.slug}-product-tools`
export const inject = ['tools']
export const PRODUCT_ROOT = realpathSync(resolve(dirname(fileURLToPath(import.meta.url)), '..'))

export function resolveProductStateRoot(configured = process.env.AGENT_WORKBENCH_PRODUCT_ROOT) {
  if (configured === undefined || configured === '') return PRODUCT_ROOT
  if (!isAbsolute(configured)) throw new Error('AGENT_WORKBENCH_PRODUCT_ROOT must be an absolute path')
  const root = realpathSync(configured)
  if (!existsSync(resolve(root, 'agent_project.json'))) {
    throw new Error('AGENT_WORKBENCH_PRODUCT_ROOT is not an Agent workbench project')
  }
  return root
}

export const PRODUCT_STATE_ROOT = resolveProductStateRoot()
export const PRODUCT_WORK_ROOT = resolve(PRODUCT_STATE_ROOT, 'work')

export function executionRunId(callId) {
  const identity = typeof callId === 'string' && callId !== '' ? callId : randomUUID()
  return `dsh-${createHash('sha256').update(identity, 'utf8').digest('hex').slice(0, 40)}`
}

const TASK_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    task_id: { type: 'string', description: 'Stable task identifier.' },
    scenario_id: { type: 'string', description: 'A representative scenario id from the product contract.' },
    content: { type: 'string', description: 'Task content to process.' },
  },
  required: ['task_id', 'scenario_id', 'content'],
}

const RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: true,
  properties: {
    schema: { type: 'string' },
    status: { type: 'string' },
    taskId: { type: 'string' },
    scenarioId: { type: 'string' },
    capabilityId: { type: 'string' },
    sideEffectWritten: { type: 'boolean' },
    outcomeHash: { type: 'string' },
    output: { type: 'string' },
  },
  required: ['schema', 'status', 'taskId', 'scenarioId', 'capabilityId', 'sideEffectWritten', 'outcomeHash'],
}

function renderToolValue(_args, value) {
  // Native tool conversations consume rendered content, not the structured value.
  // Fail visibly when too large: a truncated draft is not a safe approval preview.
  const text = JSON.stringify(value, null, 2)
  if (Buffer.byteLength(text, 'utf8') > 64 * 1024) {
    throw new AgentProjectError(
      'TOOL_OUTPUT_TOO_LARGE',
      'The tool result exceeds the 64 KiB preview limit.',
      'Reduce the result or split the task; preview the complete draft before approving.',
    )
  }
  return [{ type: 'text', text }]
}

function productToken() {
  return PROJECT.slug.replaceAll('-', '_')
}

export function toolNamesForCapability(capability) {
  const base = `${productToken()}_${capabilityToolToken(capability.id)}`
  return {
    plan: `${base}_plan`,
    commit: capability.risk === 'approval-required' ? `${base}_commit` : null,
  }
}

export function applyWithWorkRoot(ctx, workRoot) {
  if (!isAbsolute(workRoot)) throw new Error('Product work root must be an absolute path')
  ctx.tools.register({
    name: `${productToken()}_catalog`,
    description: `List the declared capabilities and representative scenarios for ${PROJECT.title}. This tool is read-only.`,
    parameters: { type: 'object', additionalProperties: false, properties: {} },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          schema: { type: 'string' },
          productKind: { type: 'string' },
          purpose: { type: 'string' },
          capabilities: { type: 'array' },
          scenarios: { type: 'array' },
        },
        required: ['schema', 'productKind', 'purpose', 'capabilities', 'scenarios'],
      },
      render: renderToolValue,
    },
    execute: async () => listCapabilityCatalog(),
    presentCall: () => ({ card: 'generic', title: `${PROJECT.title} capability catalog`, kind: 'search', rawInput: {} }),
  })

  const writeReasons = new Map()
  for (const capability of CAPABILITIES) {
    const names = toolNamesForCapability(capability)
    ctx.tools.register({
      name: names.plan,
      description: `Plan the ${capability.title} capability for ${PROJECT.purpose}. This tool is read-only.`,
      parameters: TASK_SCHEMA,
      output: {
        schema: RESULT_SCHEMA,
        render: renderToolValue,
      },
      execute: async args => executeCapability(capability.id, args),
      presentCall: args => ({ card: 'generic', title: `Plan · ${capability.title}`, kind: 'search', rawInput: args }),
    })
    if (names.commit !== null) {
      writeReasons.set(names.commit, PROJECT.dangerousWrites.join('; '))
      ctx.tools.register({
        name: names.commit,
        description: `Commit the approved ${capability.title} output inside this Product Bundle workspace. This always requires one-time approval.`,
        parameters: TASK_SCHEMA,
        output: {
          schema: RESULT_SCHEMA,
          render: (_args, value) => [{ type: 'text', text: `${value.status}: ${value.output ?? 'no business output'}` }],
        },
        execute: async (args, execution) => commitCapability(capability.id, args, {
          approved: true,
          runId: executionRunId(execution?.callId),
          workRoot,
        }),
        presentCall: args => ({ card: 'generic', title: `Commit · ${capability.title}`, kind: 'write', rawInput: args }),
      })
    }
  }

  ctx.on('tools/pre-execute', async (execution, next) => {
    const reason = writeReasons.get(execution.name)
    if (reason === undefined) return next()
    // A local approval requirement must never hide a later policy rejection.
    const decision = await next()
    if (decision?.kind === 'deny' || decision?.kind === 'ask') return decision
    return { kind: 'ask', reason }
  })
}

export function apply(ctx) {
  return applyWithWorkRoot(ctx, PRODUCT_WORK_ROOT)
}
