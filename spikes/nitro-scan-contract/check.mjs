import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, symlinkSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Deliberately not a product build, package install, native runtime test or maintained API gate.
const source = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(source, '../..')
const integration = path.join(root, 'integrations/react-native-bleng')
const modules = path.join(integration, 'node_modules')
const manifest = JSON.parse(readFileSync(path.join(integration, 'package.json'), 'utf8'))
for (const dependency of ['nitrogen', 'react-native-nitro-modules', 'typescript']) {
  const installed = JSON.parse(readFileSync(path.join(modules, dependency, 'package.json'), 'utf8'))
  assert.equal(installed.version, manifest.devDependencies[dependency], `${dependency} is not the pinned version`)
}

const work = path.join(root, '.work/nitro-scan-contract')
mkdirSync(work, { recursive: true })
const attempt = mkdtempSync(path.join(work, 'attempt-'))
console.log(`scan_contract_artifacts=${attempt}`)
symlinkSync(modules, path.join(attempt, 'node_modules'), 'dir')
for (const folder of ['valid', 'negative']) mkdirSync(path.join(attempt, folder))
copyFileSync(path.join(source, 'ScanCandidate.nitro.ts'), path.join(attempt, 'valid/ScanCandidate.nitro.ts'))
writeFileSync(path.join(attempt, 'nitro.json'), JSON.stringify({
  cxxNamespace: ['blengscanproposal'],
  ios: { iosModuleName: 'BlengScanProposal' },
  android: { androidNamespace: ['blengscanproposal'], androidCxxLibName: 'BlengScanProposal' },
  autolinking: {},
  ignorePaths: ['**/node_modules'],
  gitAttributesGeneratedFlag: false,
}, null, 2))
writeFileSync(path.join(attempt, 'negative/LiteralTag.nitro.ts'), `
import type { HybridObject } from 'react-native-nitro-modules'
interface Advertisement { kind: 'advertisement'; value: number }
export interface LiteralTag extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  nextEvent(): Promise<Advertisement>
}
`)
writeFileSync(path.join(attempt, 'tsconfig.json'), JSON.stringify({
  compilerOptions: {
    strict: true, noEmit: true, skipLibCheck: true, types: [],
    target: 'ES2022', module: 'ESNext', moduleResolution: 'Bundler',
  },
  include: ['valid/**/*.ts', 'negative/**/*.ts'],
}))

function run(name, script, args) {
  const result = spawnSync(process.execPath, [path.join(modules, script), ...args], {
    cwd: attempt, encoding: 'utf8', timeout: 45_000, maxBuffer: 4 * 1024 * 1024,
  })
  const log = `${result.stdout ?? ''}${result.stderr ?? ''}`
  writeFileSync(path.join(attempt, `${name}.log`), log)
  if (result.error) throw result.error
  return { ...result, log }
}

const checked = run('typecheck', 'typescript/bin/tsc', ['--project', 'tsconfig.json'])
assert.equal(checked.status, 0, checked.log)
const positive = run('positive', 'nitrogen/lib/index.js', ['valid', '--out', 'generated-positive'])
assert.equal(positive.status, 0, positive.log)
assert.match(positive.log, /Generated 4\/4 HybridObjects/, 'zero/partial generation is not success')
const generated = path.join(attempt, 'generated-positive')
const files = readdirSync(generated, { recursive: true }).map(String)
for (const [suffix, required] of [
  ['ios/swift/HybridScanSessionCandidateSpec.swift', ['func waitUntilStarted(', 'func nextEvent(', 'func beginStop(', 'func cancelStart(']],
  ['HybridScanSessionCandidateSpec.kt', ['fun waitUntilStarted(', 'fun nextEvent(', 'fun beginStop(', 'fun cancelStart(']],
  ['ios/swift/HybridScanStopCandidateSpec.swift', ['func wait(', 'func cancel(']],
  ['HybridScanStopCandidateSpec.kt', ['fun wait(', 'fun cancel(']],
  ['ScanNextCandidate.hpp', ['std::variant', 'ScanAdvertisementEvent', 'ScanEndedEvent', 'ScanFailure']],
  ['ScanAdvertisement.hpp', ['ScanPeripheralCandidate', 'ScanManufacturerData', 'ScanServiceData']],
  ['ScanManufacturerData.hpp', ['ArrayBuffer']],
]) {
  const file = files.find(file => file.endsWith(suffix))
  assert.ok(file, `missing generated ${suffix}`)
  const text = readFileSync(path.join(generated, file), 'utf8')
  for (const fragment of required) assert.ok(text.includes(fragment), `${file} lacks ${fragment}`)
}
const negative = run('negative', 'nitrogen/lib/index.js', ['negative', '--out', 'generated-negative'])
assert.notEqual(negative.status, 0, 'singleton literal must not be silently accepted')
assert.match(negative.log, /String literal.*cannot be represented/, negative.log)
console.log(`scan_contract_codegen=passed nitrogen=${manifest.devDependencies.nitrogen} hybrids=4`)
console.log(`scan_contract_literal_negative=passed`)
console.log('scope=typescript-and-codegen-only; no native compile, runtime, BLE or RF evidence')
