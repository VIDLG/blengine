# Historical fixture contracts — evidence only

The v1 JSON files here and their profiles in the parent directory remain at their original report-linked paths as the unique historical evidence copies. They do not authorize live runs, firmware provisioning, or v1 wire compatibility. Historical reports are unchanged.

Maintained RF gates select the `multifunction-rf-v2-{a,b}.json` contracts under `tests/peripherals/nrf52840/firmware/contracts/` and the matching profiles under `firmware/profiles/`. A/B services are `fd600000-0000-4000-8000-000000000000` / `fd700000-0000-4000-8000-000000000000`. A build/exported contract is not proof of installation: ordinary runs still require live attestation under the physical lease and never flash/reset automatically.
