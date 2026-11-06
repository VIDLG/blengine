import React, { useEffect, useRef, useState } from 'react';
import { Platform, Text, View } from 'react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { DispatchError, RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import {
  isCanonicalU64, RF_CONNECTION_TIMEOUT_MS, RF_INITIALIZATION_TIMEOUT_MS,
  RF_POST_DISCONNECT_OBSERVATION_MS, RF_SCAN_TIMEOUT_MS, validateRfGattDatabase,
} from './rf_contract';
import { validateRecoveryResources } from './rf_recovery_ten';
import { RF_BOOT_NONCE } from './rf_runtime';
import { parseRfControl, RfSignalBarrier, type RfControl } from './rf_signal';
import { useBeginRun, useRunReporter } from './rf_lab_session';
import { styles } from './rf_lab_screen';

type Probe = ReturnType<typeof createBleHubProbe>;
type Report = (line: string) => void;
interface Profile { rfServiceUuid?: string; rfExpectedLocalName?: string; rfWriteRejectControl?: string }
interface Expectation {
  domains: readonly string[];
  optional?: readonly string[];
  connectionId?: string;
  gattSessionId?: string;
  characteristicId?: string;
  scanSessionId?: string;
  value?: number;
  rejected?: boolean;
}
const ID = {
  initialize: '30000', scan: '30001', scanStart: '30002', scanStop: '30003',
  connection: '30004', connect: '30005', disconnect: '30006', gatt: '30007', discover: '30008',
  baseline: '30009', rejected: '30010', queued: '30011', recoveryWrite: '30012', recoveryRead: '30013',
} as const;
const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;
const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
const detail = (error: unknown): string => error instanceof Error ? error.message : String(error);
const attempts = new Set<string>();

function launch(profile: Profile): { control: RfControl; service: string; name: string } {
  const control = parseRfControl(profile.rfWriteRejectControl, 'rf-write-reject');
  if (!profile.rfServiceUuid || !UUID.test(profile.rfServiceUuid) ||
      !profile.rfExpectedLocalName?.trim() || profile.rfExpectedLocalName.length > 128) {
    throw new Error('write reject requires canonical service UUID and frozen local name');
  }
  return { control, service: profile.rfServiceUuid, name: profile.rfExpectedLocalName };
}

/** Presentation only; invalid launch metadata is rejected by the executor before native construction. */
export function writeRejectSession(raw?: string): string | undefined {
  try { return parseRfControl(raw, 'rf-write-reject').session; } catch { return undefined; }
}

/** Bound the full topology before projecting the complete target service; never truncate evidence. */
export function projectAttRejectEvent(event: RfScanEvent, runId: string, service: string, prefix = 'rf-write-reject'): string {
  let projected = event;
  if (event.gattServices != null) {
    const services = event.gattServices;
    if (services.length > 128) throw new Error('event GATT exceeds 128 services');
    let characteristics = 0;
    let descriptors = 0;
    for (const entry of services) {
      characteristics += entry.characteristics.length;
      if (characteristics > 512) throw new Error('event GATT exceeds 512 characteristics');
      for (const characteristic of entry.characteristics) {
        descriptors += characteristic.descriptors.length;
        if (descriptors > 512) throw new Error('event GATT exceeds 512 descriptors');
      }
    }
    projected = { ...event, gattServices: services.filter((entry) => entry.uuid === service) };
  }
  const json = JSON.stringify({ ...projected, runId, nonce: RF_BOOT_NONCE }, (_key, value: unknown) => {
    if (value instanceof ArrayBuffer) {
      if (value.byteLength > 64) throw new Error('event value exceeds 64 bytes');
      return Array.from(new Uint8Array(value));
    }
    if (typeof value === 'string' && value.length > 2048) throw new Error('event string exceeds 2048 characters');
    if (Array.isArray(value) && value.length > 512) throw new Error('event array exceeds 512 entries');
    return value;
  });
  const line = `${prefix}:event:${json}`;
  // Hermes-safe UTF-8 byte count, including the runtime prefix and marker, not just JSON characters.
  if (encodeURIComponent(`BLEHUB_NITRO_RUNTIME ${line}`).replace(/%[0-9A-F]{2}|./g, 'x').length >= 4000) {
    throw new Error('event evidence reaches 4000-byte UTF-8 log ceiling; no truncated event emitted');
  }
  return line;
}

