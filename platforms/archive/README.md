# Archived platform backends

`platforms/archive/` contains platform implementations that are retained for historical review or explicit compatibility reproduction. They are not Cargo workspace members, default test/lint inputs, production package inputs, or maintained release targets.

The archived `windows/` backend is retained as the Phase 2 reference implementation and as evidence for the historical Windows contract/runtime work. Maintained product backends are Apple and Android; their public contract remains owned by `crates/blehub`.
