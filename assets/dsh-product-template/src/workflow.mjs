import { createHash, randomBytes } from 'node:crypto'
import { link, lstat, mkdir, open, readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname, isAbsolute, join, parse, relative, resolve, sep } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

import { CAPABILITIES } from './project.mjs'
import { AgentProjectError, validateTask } from './domain.mjs'
import { capabilityToolToken, executeCapability } from './capabilities.mjs'

const LOCK_TIMEOUT_MS = 10_000
const LOCK_POLL_MS = 20
const RUN_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u
const WINDOWS_DEVICE_RE = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$/iu
const MAX_LEDGER_BYTES = 1024 * 1024
const MAX_LEDGER_ENTRIES = 10_000
const MAX_RECEIPT_BYTES = 1024 * 1024

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonicalize(value[key])]))
  }
  return value
}

function canonicalBytes(value) {
  return Buffer.from(`${JSON.stringify(canonicalize(value), null, 2)}\n`, 'utf8')
}

function digest(data) {
  return createHash('sha256').update(data).digest('hex')
}

function validateRunId(value) {
  if (typeof value !== 'string' || !RUN_ID_RE.test(value) || WINDOWS_DEVICE_RE.test(value)) {
    throw new AgentProjectError(
      'INVALID_RUN_ID',
      'runId must be a safe filename token',
      'Pass 1-128 ASCII letters, digits, underscores, or hyphens; never pass a path or Windows device name.',
    )
  }
  return value
}

function assertInside(root, candidate) {
  const base = resolve(root)
  const target = resolve(candidate)
  const delta = relative(base, target)
  if (delta === '..' || delta.startsWith(`..${sep}`) || isAbsolute(delta)) {
    throw new AgentProjectError('PATH_ESCAPE', 'resolved output escaped the work root', 'Choose a work root that owns state, output, and receipts.')
  }
  return target
}

async function assertNoLinkComponents(path) {
  const absolute = resolve(path)
  const root = parse(absolute).root
  const remainder = relative(root, absolute)
  let current = root
  for (const part of remainder.split(sep).filter(Boolean)) {
    current = join(current, part)
    try {
      const info = await lstat(current)
      if (info.isSymbolicLink()) {
        throw new AgentProjectError('UNSAFE_PATH', `linked path component is not accepted: ${part}`, 'Use a work root whose state, output, and receipts contain no links or junctions.')
      }
    } catch (error) {
      if (error instanceof AgentProjectError) throw error
      if (error && typeof error === 'object' && error.code === 'ENOENT') return
      throw new AgentProjectError('UNSAFE_PATH', `cannot inspect path component: ${part}`, 'Choose a readable local work root and retry.')
    }
  }
}

async function secureMkdir(path) {
  await assertNoLinkComponents(dirname(path))
  await mkdir(path, { recursive: true })
  await assertNoLinkComponents(path)
}

async function atomicWrite(path, data) {
  await secureMkdir(dirname(path))
  await assertNoLinkComponents(path)
  const temporary = `${path}.tmp-${process.pid}-${randomBytes(6).toString('hex')}`
  try {
    await writeFile(temporary, data, { flag: 'wx', mode: 0o600 })
    await rename(temporary, path)
  } catch (error) {
    try { await import('node:fs/promises').then(fs => fs.unlink(temporary)) } catch {}
    throw error
  }
}

async function readBoundedFile(path, maximum) {
  await assertNoLinkComponents(path)
  const handle = await open(path, 'r')
  try {
    const info = await handle.stat()
    if (!info.isFile() || info.size > maximum) throw new Error(`file exceeds ${maximum} bytes or is not regular`)
    const chunks = []
    let observed = 0
    while (observed <= maximum) {
      const buffer = Buffer.alloc(Math.min(64 * 1024, maximum + 1 - observed))
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, null)
      if (bytesRead === 0) break
      chunks.push(buffer.subarray(0, bytesRead))
      observed += bytesRead
      if (observed > maximum) throw new Error(`file exceeds ${maximum} bytes while reading`)
    }
    return Buffer.concat(chunks, observed)
  } finally {
    await handle.close()
  }
}

async function writeReceiptOnce(path, data) {
  if (data.length > MAX_RECEIPT_BYTES) {
    throw new AgentProjectError('RECEIPT_LIMIT', 'receipt exceeds its storage limit', 'Reduce receipt metadata before retrying with a new runId.')
  }
  await secureMkdir(dirname(path))
  await assertNoLinkComponents(path)
  const temporary = `${path}.tmp-${process.pid}-${randomBytes(6).toString('hex')}`
  try {
    const handle = await open(temporary, 'wx', 0o600)
    try {
      await handle.writeFile(data)
      await handle.sync()
    } finally {
      await handle.close()
    }
    try {
      await link(temporary, path)
      return true
    } catch (error) {
      if (!(error && typeof error === 'object' && error.code === 'EEXIST')) throw error
      const existing = await readBoundedFile(path, MAX_RECEIPT_BYTES)
      if (existing.equals(data)) return false
      throw new AgentProjectError('RECEIPT_CONFLICT', 'runId already belongs to a different immutable receipt', 'Use a new runId; inspect the existing receipt and never overwrite it.')
    }
  } finally {
    try { await unlink(temporary) } catch (error) {
      if (!(error && typeof error === 'object' && error.code === 'ENOENT')) throw error
    }
  }
}