class WriteRejectStream {
  private readonly commands = new Map<string, Expectation>();
  private readonly terminals = new Set<string>();
  private readonly domains = new Set<string>();
  private readonly events: RfScanEvent[] = [];
  private readonly consumed = new Set<number>();
  private failure?: Error;
  private received = 0;
  private scanActive = false;
  private disconnected = false;
  rejectRequested = false;

  constructor(private readonly probe: Probe, report: Report, session: string, service: string,
    private readonly android: boolean, private readonly healthy: () => void) {
    probe.setRfScanListener((event) => {
      if (this.received++ >= 256) { this.failure ??= new Error('write reject event ledger overflow'); return; }
      try {
        report(projectAttRejectEvent(event, session, service));
        this.validate(event);
        this.events.push(event);
      } catch (error: unknown) { this.failure ??= new Error(detail(error)); }
    });
  }

  expect(id: string, expected: Expectation): void {
    this.assertHealthy();
    if (this.commands.has(id)) throw new Error(`command ID reused: ${id}`);
    this.commands.set(id, expected);
    if (id === ID.scanStart) this.scanActive = true;
  }
  private validate(event: RfScanEvent): void {
    if (event.kind === 'advertisementObserved') {
      if (!this.scanActive || event.commandId != null || event.error ||
          event.scanSessionId !== ID.scan || !isCanonicalU64(event.peripheralId) ||
          event.connectionId != null || event.gattSessionId != null || event.characteristicId != null) {
        throw new Error('foreign advertisement route');
      }
      return;
    }
    const id = event.kind === 'adapterSnapshotChanged' && event.commandId == null ? ID.initialize : event.commandId ?? '';
    const expected = this.commands.get(id);
    if (!expected) throw new Error(`unknown command/domain: ${event.kind}:${id}`);
    if (event.subscriptionId != null) throw new Error('unexpected subscription route');
    for (const key of ['connectionId', 'gattSessionId', 'characteristicId', 'scanSessionId'] as const) {
      if ((event.kind !== 'commandCompleted' && expected[key] !== undefined && event[key] !== expected[key]) ||
          (event[key] != null && event[key] !== expected[key])) throw new Error(`${event.kind} ${key} mismatch`);
    }
    if ((id === ID.rejected || id === ID.queued) && !this.rejectRequested) {
      throw new Error('held operation completed before REJECT request');
    }
    if (event.kind === 'commandCompleted') {
      if (this.terminals.has(id)) throw new Error(`duplicate terminal: ${id}`);
      if (expected.rejected) {
        const error = event.error;
        const native = error?.platformDetails;
        if (error?.category !== 'gatt' || native?.code !== '14' ||
            native.platform !== (this.android ? 'platform_android' : 'platform_apple') ||
            native.name !== (this.android ? 'BluetoothGatt' : 'CBATTErrorDomain')) {
          throw new Error('write rejection lacks exact native ATT error 14 mapping');
        }
      } else if (event.error) throw new Error(`unexpected terminal error: ${id}:${event.error.category}`);
      this.terminals.add(id);
      return;
    }
    if (this.disconnected) throw new Error('domain event after disconnect');
    const domain = event.kind === 'connectionStateChanged' ? `${event.kind}:${event.connectionState}` : event.kind;
    if (expected.rejected || event.error || ![...expected.domains, ...(expected.optional ?? [])].includes(domain)) {
      throw new Error(`unexpected domain: ${event.kind}:${id}`);
    }
    const key = `${id}:${domain}`;
    if (this.domains.has(key)) throw new Error(`duplicate domain: ${key}`);
    this.domains.add(key);
    if (event.kind === 'adapterSnapshotChanged' && (event.adapterState !== 'PoweredOn' || event.scanAuthorization !== 'Authorized')) {
      throw new Error('adapter must be PoweredOn and Authorized');
    }
    if (event.kind === 'characteristicRead') {
      const value = new Uint8Array(event.value ?? new ArrayBuffer(0));
      if (value.length !== 1 || value[0] !== expected.value) throw new Error(`unexpected read value: ${id}`);
    }
    if (event.kind === 'scanStopped') this.scanActive = false;
    if (domain === 'connectionStateChanged:Disconnected') this.disconnected = true;
  }
  assertHealthy(): void { if (this.failure) throw this.failure; this.healthy(); }
  assertComplete(): void {
    this.assertHealthy();
    for (const [id, expected] of this.commands) {
      if (!this.terminals.has(id) || expected.domains.some((kind) => !this.domains.has(`${id}:${kind}`))) {
        throw new Error(`missing terminal/domain: ${id}`);
      }
    }
  }
  async wait(predicate: (event: RfScanEvent) => boolean, cancelled: () => boolean, timeout: number): Promise<RfScanEvent> {
    const deadline = performance.now() + timeout;
    while (true) {
      this.assertHealthy();
      if (cancelled()) throw new Error('write reject cancelled');
      const index = this.events.findIndex((event, i) => !this.consumed.has(i) && predicate(event));
      if (index >= 0) { this.consumed.add(index); return this.events[index]; }
      if (performance.now() >= deadline) throw new Error('JS wait expired; no native outcome inferred');
      await pause(10);
    }
  }
  remove(): void { this.probe.removeRfScanListener(); }
}

