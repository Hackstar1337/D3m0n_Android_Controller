import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.dirname(fileURLToPath(import.meta.url))
const require = createRequire(import.meta.url)
const ts = require('typescript')
const source = await fs.readFile(path.join(root, 'src/composables/agentConsole.ts'), 'utf8')
const transpiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, strict: true },
  fileName: 'agentConsole.ts',
}).outputText
const moduleUrl = `data:text/javascript;base64,${Buffer.from(transpiled).toString('base64')}`
const {
  DEFAULT_AGENT_MODEL,
  DEFAULT_AGENT_PROVIDER,
  clampAgentOptions,
  createAgentSseParser,
  formatPullProgress,
  pollAgentPull,
  resolveAgentSelection,
} = await import(moduleUrl)

const providers = [
  { id: 'anthropic', label: 'Claude API', models: ['claude-sonnet'] },
  { id: DEFAULT_AGENT_PROVIDER, label: 'Ollama', models: [DEFAULT_AGENT_MODEL, 'qwen2.5:7b'] },
]
assert.deepEqual(resolveAgentSelection(providers, 'missing', 'missing'), {
  provider: DEFAULT_AGENT_PROVIDER, model: DEFAULT_AGENT_MODEL,
})
assert.deepEqual(resolveAgentSelection(providers, 'anthropic', 'claude-sonnet'), {
  provider: 'anthropic', model: 'claude-sonnet',
})
assert.deepEqual(clampAgentOptions({ contextLength: 999999, maxOutputTokens: -4, maxTurns: 99 }), {
  contextLength: 32768, maxOutputTokens: 128, maxTurns: 24,
})
assert.equal(formatPullProgress({ status: 'pulling', percent: 37.4 }), '37%')
assert.equal(formatPullProgress({ status: 'done' }), 'Downloaded')

const calls = []
const notifications = []
const responses = [
  { status: 'pulling', model: 'qwen3.5:4b', completed: 25, total: 100 },
  { status: 'done', model: 'qwen3.5:4b' },
]
const pull = await pollAgentPull(async (url) => {
  calls.push(url)
  return { json: async () => responses.shift() }
}, 'qwen3.5:4b', { intervalMs: 0, maxAttempts: 3, onProgress: (state) => notifications.push(state.status) })
assert.equal(pull.status, 'done')
assert.equal(calls.length, 2)
assert.match(calls[0], /pull\/qwen3\.5%3A4b$/)
assert.deepEqual(notifications, ['pulling', 'done'])
await assert.rejects(pollAgentPull(async () => ({ ok: false, status: 401 }), 'model', { intervalMs: 0, maxAttempts: 1 }), /401/)
const aborted = new AbortController(); aborted.abort()
await assert.rejects(pollAgentPull(async () => { throw new Error('must not fetch') }, 'model', { signal: aborted.signal }), /canceled/)

const parser = createAgentSseParser()
assert.deepEqual(parser.push('data: {"type":"text","content":"hel'), [])
assert.deepEqual(parser.push('lo"}\n\ndata: {"type":"stopped","content":"Stop'), [
  { type: 'text', content: 'hello' },
])
assert.deepEqual(parser.push('ped"}\n\nmalformed\n\n'), [
  { type: 'stopped', content: 'Stopped' },
])
assert.deepEqual(parser.push('data: {"type":"done"}'), [])
assert.deepEqual(parser.flush(), [{ type: 'done' }])

console.log('agent-console helper mocked regression tests: PASS')
