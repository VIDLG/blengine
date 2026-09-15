# nRF AutoPTS boundary spike

This spike validates the process boundary for an optional nRF-only AutoPTS
adapter. It owns no AutoPTS code and does not import `nrftest`.

It verifies that an external checkout and command can be run with:

- explicit working directory;
- a new process group;
- no implicit hardware reset or flashing;
- bounded and idempotent shutdown;
- terminal closed state after shutdown;
- readiness marker versus process-alive distinction;
- terminal failure marker;
- exclusive device/session lock.

Run from this directory:

```sh
pixi run python -m unittest -v test_autopts_boundary
```

This is not yet the active nRF provider. It does not start AutoPTS, open a
serial port, or access hardware. The next stage will add a real configuration
adapter and readiness protocol after the exact AutoPTS command/session contract
is selected.