async function assertReceiptUnused(path) {
  try {
    await readBoundedFile(path, MAX_RECEIPT_BYTES)
  } catch (error) {
    if (error && typeof error === 'object' && error.code === 'ENOENT') return
    if (error instanceof AgentProjectError) throw error
    throw new AgentProjectError('RECEIPT_CONFLICT', 'existing receipt cannot be validated for reuse', 'Inspect the existing receipt and use a new runId.')
  }
  throw new AgentProjectError('RECEIPT_CONFLICT', 'runId already belongs to an immutable receipt', 'Use a new runId; inspect the existing receipt and never overwrite it.')
}

async function readLedger(path) {
  try {
    await assertNoLinkComponents(path)
    const parsed = JSON.parse((await readBoundedFile(path, MAX_LEDGER_BYTES)).toString('utf8'))
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not an object')
    if (Object.keys(parsed).length > MAX_LEDGER_ENTRIES) throw new Error('too many ledger entries')
    return parsed
  } catch (error) {
    if (error && typeof error === 'object' && error.code === 'ENOENT') return {}
    throw new AgentProjectError('STATE_CORRUPT', 'idempotency ledger is unreadable', 'Restore the ledger from backup or inspect it before retrying.')
  }
}

async function reserveIdempotencyKey(workRoot, ledgerKey) {
  const lockRoot = assertInside(workRoot, join(workRoot, 'state', '.locks'))
  await secureMkdir(lockRoot)
  const lockPath = assertInside(lockRoot, join(lockRoot, `${digest(ledgerKey)}.lock`))
  const token = randomBytes(16).toString('hex')
  const deadline = Date.now() + LOCK_TIMEOUT_MS
  while (true) {
    await assertNoLinkComponents(lockPath)
    let handle
    try {
      handle = await open(lockPath, 'wx', 0o600)
    } catch (error) {
      const code = error && typeof error === 'object' ? error.code : undefined
      // Windows can report EPERM while another process releases a lock. Retry
      // only exclusive-open failures, never owner-record writes or close errors.
      // Inspection does not grant ownership: each retry must still acquire wx.
      let transientSharingError = false
      if (process.platform === 'win32' && code === 'EPERM') {
        try {
          const info = await lstat(lockPath)
          transientSharingError = info.isFile() && !info.isSymbolicLink()
        } catch (inspectionError) {
          transientSharingError = inspectionError?.code === 'ENOENT'
        }
      }
      if (code !== 'EEXIST' && !transientSharingError) throw error
      if (Date.now() >= deadline) {
        if (transientSharingError) {
          throw new AgentProjectError('LOCK_UNAVAILABLE', 'the local task lock remained unavailable', 'Wait for other workers to finish and check directory permissions. Inspect crash locks before removing anything; no business write was authorized by this failure.')
        }
        throw new AgentProjectError('IDEMPOTENCY_BUSY', 'another worker still owns this idempotency key', 'Wait for that worker to finish. If its process crashed, inspect and remove only this stale lock before retrying.')
      }
      await delay(LOCK_POLL_MS)
      continue
    }
    try {
      await handle.writeFile(canonicalBytes({ pid: process.pid, token }))
    } finally {
      await handle.close()
    }
    return async () => {
      try {
        const owner = JSON.parse(await readFile(lockPath, 'utf8'))
        if (owner.token === token) await unlink(lockPath)
      } catch (error) {
        if (!(error && typeof error === 'object' && error.code === 'ENOENT')) throw error
      }
    }
  }
}

