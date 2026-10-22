import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const root = dirname(fileURLToPath(import.meta.url))
const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'))
const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'))
const failures = []
if (lock.lockfileVersion !== 3) failures.push('package-lock.json must use lockfileVersion 3')
if (!pkg.engines?.node) failures.push('package.json must declare a Node engine')
if (!pkg.scripts?.build || !pkg.scripts?.['test:portable']) failures.push('build and test:portable scripts are required')
if (!existsSync(join(root, 'index.html'))) failures.push('frontend/index.html is missing')
if (process.env.D3M0N_REQUIRE_BUILD === '1' && !existsSync(join(root, 'dist', 'index.html'))) failures.push('frontend/dist/index.html is missing; run npm run build')
if (failures.length) {
  console.error(failures.map((item) => `portable frontend check failed: ${item}`).join('\n'))
  process.exit(1)
}
console.log('portable frontend checks passed (no server, phone, provider, or network action)')
const suites = readdirSync(root).filter((name) => /^test_[a-z0-9_]+\.mjs$/.test(name)).sort()
if (!suites.length) throw new Error('No frontend regression suites found')
for (const suite of suites) {
  console.log(`\n--- ${suite} ---`)
  const result = spawnSync(process.execPath, [join(root, suite)], { cwd: root, stdio: 'inherit' })
  if (result.error || result.status !== 0) process.exit(result.status || 1)
}
console.log(`\n${suites.length}/${suites.length} frontend regression suites passed`)
