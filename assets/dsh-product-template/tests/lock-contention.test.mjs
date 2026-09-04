/** Deterministic Windows sharing-error cases; only the OS error is injected.
 * The production workflow, local files, ledger and output are exercised as-is.
 * Node isolates this test file from other files; every builtin patch is restored.
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { syncBuiltinESMExports } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { CAPABILITIES, SCENARIOS } from '../src/project.mjs'
import { commitCapability } from '../src/workflow.mjs'

const capability = CAPABILITIES.find(item => item.risk === 'approval-required')
const scenario = SCENARIOS.find(item => item.capabilityIds.includes(capability.id))
const input = { task_id: 'lock-check', scenario_id: scenario.id, content: 'A local review draft.' }
const permissionError = () => Object.assign(new Error('injected Windows sharing error'), { code: 'EPERM' })

async function withOpenPatch(replacement, run) {
  const root = await fs.promises.mkdtemp(join(tmpdir(), 'awb-lock-case-'))
  const originalOpen = fs.promises.open
  const originalNow = Date.now
  try {
    fs.promises.open = (...args) => replacement(originalOpen, ...args)
    syncBuiltinESMExports()
    await run(root)
  } finally {
    fs.promises.open = originalOpen
    Date.now = originalNow
    syncBuiltinESMExports()
    await fs.promises.rm(root, { recursive: true, force: true })
  }
}

function commit(root) {
  return commitCapability(capability.id, input, { approved: true, runId: 'lock-check', workRoot: root })
}

const windowsOnly = { skip: process.platform !== 'win32' }

test('Windows sharing error retries a regular lock without stealing its contents', windowsOnly, async () => {
  let lockPath, ownerBytes, attempts = 0
  await withOpenPatch(async (open, path, flags, mode) => {
    if (flags === 'wx' && String(path).endsWith('.lock') && attempts++ === 0) {
      lockPath = path
      ownerBytes = Buffer.from('{"token":"other-worker"}')
      await fs.promises.writeFile(path, ownerBytes)
      throw permissionError()
    }
    if (path === lockPath && attempts === 2) {
      assert.deepEqual(await fs.promises.readFile(path), ownerBytes)
      // The owning worker releases its lock before the next exclusive open.
      await fs.promises.unlink(path)
    }
    return open(path, flags, mode)
  }, async root => {
    const result = await commit(root)
    assert.equal(result.status, 'committed')
    assert.equal((await fs.promises.readdir(join(root, 'output'))).length, 1)
  })
})

test('Windows sharing error tolerates a lock released before its inspection', windowsOnly, async () => {
  let injected = false
  await withOpenPatch(async (open, path, flags, mode) => {
    if (!injected && flags === 'wx' && String(path).endsWith('.lock')) {
      injected = true
      throw permissionError()
    }
    return open(path, flags, mode)
  }, async root => assert.equal((await commit(root)).status, 'committed'))
})

test('a directory cannot be mistaken for a retryable regular lock', windowsOnly, async () => {
  let attempts = 0
  await withOpenPatch(async (open, path, flags, mode) => {
    if (flags === 'wx' && String(path).endsWith('.lock')) {
      attempts++
      await fs.promises.mkdir(path)
      throw permissionError()
    }
    return open(path, flags, mode)
  }, async root => {
    await assert.rejects(commit(root), error => error.code === 'EPERM')
    assert.equal(attempts, 1)
    assert.deepEqual(await fs.promises.readdir(join(root, 'output')).catch(() => []), [])
  })
})

test('persistent Windows permission failure is bounded and has no business output', windowsOnly, async () => {
  let attempts = 0, time = 0
  await withOpenPatch(async (open, path, flags, mode) => {
    if (flags === 'wx' && String(path).endsWith('.lock')) {
      attempts++
      throw permissionError()
    }
    return open(path, flags, mode)
  }, async root => {
    Date.now = () => (time += 5000)
    await assert.rejects(commit(root), error => error.code === 'LOCK_UNAVAILABLE')
    assert(attempts >= 1 && attempts <= 3)
    assert.deepEqual(await fs.promises.readdir(join(root, 'output')).catch(() => []), [])
  })
})

test('a Windows junction cannot be mistaken for a retryable lock', windowsOnly, async () => {
  let target, attempts = 0
  await withOpenPatch(async (open, path, flags, mode) => {
    if (flags === 'wx' && String(path).endsWith('.lock')) {
      attempts++
      await fs.promises.symlink(target, path, 'junction')
      throw permissionError()
    }
    return open(path, flags, mode)
  }, async root => {
    target = join(root, 'unrelated-target')
    await fs.promises.mkdir(target)
    await assert.rejects(commit(root), error => error.code === 'EPERM')
    assert.equal(attempts, 1)
    assert.deepEqual(await fs.promises.readdir(target), [])
  })
})

test('a lock owner-record write failure is not retried as lock contention', async () => {
  let attempts = 0, closed = false
  await withOpenPatch(async (open, path, flags, mode) => {
    const handle = await open(path, flags, mode)
    if (flags !== 'wx' || !String(path).endsWith('.lock')) return handle
    attempts++
    return {
      writeFile: async () => { throw permissionError() },
      close: async () => { closed = true; await handle.close() },
    }
  }, async root => {
    await assert.rejects(commit(root), error => error.code === 'EPERM')
    assert.equal(attempts, 1)
    assert.equal(closed, true)
    assert.deepEqual(await fs.promises.readdir(join(root, 'output')).catch(() => []), [])
  })
})

test('a lock handle close failure is not retried as lock contention', async () => {
  let attempts = 0
  await withOpenPatch(async (open, path, flags, mode) => {
    const handle = await open(path, flags, mode)
    if (flags !== 'wx' || !String(path).endsWith('.lock')) return handle
    attempts++
    return {
      writeFile: value => handle.writeFile(value),
      close: async () => { await handle.close(); throw permissionError() },
    }
  }, async root => {
    await assert.rejects(commit(root), error => error.code === 'EPERM')
    assert.equal(attempts, 1)
    assert.deepEqual(await fs.promises.readdir(join(root, 'output')).catch(() => []), [])
  })
})
