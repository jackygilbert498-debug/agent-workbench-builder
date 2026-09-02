// Keep module resolution anchored to the external DSH checkout, then hand the
// product workspace to DSH before its CLI reads process.cwd().
const entry = process.env.AGENT_WORKBENCH_DSH_ENTRY
const project = process.env.AGENT_WORKBENCH_PROJECT_ROOT
if (!entry || !project) throw new Error('external DSH bootstrap environment is incomplete')
process.chdir(project)
process.stdin.setEncoding('utf8')
let buffered = ''
process.stdin.on('data', chunk => {
  buffered += chunk
  const lines = buffered.split(/\r?\n/u)
  buffered = lines.pop() ?? ''
  if (lines.includes('__AGENT_WORKBENCH_STOP__')) process.emit('SIGTERM')
})
process.stdin.resume()
await import(entry)
