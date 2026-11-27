/**
 * Compile the quickstart example with its own `babel.config.js`, and prove React Compiler ran.
 *
 * The example ships no native template, so it has no Metro bundle to inspect: without this gate its
 * Babel config would be a file nobody executes. Two properties are checked, both derived from the
 * config itself rather than from a list kept here:
 *
 * 1. every source under `src/` compiles with the example's own presets and plugins;
 * 2. every `.tsx` contains at least one component the compiler actually transformed — the emitted
 *    `compiler-runtime` import is the compiler's own marker. A silent bail-out (the compiler
 *    skipping code it cannot lower) therefore fails the gate rather than passing as "configured".
 *
 * Module resolution is deliberately the caller's: the gate runs from a harness directory whose
 * `node_modules` is the formal host's, so presets, plugins and Babel itself resolve exactly the way
 * they would in an installed app. That root is `process.cwd()`; the example stays read-only.
 */
import { createRequire } from 'node:module'
import { readdirSync } from 'node:fs'
import path from 'node:path'

const COMPILER = 'babel-plugin-react-compiler'
const SOURCE = /\.(?:js|jsx|ts|tsx)$/
/** The compiler's own output marker; it appears only when a component was transformed. */
const MARKER = 'compiler-runtime'

const example = path.resolve(process.argv[2] ?? '.')
const quiet = process.argv.includes('--quiet')
const root = process.cwd()
const require = createRequire(path.join(root, 'index.js'))
const babel = require('@babel/core')

const configPath = path.join(example, 'babel.config.js')
const config = require(configPath)

// Loading the config as data rather than letting Babel resolve it is what allows a diagnostic
// logger to be attached. Anything this gate does not understand must fail loudly instead of being
// silently dropped from the transform.
const supported = new Set(['presets', 'plugins'])
const unsupported = Object.keys(config).filter(key => !supported.has(key))
if (unsupported.length > 0) {
  console.error(
    `quickstart-compile: ${rel(configPath)} declares ${unsupported.join(', ')}; this gate reads`
    + ' only presets and plugins, so extend it before using more config keys',
  )
  process.exit(2)
}

const outcomes = new Map()
const plugins = (config.plugins ?? []).map(entry => {
  const name = Array.isArray(entry) ? entry[0] : entry
  if (name !== COMPILER) return entry
  const options = Array.isArray(entry) ? (entry[1] ?? {}) : {}
  return [name, {
    ...options,
    logger: {
      logEvent: (filename, event) => {
        const outcome = outcomes.get(filename) ?? { compiled: 0, bailOuts: [] }
        if (event.kind === 'CompileSuccess') {
          outcome.compiled += 1
        } else {
          outcome.bailOuts.push(event.detail?.reason ?? event.kind)
        }
        outcomes.set(filename, outcome)
      },
    },
  }]
})
if (!plugins.some(entry => (Array.isArray(entry) ? entry[0] : entry) === COMPILER)) {
  console.error(
    `quickstart-compile: ${rel(configPath)} does not list ${COMPILER}, so the example would ship`
    + ' without the memoization it claims to demonstrate',
  )
  process.exit(1)
}

function rel(target) {
  return path.relative(example, target) || path.basename(target)
}

function sources(directory) {
  return readdirSync(directory, { withFileTypes: true })
    .flatMap(entry => {
      const full = path.join(directory, entry.name)
      if (entry.isDirectory()) return entry.name === 'node_modules' ? [] : sources(full)
      return SOURCE.test(entry.name) ? [full] : []
    })
    .sort()
}

const files = [path.join(example, 'index.js'), ...sources(path.join(example, 'src'))]
let failures = 0

for (const file of files) {
  let result
  try {
    result = babel.transformFileSync(file, {
      babelrc: false,
      configFile: false,
      cwd: root,
      root,
      filename: file,
      presets: config.presets ?? [],
      plugins,
    })
  } catch (error) {
    console.error(`quickstart-compile: ${rel(file)} does not compile:`)
    console.error(`  ${error.message.split('\n')[0]}`)
    failures += 1
    continue
  }
  if (!file.endsWith('.tsx')) continue
  if (result.code.includes(MARKER)) continue
  failures += 1
  const { bailOuts } = outcomes.get(file) ?? { bailOuts: [] }
  console.error(
    `quickstart-compile: ${rel(file)} compiled, but React Compiler skipped every component in it`
    + ` (no ${MARKER} emitted)`,
  )
  for (const reason of bailOuts) console.error(`  - ${reason}`)
  if (bailOuts.length === 0) console.error('  - no reason reported by the compiler')
}

if (failures > 0) process.exit(1)
const compiled = [...outcomes.values()].reduce((total, one) => total + one.compiled, 0)
if (!quiet) {
  console.log(`quickstart-compile: passed (${files.length} files, ${compiled} components compiled)`)
}
