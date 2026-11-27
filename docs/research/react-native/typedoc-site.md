# TypeDoc site: a generated second view, never a second truth

The package ships two hand-written references — `API.md` (English) and `API.zh.md` — pinned by
`tools/tests/react_native/test_api_docs.py`. They explain **behaviour**: the three shapes, deadlines,
receipts, refusals and what each failure is allowed to promise.

The TypeDoc site is a different artifact with a different owner: **TypeScript owns it**. It is
generated from `integrations/react-native-bleng/src/index.ts` (the package's public entry point, which
re-exports every Nitro spec) and answers only "what exactly does this declaration say" — signatures,
union arms, optional fields, narrowed results. No prose is authored for it, so it cannot drift from
the specs, and it must never be edited to describe behaviour.

## Commands

```bash
pixi run just react-native::api-site        # write integrations/react-native-bleng/site/api
pixi run just react-native::api-site-check  # validate the same entry point, write nothing
```

`api-site-check` runs TypeDoc with `--emit none --treatWarningsAsErrors`, so a broken link, an
unresolvable type or a spec that TypeDoc cannot parse fails the check rather than silently producing a
partial site.

## Rules that keep it derived

- **One entry point.** `src/index.ts` is the only source of truth for what is public. Adding a page
  means adding an export, not a config entry; `excludeExternals` keeps Nitro's inherited `HybridObject`
  members off every capability page.
- **The output directory is generated.** `integrations/react-native-bleng/site/` is gitignored; nothing
  is reviewed or committed from it. Regenerate instead of patching HTML.
- **The landing page is a pointer, not a manual.** `typedoc-readme.md` says where behaviour lives and
  names both API files, so a reader who lands on the site first cannot mistake it for the contract.

## CI and hosting

Two workflows exist; neither commits a build artifact.

| workflow | trigger | does |
|---|---|---|
| `.github/workflows/docs-checks.yml` | PR / push to `main`, docs paths | API reference tests, site-ownership tests, Mermaid syntax, `docs:api:check` |
| `.github/workflows/docs-pages.yml` | push to `main` (API paths) or `workflow_dispatch` | builds the site, uploads the Pages artifact, deploys it |

**Hosting is provisioned and entitled.** Pages is enabled for this repository with "GitHub Actions"
as the source, and the repository is public, so the deploy job can publish. The site lives at
`https://vidlg.github.io/blengine/` (a project page, not an organization root site), and TypeDoc's
relative asset paths already work under that subpath.

The `push` trigger fires on `main` whenever the API surface or its inputs change;
`workflow_dispatch` stays available for a manual republish. `docs-checks.yml` remains the gate — the
deploy workflow only builds inputs the check can validate too.

Publishing does not freeze the ABI: the site tracks the `0.0.0` declarations and is regenerated on
every change, so it shows what the specs say today, not a released contract.

Do not “fix” a failed deploy by committing `site/api` into the repository. The site is regenerable; a
committed copy is the drift this whole design avoids.

## Not a replacement for `API.md`

TypeDoc renders declarations, and declarations are compatible with several different contracts. The
distinctions this package cares about — accepted versus delivered, cancel versus rollback, a timeout
belonging to the caller, a `not_supported` refusal instead of a silent fallback — exist only in
`API.md` / `API.zh.md`. Keep them there.
