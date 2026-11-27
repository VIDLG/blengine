"""Render the human-readable ABI freeze inventory from the reviewed snapshots.

The nine `api-snapshots/*.txt` files are the *machine* form of what a freeze promises: any drift
fails `just binding snapshot-check` until someone reviews it. A person signing a freeze needs the
same content in a form they can read, plus the two things the snapshots do not carry: the JS/Nitro
surface and the failure `code` space.

This script never decides anything and never writes outside `docs/research/`. Its output is
regenerated and compared by `tools/tests/test_freeze_inventory.py`, so the document cannot drift
from the reviewed snapshots it summarizes.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = PROJECT_ROOT / "api-snapshots"
SPEC_ROOT = PROJECT_ROOT / "integrations" / "react-native-bleng" / "specs"
FAILURE_SOURCE = (
    PROJECT_ROOT / "crates" / "bleng-supervisor" / "src" / "ffi" / "failure.rs"
)
OUTPUT = PROJECT_ROOT / "docs" / "research" / "bleng" / "abi-freeze-inventory.md"

# One component per line, three generated languages each.
COMPONENTS = (
    ("blehub", "BleHub canonical core"),
    ("bleng", "BLeng async runtime"),
    ("supervisor", "BLeng supervisor (FFI owner)"),
)
LANGUAGES = ("rust", "kotlin", "swift")

_DECLARATIONS = (
    re.compile(r"^pub (?:struct|enum|trait) (\w+)"),
    re.compile(r"^(?:class|sealed class|object|interface|enum class) (\w+)"),
    re.compile(r"^(?:class|enum|struct|protocol) (\w+)"),
)
_JS_INTERFACE = re.compile(r"^export interface (\w+)")
_JS_ALIAS = re.compile(r"^export type (\w+)")
_JS_METHOD = re.compile(r"^  (\w+)\(([^)]*)\): (.+)$")
_CODE = re.compile(r'"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)"')
_FUNCTION = re.compile(r"^(?:pub(?:\(super\))? )?fn (\w+)")

# A `code` only reaches JS through one of these three compositions. Listing the literals without
# the function that produces them would be ambiguous: `backend_assembly` is only ever seen as
# `bleng.assembly.backend_assembly`.
# The three path prefixes the delegated mappers prepend. They appear as literals in the main
# match, but they are prefixes, not codes, so they are reported separately.
FAILURE_PATHS = ("bleng.operation", "bleng.assembly", "bleng.cleanup")


def snapshot_path(component: str, language: str) -> Path:
    """The reviewed snapshot for one component in one generated language.

    The BleHub component keeps the historical bare names (`rust.txt`); the two later components
    are prefixed, which is what tells them apart in one flat directory.
    """
    name = f"{language}.txt" if component == "blehub" else f"{component}-{language}.txt"
    return SNAPSHOT_ROOT / name


def declarations(path: Path) -> list[str]:
    """Top-level type names one snapshot declares, in the snapshot's own order."""
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        for pattern in _DECLARATIONS:
            found = pattern.match(line)
            if found is not None:
                names.append(found.group(1))
                break
    return names


