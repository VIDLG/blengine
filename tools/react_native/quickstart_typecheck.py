"""Compile the quickstart example's type layer against the package source it demonstrates.

The example ships JavaScript only, on purpose: a vendored native template rots with React Native
and forks away from a real App. What it *does* own is the shape of its use of the package, and the
type layer is where that shape lives — a wrong API use is a compile error, not a runtime surprise.

The package itself ships source, so the check has to compile the example the way a consumer does,
against `integrations/react-native-bleng/src`. It reuses the formal host's installed toolchain
instead of installing a second copy, and it fails closed when that toolchain is absent: Node
installation stays an explicit prerequisite, never work hidden in an ordinary check. Nothing is
written into the example directory; the generated project lives under `.work/`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .host_paths import PROJECT_ROOT

EXAMPLE = PROJECT_ROOT / "examples" / "react-native-bleng-quickstart"
HOST_NODE_MODULES = (
    PROJECT_ROOT / "tests" / "consumers" / "react-native-bleng" / "node_modules"
)
PACKAGE_SOURCE = (
    PROJECT_ROOT / "integrations" / "react-native-bleng" / "src" / "index.ts"
)
OUTPUT = PROJECT_ROOT / ".work" / "quickstart-typecheck"


def relative(target: Path, base: Path) -> str:
    """`target` as a path from `base`, the way TypeScript reads a config's relative entries."""
    return Path(os.path.relpath(target, base)).as_posix()


# Relative to the generated project's own directory: without `baseUrl` (deprecated in TypeScript 6)
# a `paths` entry resolves from the config that declares it. Only the three resolutions a consumer
# supplies are mapped; everything else resolves the ordinary way.
RESOLUTIONS = {
    "@blengine/react-native-bleng": PACKAGE_SOURCE,
    "react": HOST_NODE_MODULES / "@types/react/index.d.ts",
    "react-native": HOST_NODE_MODULES / "react-native/types/index.d.ts",
}


def project(base: Path) -> dict[str, object]:
    """The generated project: the example's own compiler options plus the host's resolutions."""
    return {
        "extends": relative(EXAMPLE / "tsconfig.json", base),
        "compilerOptions": {
            "paths": {
                name: [relative(path, base)] for name, path in RESOLUTIONS.items()
            },
            "typeRoots": [relative(HOST_NODE_MODULES / "@types", base)],
        },
        "include": [relative(EXAMPLE / "src", base) + "/**/*.tsx"],
    }


def compile_example() -> int:
    """Return tsc's exit code after compiling the example, or raise for a missing prerequisite."""
    typescript = HOST_NODE_MODULES / "typescript" / "bin" / "tsc"
    if not typescript.is_file():
        raise RuntimeError(
            f"{relative(typescript, PROJECT_ROOT)} is missing: install the formal host's Node "
            "dependencies first (see tests/consumers/react-native-bleng/README.md)"
        )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest = OUTPUT / "tsconfig.json"
    manifest.write_text(json.dumps(project(OUTPUT), indent=2) + "\n", encoding="utf-8")
    completed = subprocess.run(
        ["node", str(typescript), "--noEmit", "-p", str(manifest)],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    arguments = parser.parse_args(argv)
    try:
        exit_code = compile_example()
    except RuntimeError as error:
        print(f"quickstart-typecheck: {error}", file=sys.stderr)
        return 2
    if exit_code != 0:
        print(
            "quickstart-typecheck: the example does not compile against the package",
            file=sys.stderr,
        )
        return 1
    if not arguments.quiet:
        print(f"quickstart-typecheck: passed ({EXAMPLE.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
