from __future__ import annotations

import argparse
import re
from pathlib import Path

ERROR_CLASS = re.compile(r"^sealed class \w+ : Exception\(\) \{")
MESSAGE_FIELD = re.compile(r"^(\s*)val message: String(,?)$")


def patch_file(path: Path) -> tuple[int, int]:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_error_class = False
    brace_depth = 0
    error_classes = 0
    replacements = 0
    patched: list[str] = []

    for line in lines:
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        if not in_error_class and ERROR_CLASS.match(content):
            in_error_class = True
            brace_depth = 0
            error_classes += 1

        if in_error_class:
            match = MESSAGE_FIELD.match(content)
            if match is not None:
                indent, comma = match.groups()
                content = f"{indent}override val message: String{comma}"
                replacements += 1
            brace_depth += content.count("{") - content.count("}")
            if brace_depth == 0:
                in_error_class = False

        patched.append(content + ending)

    if replacements > 0:
        path.write_text("".join(patched), encoding="utf-8", newline="")
    return error_classes, replacements


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply the temporary BoltFFI 0.30.1 Kotlin error-message override workaround."
        )
    )
    parser.add_argument("path", type=Path)
    args = parser.parse_args()

    error_classes, replacements = patch_file(args.path)
    if error_classes == 0:
        raise SystemExit(f"no generated Kotlin error classes found in {args.path}")
    if replacements == 0:
        print("BoltFFI Kotlin message override workaround not needed")
    else:
        print(
            "Applied temporary BoltFFI Kotlin message override workaround: "
            f"{replacements} field(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