def nitro_surface() -> list[
    tuple[str, list[tuple[str, list[tuple[str, str, str]]]], list[str]]
]:
    """Every Nitro spec: interfaces with their methods, and the union aliases beside them."""
    specs = []
    for path in sorted(SPEC_ROOT.glob("*.nitro.ts")):
        interfaces: list[tuple[str, list[tuple[str, str, str]]]] = []
        aliases: list[str] = []
        current: str | None = None
        methods: list[tuple[str, str, str]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            interface = _JS_INTERFACE.match(line)
            if interface is not None:
                if current is not None:
                    interfaces.append((current, methods))
                current, methods = interface.group(1), []
                continue
            alias = _JS_ALIAS.match(line)
            if alias is not None:
                aliases.append(alias.group(1))
                continue
            method = _JS_METHOD.match(line)
            if method is not None and current is not None:
                methods.append((method.group(1), method.group(2), method.group(3)))
        if current is not None:
            interfaces.append((current, methods))
        specs.append((path.name.removesuffix(".nitro.ts"), interfaces, aliases))
    return specs


def _bodies_by_function() -> dict[str, list[str]]:
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    for line in FAILURE_SOURCE.read_text(encoding="utf-8").splitlines():
        found = _FUNCTION.match(line)
        if found is not None:
            current = found.group(1)
            bodies.setdefault(current, [])
            continue
        if current is not None:
            bodies[current].extend(_CODE.findall(line))
    return bodies


def failure_code_groups() -> list[tuple[str, str, list[str]]]:
    """The three wire shapes a `code` can take, each with its own literals.

    Grouping by shape rather than by function is what keeps `supervisor.*` honest: those
    literals are produced in several functions and are used verbatim, so they belong in one
    group regardless of where they are written.
    """
    bodies = _bodies_by_function()
    main = [
        code
        for function in ("bleng_failure", "dispatch_failure")
        for code in bodies.get(function, [])
        if code not in FAILURE_PATHS and not code.startswith("supervisor.")
    ]
    delegated = [
        code
        for function in ("ble_failure", "hub_failure")
        for code in bodies.get(function, [])
    ]
    verbatim = [
        code
        for codes in bodies.values()
        for code in codes
        if code.startswith("supervisor.")
    ]
    return [
        ("bleng.<code>", "BlengError / dispatch 映射", _unique(main)),
        (
            "<path>.<code>",
            "ble_failure / hub_failure，path ∈ "
            + "、".join(f"`{path}`" for path in FAILURE_PATHS),
            _unique(delegated),
        ),
        ("原样", "supervisor 自己产生，不经前缀组合", _unique(verbatim)),
    ]


def _unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def render() -> str:
    lines: list[str] = [
        "# ABI freeze inventory（人可读版）",
        "",
        "> 本文件由 `pixi run python -m tools.freeze_inventory` 生成，并由",
        "> `tools/tests/test_freeze_inventory.py` 断言与 `api-snapshots/` 一致。**不要手改。**",
        "",
        "它是冻结**清单**，不是冻结公告：ABI 未冻结，Phase 退出条件不由本文改变。九份 reviewed 快照是",
        "机械替身（任何漂移让 `snapshot-check` 失败），本文是给人签名看的同一份形状 + 快照里没有的两块",
        "（JS/Nitro 面与失败 `code` 空间）。决定本身见",
        "[`abi-freeze-review-packet.md`](abi-freeze-review-packet.md)。",
        "",
        "## 1. Rust Stable UniFFI 形状",
        "",
        "| component | rust | kotlin | swift |",
        "|---|---|---|---|",
    ]
    for name, label in COMPONENTS:
        cells = [
            f"{len(declarations(snapshot_path(name, language)))} 个声明"
            for language in LANGUAGES
        ]
        lines.append(f"| `{name}`（{label}） | {' | '.join(cells)} |")
    lines.append("")
    for name, label in COMPONENTS:
        lines += [f"### `{name}` 逐符号", ""]
        for language in LANGUAGES:
            names = declarations(snapshot_path(name, language))
            lines.append(
                f"- **{language}**（{len(names)}）："
                + "、".join(f"`{item}`" for item in names)
            )
        lines.append("")

    lines += ["## 2. JS / Nitro 面", ""]
    for spec, interfaces, aliases in nitro_surface():
        lines += [f"### `{spec}.nitro.ts`", ""]
        for interface, methods in interfaces:
            lines.append(f"- `{interface}`")
            for method, args, result in methods:
                lines.append(f"  - `{method}({args}): {result}`")
        if aliases:
            lines.append("- 联合/别名：" + "、".join(f"`{alias}`" for alias in aliases))
        lines.append("")
    lines += [
        "入口标识（D9）：包名 `@blehub/react-native-bleng`、`createBlengCentral()`、",
        "`getBlengCentral()`、`BlengProvider`、`BlengCentral`、`BleHubBlengEngineLifecycle`。",
        "D7 的派生面（同一 JS 面，冻结时一并算）：`pendingOperation`、`run`、`requireAccepted`、",
        (
            "`requireOutcome`、`AbortSignalLike`、`BlengOperationError`、`Operation`、`OperationSubmission`、"
            "`PendingOperation`、`isBlengFailure`、`shareLane`、`useBlengCentral`、`closeBlengCentral`。"
        ),
        "",
        "## 3. 失败 `code` 空间（D4：只允许追加）",
        "",
        "一个 `code` 只有在下列三种组合下才是 wire 字符串，所以按**形状**分组：同一形状内的",
        "字面量就是全部可选值。",
        "两端的 `BlengFailureMapping` 不做二次命名，桥级只额外产生一个 `bridgeFailure`。",
        "",
    ]
    for shape, label, literals in failure_code_groups():
        lines += [
            f"### `{shape}` — {label}（{len(literals)}）",
            "",
            "```text",
            *literals,
            "```",
            "",
        ]
    lines += [
        "新增 `code` 是允许的（追加）；重命名或删除需要解冻评审。",
        "",
        "## 4. 明确不冻结",
        "",
        "- marker 字符串、evidence JSON schema、fixture contract/profile、`tools/` 与 recipe、任何 `*_ledger`。",
        "- 场景计划与宿主动作（`scene_plans.py`）：运行期数据，不是 ABI。",
        "- native bundle id / package 名：留到真正对外发布时再核对（D9）。",
        "",
        "## 5. 签名",
        "",
        "| 项 | 决定 |",
        "|---|---|",
        "| D1 跨语言关闭命名 | 保持本地惯用名 |",
        "| D2 事件读取粒度 | 按能力 pull |",
        "| D3 deadline 表达 | 显式 `timeoutMs` |",
        "| D4 错误 taxonomy | 形状冻结，`code` 只追加 |",
        "| D5 可选字段语义 | 缺省即该分支 |",
        "| D6 平台名类型 | `string` |",
        "| D7 操作模型 | `begin*` / `wait` / `cancel` + 派生 await 层 |",
        "| D8 版本策略 | 开发阶段不承诺兼容 |",
        "| D9 公开包边界 | 冻结包名与入口 |",
        "",
        "签署后，任何改动本文件所列形状的 commit 都必须走解冻/评审，而不是 `snapshot-update`。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"freeze_inventory=written path={OUTPUT.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
