# Independent iOS inspection replay

These files are owner-local offline inputs, not a device configuration or a new RF
result. They preserve the successful `8ac69242dbec4102b4c57f72131da2e9` inspect-only
capture described in the [radio report](../../../../../docs/testing/reports/2026-10-17-rn-ios-radio-control-feasibility.md).

- `inspection-note.txt`, `toggle-tree.txt`, `summary.json` and `tests.json` are
  unchanged UTF-8 bytes exported from that capture.
- `manifest.json` is an explicit two-attachment excerpt. Its selected note/tree
  metadata is unchanged; screenshots, context and video are deliberately omitted.
- `provenance.json` records the original manifest hash, selected file hashes,
  exported filenames and the original staging/selector contract.

The replay test uses these external observations rather than generating its
successful wire values from Python's `METHOD`, `NOTE` or attachment-prefix
constants. Temporary App/result directories and SDK calls remain synthetic.
Passing this replay proves the parser can still consume the recorded observation,
not that automation, radio recovery or a phone lease currently passes.

Do not regenerate these bytes from the implementation when a test fails. A new
capture needs its own provenance and an explicit review of the contract change.