export async function commitCapability(capabilityId, input, options = {}) {
  const task = validateTask(input)
  const capability = CAPABILITIES.find(item => item.id === capabilityId)
  if (capability === undefined) {
    throw new AgentProjectError('UNKNOWN_CAPABILITY', `capability is not declared: ${capabilityId}`, 'Choose a capability from the product catalog.')
  }
  if (capability.risk !== 'approval-required') {
    throw new AgentProjectError('READ_ONLY_CAPABILITY', `${capabilityId} is read-only`, 'Use its plan tool; it has no commit operation.')
  }
  const planned = executeCapability(capabilityId, task)
  const approved = options.approved === true
  const runId = validateRunId(options.runId ?? 'run')
  const workRoot = resolve(options.workRoot ?? 'work')
  const outputName = `${task.task_id}-${capabilityToolToken(capabilityId)}.json`
  const outputPath = assertInside(workRoot, join(workRoot, 'output', outputName))
  const receiptRoot = assertInside(workRoot, join(workRoot, 'receipts'))
  const receiptPath = assertInside(receiptRoot, join(receiptRoot, `${runId}.json`))
  const ledgerPath = assertInside(workRoot, join(workRoot, 'state', 'idempotency.json'))
  const ledgerKey = `${task.scenario_id}:${capabilityId}:${task.task_id}`

  await assertNoLinkComponents(workRoot)
  await assertNoLinkComponents(outputPath)
  await assertNoLinkComponents(receiptPath)
  await assertNoLinkComponents(ledgerPath)

  const releaseReceiptReservation = await reserveIdempotencyKey(workRoot, `receipt:${runId}`)
  try {
  if (approved) await assertReceiptUnused(receiptPath)
  if (!approved) {
    const receipt = {
      schema: 'agent-workbench-run/v3',
      status: 'denied',
      taskId: task.task_id,
      scenarioId: task.scenario_id,
      capabilityId,
      sideEffectWritten: false,
      outcomeHash: planned.outcomeHash,
    }
    await writeReceiptOnce(receiptPath, canonicalBytes(receipt))
    return receipt
  }

  const releaseReservation = await reserveIdempotencyKey(workRoot, ledgerKey)
  let releaseLedgerTransaction
  try {
    releaseLedgerTransaction = await reserveIdempotencyKey(workRoot, '__shared-ledger-transaction__')
    const output = { schema: 'agent-workbench-business-output/v3', ...planned, status: 'completed' }
    const outputBytes = canonicalBytes(output)
    const outputHash = digest(outputBytes)
    const ledger = await readLedger(ledgerPath)
    const previous = ledger[ledgerKey]
    let status = 'committed'
    let sideEffectWritten = false
    if (previous !== undefined) {
      let current
      try {
        await assertNoLinkComponents(outputPath)
        current = await readFile(outputPath)
      } catch (error) {
        if (error instanceof AgentProjectError) throw error
        current = undefined
      }
      if (previous.outputHash !== outputHash || current === undefined || digest(current) !== outputHash) {
        throw new AgentProjectError('IDEMPOTENCY_CONFLICT', 'ledger and business output no longer match', 'Inspect the existing output and ledger; do not overwrite either automatically.')
      }
      status = 'replayed'
    } else {
      try {
        await assertNoLinkComponents(outputPath)
        await readFile(outputPath)
        throw new AgentProjectError('IDEMPOTENCY_CONFLICT', 'an untracked output already exists', 'Inspect and reconcile the existing output before retrying.')
      } catch (error) {
        if (error instanceof AgentProjectError) throw error
        if (!(error && typeof error === 'object' && error.code === 'ENOENT')) throw error
      }
      ledger[ledgerKey] = {
        outcomeHash: planned.outcomeHash,
        outputHash,
        output: `output/${outputName}`,
      }
      const ledgerBytes = canonicalBytes(ledger)
      if (Object.keys(ledger).length > MAX_LEDGER_ENTRIES || ledgerBytes.length > MAX_LEDGER_BYTES) {
        delete ledger[ledgerKey]
        throw new AgentProjectError('LEDGER_LIMIT', 'idempotency ledger reached its bounded storage limit', 'Archive the current work root and start a reviewed new ledger; do not overwrite prior audit entries.')
      }
      await atomicWrite(outputPath, outputBytes)
      try {
        await atomicWrite(ledgerPath, ledgerBytes)
      } catch (error) {
        const quarantineRoot = assertInside(workRoot, join(workRoot, 'state', 'quarantine'))
        await secureMkdir(quarantineRoot)
        const quarantinePath = assertInside(quarantineRoot, join(quarantineRoot, `${outputName}.${outputHash}`))
        await rename(outputPath, quarantinePath)
        throw new AgentProjectError('LEDGER_WRITE_FAILED', 'ledger update failed after output staging', `The uncommitted output was quarantined as ${quarantinePath}; repair ledger storage before retrying.`)
      }
      sideEffectWritten = true
    }
    const receipt = {
      schema: 'agent-workbench-run/v3',
      status,
      taskId: task.task_id,
      scenarioId: task.scenario_id,
      capabilityId,
      sideEffectWritten,
      outcomeHash: planned.outcomeHash,
      output: `output/${outputName}`,
    }
    await writeReceiptOnce(receiptPath, canonicalBytes(receipt))
    return receipt
  } finally {
    if (releaseLedgerTransaction !== undefined) await releaseLedgerTransaction()
    await releaseReservation()
  }
  } finally {
    await releaseReceiptReservation()
  }
}
