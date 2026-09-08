import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const candidates = process.env.MMS_BUILD_PYTHON
  ? [process.env.MMS_BUILD_PYTHON]
  : [resolve(root, '.venv/Scripts/python.exe'), resolve(root, '.venv/bin/python'), 'python3', 'python']
for (const python of candidates) {
  if (python.includes('/') || python.includes('\\')) {
    if (!existsSync(python)) continue
  }
  const probe = spawnSync(python, ['-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'], { stdio: 'ignore' })
  if (probe.error || probe.status !== 0) continue
  const result = spawnSync(python, [resolve(root, 'scripts/build_web.py'), 'build', '--node', process.execPath], { stdio: 'inherit', cwd: root })
  if (result.error) console.error(result.error.message)
  process.exit(result.status ?? 2)
}
console.error('A project Python interpreter is required. Run scripts/setup_web, or set MMS_BUILD_PYTHON.')
process.exit(2)
