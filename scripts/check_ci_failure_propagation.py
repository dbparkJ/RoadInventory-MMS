"""Prove that each CI tool returns failure for an intentionally broken fixture.

Only synthetic files in a unique, temporary directory are used. No tracked source
is edited and neither a successful build nor a production dist is overwritten.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-only", action="store_true")
    args = parser.parse_args()
    root = PROJECT_ROOT
    cache = root / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ci-failure-probe-", dir=cache) as temporary:
        probe = Path(temporary)
        python_test = probe / "test_expected_failure.py"
        python_test.write_text("def test_expected_failure():\n    assert False, 'CI_EXPECTED_FAILURE'\n", encoding="utf-8")
        commands = [("pytest", [sys.executable, "-m", "pytest", "-q", str(python_test)], "CI_EXPECTED_FAILURE", 1)]
        if not args.python_only:
            node = shutil.which("node")
            if node is None:
                print("Node is required for frontend failure probes.", file=sys.stderr)
                return 2
            modules = root / "webui" / "node_modules"
            type_input = probe / "invalid.ts"
            type_input.write_text("const invalid: number = 'CI_EXPECTED_FAILURE';\n", encoding="utf-8")
            frontend_test = probe / "expected.test.ts"
            # Resolving Vitest from its absolute file avoids depending on a
            # temporary directory being inside webui/node_modules resolution.
            frontend_test.write_text(
                f"import {{ test, expect }} from {str((modules / 'vitest' / 'dist' / 'index.js').as_uri())!r};\n"
                "test('CI_EXPECTED_FAILURE', () => expect(1).toBe(2));\n", encoding="utf-8",
            )
            vitest_config = probe / "vitest.config.mjs"
            vitest_config.write_text(
                "export default { test: { environment: 'node', include: ['expected.test.ts'] } };\n", encoding="utf-8",
            )
            vite_config = probe / "vite.config.mjs"
            vite_config.write_text("throw new Error('CI_EXPECTED_BUILD_FAILURE');\n", encoding="utf-8")
            commands.extend([
                ("typescript", [node, str(modules / "typescript/bin/tsc"), "--noEmit", "--skipLibCheck", str(type_input)], "TS2322", 2),
                ("vitest", [node, str(modules / "vitest/vitest.mjs"), "run", "--root", str(probe), "--config", str(vitest_config), "--maxWorkers=1"], "CI_EXPECTED_FAILURE", 1),
                ("vite", [node, str(modules / "vite/bin/vite.js"), "build", "--config", str(vite_config), "--outDir", str(probe / "dist")], "CI_EXPECTED_BUILD_FAILURE", 1),
            ])
        for name, command, marker, expected_exit in commands:
            result = subprocess.run(command, cwd=root, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, check=False)
            if result.returncode != expected_exit or marker not in result.stdout:
                print(f"{name}: failure probe did not produce its expected failure (exit={result.returncode}).", file=sys.stderr)
                print(result.stdout, file=sys.stderr)
                return 1
            print(f"{name}: expected fixture failure propagated (exit={expected_exit}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
