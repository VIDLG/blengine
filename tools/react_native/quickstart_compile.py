"""Compile the quickstart example with its own Babel config, and prove React Compiler ran.

The example ships JavaScript only, so nothing bundles it: a `babel.config.js` there would otherwise
be a file that is never executed. This gate executes it — `quickstart_compile.mjs` runs Babel over
every source with the example's own presets and plugins — and rejects the two ways such a config can
be decorative rather than effective:

- sources that no longer compile (a wrong API use Babel rejects before Metro would);
- components the React Compiler silently skips, so the memoization the example demonstrates is not
  actually applied.

Resolution happens from the formal host, which owns the repo's single installed Node toolchain; the
example itself is only read. Like `quickstart_typecheck`, this fails closed when that toolchain (or
the compiler plugin) is absent, because installing Node dependencies stays an explicit prerequisite
rather than work hidden inside an ordinary check.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from .host_paths import PROJECT_ROOT

EXAMPLE = PROJECT_ROOT / "examples" / "react-native-bleng-quickstart"
HOST = PROJECT_ROOT / "tests" / "consumers" / "react-native-bleng"
HOST_NODE_MODULES = HOST / "node_modules"
DRIVER = PROJECT_ROOT / "tools" / "react_native" / "quickstart_compile.mjs"
PREREQUISITES = (
    "@babel/core",
    "babel-plugin-react-compiler",
    "@react-native/babel-preset",
)


def missing_prerequisites() -> list[str]:
    """The host packages this gate needs, named the way an install instruction would name them."""
    return [
        package
        for package in PREREQUISITES
        if not (HOST_NODE_MODULES / package / "package.json").is_file()
    ]


def compile_example(quiet: bool = False) -> int:
    """Return the driver's exit code, or raise for a missing prerequisite."""
    missing = missing_prerequisites()
    if missing:
        raise RuntimeError(
            f"{', '.join(missing)} missing from {HOST.relative_to(PROJECT_ROOT)}/node_modules: "
            "install the formal host's Node dependencies first "
            "(see tests/consumers/react-native-bleng/README.md)"
        )
    command = ["node", str(DRIVER), str(EXAMPLE)]
    if quiet:
        command.append("--quiet")
    completed = subprocess.run(command, cwd=HOST, check=False)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    arguments = parser.parse_args(argv)
    try:
        exit_code = compile_example(quiet=arguments.quiet)
    except RuntimeError as error:
        print(f"quickstart-compile: {error}", file=sys.stderr)
        return 2
    if exit_code != 0:
        print(
            "quickstart-compile: the example does not compile or the compiler skipped a component",
            file=sys.stderr,
        )
        return 1
    if not arguments.quiet:
        print(f"quickstart-compile: passed ({EXAMPLE.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
