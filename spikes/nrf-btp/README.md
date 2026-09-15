# nRF BTP protocol spike

This is a hardware-free, isolated spike for the nRF52840 provider boundary. It
It validates the BTP frame format, incremental stream parsing, serialized
request/response/event dispatching, and bounded fake-transport session behavior
before any AutoPTS or serial integration.

The spike intentionally does **not**:

- import or invoke `nrftest`;
- copy AutoPTS source;
- open a serial port or socket;
- reset or flash hardware;
- serve as an active provider or default test dependency.

Run it from this directory with:

```sh
pixi run python -m unittest -v test_btp_spike test_btp_session_spike
```

The wire facts are compared against the locally pinned AutoPTS behavior and
`nrftest` documentation, but the spike implementation is independently owned.
If this boundary is promoted into `tests/peripherals/nrf52840`, the AutoPTS
license and integration decision must be recorded first.