export async function runRfWriteReject(probe: Probe, profile: Profile, report: Report,
  cancelled: () => boolean): Promise<void> {
  let stream: WriteRejectStream | undefined;
  let barrier: RfSignalBarrier | undefined;
  let failure: unknown;
  let session: string | undefined;

  try {
    const config = launch(profile);
    session = config.control.session;
    const attempt = `${session}:${config.control.token}`;
    if (attempts.has(attempt)) throw new Error('write reject attempt already used; new host launch required');
    attempts.add(attempt);
    const nonce = RF_BOOT_NONCE;
    const healthy = (): void => {
      if (RF_BOOT_NONCE !== nonce) throw new Error('JS runtime identity changed');
      if (cancelled()) throw new Error('write reject cancelled');
      barrier?.assertHealthy();
    };
    report(`rf-write-reject:runtime:session=${session}:nonce=${nonce}:phase=start`);

    barrier = new RfSignalBarrier(config.control, report);
    stream = new WriteRejectStream(probe, report, session, config.service, Platform.OS === 'android', healthy);
    const ledger = stream;
    const signals = barrier;
    const step = (name: string, route: string): void => report(`rf-write-reject:step:${name}:passed:${route}`);
    const dispatch = (id: string, expected: Expectation, submit: () => DispatchError | undefined): void => {
      ledger.expect(id, expected);
      const error = submit();
      if (error) throw new Error(`dispatch rejected: ${id}:${error.category}:${error.detail ?? ''}`);
      ledger.assertHealthy();
    };
    const wait = (id: string, kind: string, timeout = RF_CONNECTION_TIMEOUT_MS): Promise<RfScanEvent> =>
      ledger.wait((event) => event.kind === kind && (event.commandId === id || kind === 'adapterSnapshotChanged' && event.commandId == null), cancelled, timeout);
    const settle = async (id: string, kind?: string): Promise<RfScanEvent | undefined> => {
      const event = kind ? await wait(id, kind) : undefined;
      await wait(id, 'commandCompleted');
      return event;
    };
    dispatch(ID.initialize, { domains: ['adapterSnapshotChanged'] }, () => probe.initialize(ID.initialize));
    await wait(ID.initialize, 'adapterSnapshotChanged', RF_INITIALIZATION_TIMEOUT_MS);
    await wait(ID.initialize, 'commandCompleted', RF_INITIALIZATION_TIMEOUT_MS);
    step('initialize', `commandId=${ID.initialize}`);
    dispatch(ID.scanStart, { domains: ['scanStarted'], scanSessionId: ID.scan },
      () => probe.startRfScan(ID.scanStart, ID.scan, config.service));
    await settle(ID.scanStart, 'scanStarted');
    const advertisement = await ledger.wait((event) => event.kind === 'advertisementObserved' &&
      event.advertisedServices?.includes(config.service) === true &&
            (event.localName === undefined || event.localName === config.name), cancelled, RF_SCAN_TIMEOUT_MS);
    dispatch(ID.scanStop, { domains: ['scanStopped'], scanSessionId: ID.scan }, () => probe.stopRfScan(ID.scanStop, ID.scan));
    await settle(ID.scanStop, 'scanStopped');
    step('scan', `scanSessionId=${ID.scan}:peripheralId=${advertisement.peripheralId}`);
    const connection = { connectionId: ID.connection };
    dispatch(ID.connect, { domains: ['connectionStateChanged:Connecting', 'connectionStateChanged:Connected'], ...connection },
      () => probe.connectRfPeripheral(ID.connect, ID.connection, advertisement.peripheralId!));
    await ledger.wait((event) => event.commandId === ID.connect && event.connectionState === 'Connected', cancelled, RF_CONNECTION_TIMEOUT_MS);
    await wait(ID.connect, 'commandCompleted');
    step('connected', `commandId=${ID.connect}:connectionId=${ID.connection}:peripheralId=${advertisement.peripheralId}`);
    dispatch(ID.discover, { domains: ['gattDiscovered'], ...connection, gattSessionId: ID.gatt },
      () => probe.discoverRfGatt(ID.discover, ID.connection, ID.gatt));
    const discovered = (await settle(ID.discover, 'gattDiscovered'))!;
    const database = validateRfGattDatabase(discovered, config.service, ID.connection);
    const characteristic = database.readWriteCharacteristicId;
    const route = { ...connection, gattSessionId: ID.gatt, characteristicId: characteristic };
    const routeText = `connectionId=${ID.connection}:gattSessionId=${ID.gatt}:characteristicId=${characteristic}`;
    step('gatt', `commandId=${ID.discover}:${routeText}`);
    const read = (id: string, value: number): void => dispatch(id, { domains: ['characteristicRead'], ...route, value },
      () => probe.readRfCharacteristic(id, ID.connection, ID.gatt, characteristic));
    const write = (id: string, value: number, rejected = false): void => dispatch(id,
      { domains: rejected ? [] : ['characteristicWritten'], ...route, rejected },
      () => probe.writeRfCharacteristic(id, ID.connection, ID.gatt, characteristic, new Uint8Array([value]).buffer, 'with_response'));
    read(ID.baseline, 0);
    await settle(ID.baseline, 'characteristicRead');
    step('baseline-read', `commandId=${ID.baseline}:${routeText}:value=00`);
    signals.expect('armed');
    signals.ready('arm');
    await signals.wait(cancelled, () => ledger.assertHealthy());
    // One monotonic budget across dispatch, HELD and REJECT ACK; never extend the backend watchdog.
    const rejectDeadline = performance.now() + 6500;
    const budget = (): number => {
      const remaining = rejectDeadline - performance.now();
      if (remaining <= 0) throw new Error('write reject exceeded prompt REJECT budget (<7s)');
      return remaining;
    };
    signals.expect('held');
    write(ID.rejected, 0x11, true);
    step('write-dispatched', `commandId=${ID.rejected}:${routeText}:value=11`);
    signals.ready('write-dispatched');
    await signals.wait(cancelled, () => { ledger.assertHealthy(); budget(); }, budget());
    read(ID.queued, 0);
    step('queued-read-dispatched', `commandId=${ID.queued}:${routeText}`);
    signals.expect('rejected');
    ledger.rejectRequested = true;
    signals.ready('reject');
    await signals.wait(cancelled, () => { ledger.assertHealthy(); budget(); }, budget());
    budget();
    await settle(ID.rejected);
    step('write-rejected', `commandId=${ID.rejected}:${routeText}:error=gatt:code=14`);
    await settle(ID.queued, 'characteristicRead');
    step('unchanged-read', `commandId=${ID.queued}:${routeText}:value=00`);
    write(ID.recoveryWrite, 0x22);
    await settle(ID.recoveryWrite, 'characteristicWritten');
    step('recovery-write', `commandId=${ID.recoveryWrite}:${routeText}:value=22`);
    read(ID.recoveryRead, 0x22);
    await settle(ID.recoveryRead, 'characteristicRead');
    step('recovery-read', `commandId=${ID.recoveryRead}:${routeText}:value=22`);
    signals.expect('close-allowed');
    signals.ready('recovery-done');
    await signals.wait(cancelled, () => ledger.assertHealthy());
    dispatch(ID.disconnect, { domains: ['connectionStateChanged:Disconnected'], optional: ['connectionStateChanged:Disconnecting'], ...connection },
      () => probe.disconnectRfPeripheral(ID.disconnect, ID.connection));
    await ledger.wait((event) => event.commandId === ID.disconnect && event.connectionState === 'Disconnected', cancelled, RF_CONNECTION_TIMEOUT_MS);
    await wait(ID.disconnect, 'commandCompleted');
    step('disconnected', `commandId=${ID.disconnect}:connectionId=${ID.connection}`);
    const silenceUntil = performance.now() + RF_POST_DISCONNECT_OBSERVATION_MS;
    while (performance.now() < silenceUntil) { ledger.assertHealthy(); await pause(25); }
    ledger.assertComplete();
  } catch (error: unknown) { failure = error; }
  finally {
    const recordFailure = (error: unknown): void => {
      failure = failure === undefined ? error : new Error(`${detail(failure)}; cleanup: ${detail(error)}`);
    };
    // Retain both listeners through native close so late/duplicate events remain failures.
    try { await probe.close(); stream?.assertHealthy(); barrier?.assertHealthy(); }
    catch (error: unknown) { recordFailure(error); }
    try {
      const snapshot = await probe.snapshotRfResources();
      validateRecoveryResources(snapshot, 'closed', Platform.OS === 'android');
      report(`rf-write-reject:resources:phase=closed:snapshot=${JSON.stringify(snapshot)}`);
    } catch (error: unknown) { recordFailure(error); }
    for (const remove of [() => stream?.remove(), () => barrier?.remove()]) {
      try { remove(); } catch (error: unknown) { recordFailure(error); }
    }

  }
  if (cancelled() && failure === undefined) failure = new Error('write reject cancelled');
  if (failure !== undefined) {
    report(`rf-write-reject:failed:${detail(failure)}`);
    throw failure;
  }
  report(`rf-write-reject:step:close:passed:session=${session}`);
  report(`rf-write-reject:runtime:session=${session}:nonce=${RF_BOOT_NONCE}:phase=close`);
  report('rf-write-reject:passed');
}

