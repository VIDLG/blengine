# `@blehub/react-native-bleng` — type reference

This site is **generated** from the package's Nitro specs and its public entry point by TypeDoc, so
every signature, union arm and field is exactly what the compiler sees. Nothing here is hand-edited,
and it is not a second source of truth.

Behaviour is documented by hand, on purpose, because contracts are not types:

- `API.md` — English: the model, the three shapes, deadlines, receipts and failure semantics.
- `API.zh.md` — the 中文 mirror of the same contract.

Use this site to answer "what exactly does this return", and `API.md` to answer "what does it mean,
and what is it allowed to promise". Regenerate with `pixi run just react-native::api-site`.