/** Host-only executor inside the existing mounted workbench. No UI/BTP launch shortcut. */
export function RfWriteRejectLab(profile: Profile): React.JSX.Element {
  const present = useRunReporter();
  const begin = useBeginRun();
  const [lines, setLines] = useState<string[]>([]);
  const [status, setStatus] = useState('ready');
  const started = useRef(false);
  useEffect(() => {
    let active = true;
    const token = { cancelled: false };
    const report = (line: string): void => {
      console.log(`BLEHUB_NITRO_RUNTIME ${line}`);
      present(line);
      if (active && !/:event:|:resources:/.test(line)) setLines((current) => [...current.slice(-7), line]);
    };
    void Promise.resolve().then(async () => {
      if (!active || started.current) return;
      started.current = true;
      begin();
      setStatus('running');
      let probe: Probe | undefined;
      try {
        launch(profile);
        probe = createBleHubProbe();
        await runRfWriteReject(probe, profile, report, () => token.cancelled);
        if (active) setStatus('passed');
      } catch (error: unknown) {
        if (!probe) report(`rf-write-reject:failed:${detail(error)}`);
        if (active) setStatus('failed');
      }
    });
    return () => { active = false; token.cancelled = true; };
  }, []);
  return (
    <View testID="rf-write-reject-root" style={styles.root}>
      <Text style={styles.title}>Response-write REJECT</Text>
      <Text testID="rf-write-reject-status" style={styles.statusValue}>{status}</Text>
      <Text style={styles.subtitle}>Host-controlled ARMED → HELD → REJECT → same-route recovery. App checks are not the host verdict.</Text>
      <Text style={styles.stepDetail}>A new attempt requires a new explicit host launch. No firmware or BTP control runs in this UI.</Text>
      {lines.map((line, index) => <Text key={index} style={styles.eventLine}>{line}</Text>)}
    </View>
  );
}
