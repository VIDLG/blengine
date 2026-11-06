import React, { useEffect, useRef, useState } from 'react';
import { Pressable, ScrollView, Text, View } from 'react-native';
import { RfSignalBarrier, parseRfControl, type RfControl } from './rf_signal';
import { styles } from './rf_lab_screen';
import { useBeginRun, useRunReporter } from './rf_lab_session';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { DispatchError, RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import {
  isCanonicalU64, RF_WATCHDOG_IDS as ID,
  RF_WATCHDOG_WAIT_MS, RF_WATCHDOG_SILENCE_MS, validateRfGattDatabase,
} from './rf_contract';

type Probe = ReturnType<typeof createBleHubProbe>;
type Report = (line: string) => void;
interface CommandExpectation {
  error?: 'timeout' | 'operation_aborted';
  domains: readonly string[];
  optionalDomains?: readonly string[];
  connectionId?: string;
  gattSessionId?: string;
  characteristicId?: string;
  scanSessionId?: string;
}
const delay = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
const detail = (error: unknown): string => error instanceof Error ? error.message : String(error);
const domainKey = (event: RfScanEvent): string => event.kind === 'connectionStateChanged'
  ? `${event.kind}:${event.connectionState}` : event.kind;

const MAX_LEDGER_VALUE_BYTES = 64;

function projectWatchdogEvent(event: RfScanEvent): object {
  const services = event.gattServices;
  const bytes = event.value == null ? undefined : new Uint8Array(event.value, 0, Math.min(event.value.byteLength, MAX_LEDGER_VALUE_BYTES));
  return {
    kind: event.kind,
    commandId: event.commandId,
    scanSessionId: event.scanSessionId,
    peripheralId: event.peripheralId,
    advertisedServices: event.advertisedServices,
    localName: event.localName,
    connectionId: event.connectionId,
    connectionState: event.connectionState,
    gattSessionId: event.gattSessionId,
    characteristicId: event.characteristicId,
    subscriptionId: event.subscriptionId,
    notificationState: event.notificationState,
    actualNotificationMode: event.actualNotificationMode,
    adapterState: event.adapterState,
    scanAuthorization: event.scanAuthorization,
    rssi: event.rssi,
    // Preserve native error evidence verbatim; only topology/value logging is compacted.
    error: event.error,
    gattSummary: services == null ? undefined : {
      serviceCount: services.length,
      characteristicCount: services.reduce((count, service) => count + service.characteristics.length, 0),
      descriptorCount: services.reduce((count, service) => count + service.characteristics.reduce(
        (total, characteristic) => total + characteristic.descriptors.length, 0,
      ), 0),
    },
    value: bytes == null ? undefined : Array.from(bytes),
    valueByteLength: event.value?.byteLength,
    valueTruncated: bytes == null ? undefined : event.value!.byteLength > bytes.length,
  };
}

/** A separate, fail-closed ledger: expected failures are registered before dispatch. */
export class WatchdogEventStream {
  readonly events: RfScanEvent[] = [];
  private readonly commands = new Map<string, CommandExpectation>();
  private readonly terminals = new Set<string>();
  private readonly domains = new Set<string>();
  private readonly consumed = new Set<number>();
  private failure?: Error;
  private removed = false;
  private retired = false;
  private activeScan?: string;
  private recoveryRetired = false;

  constructor(private readonly probe: Probe, private readonly report: Report, private readonly externalHealthy: () => void = () => {}) {
    probe.setRfScanListener((event) => {
      if (this.removed) return;
      // Keep even invalid/duplicate events visible to the host's native-source checker.
      report(`rf-watchdog:event:${JSON.stringify(projectWatchdogEvent(event))}`);
      try {
        this.validate(event);
        if (this.events.length >= 256) throw new Error('watchdog ledger overflow');
        this.events.push(event);
      } catch (error: unknown) {
        this.failure ??= new Error(detail(error));
      }
    });
  }

  expect(commandId: string, expectation: CommandExpectation): void {
    this.assertHealthy();
    if (this.commands.has(commandId)) throw new Error(`command ID reused: ${commandId}`);
    this.commands.set(commandId, expectation);
    if (expectation.domains.includes('scanStarted')) {
      if (this.activeScan) throw new Error('watchdog scans must not overlap');
      this.activeScan = expectation.scanSessionId;
    }
  }

  private validate(event: RfScanEvent): void {
    if (event.kind === 'commandCompleted') {
      const id = event.commandId ?? '';
      const expected = this.commands.get(id);
      if (!expected || this.terminals.has(id)) throw new Error(`unknown/duplicate terminal: ${id}`);
      if (event.error?.category !== expected.error) throw new Error(`unexpected terminal: ${id}:${event.error?.category ?? 'ok'}`);
      if (expected.error === 'timeout') {
        const error = event.error!;
        const native = error.platformDetails;
        const message = `${error.message} ${native?.message ?? ''}`;
        // Apple intentionally has no NSError/platformDetails for its own watchdog.
                const apple = (native == null || native.platform === 'platform_apple') && error.message.startsWith('Apple GATT operation watchdog expired:');
        const android = native?.platform === 'platform_android' && native.name === 'AndroidBleBackend' && message.includes('native callback watchdog');
        if (!apple && !android) throw new Error('timeout lacks production native watchdog evidence');
      }
      // CommandCompleted normally has no route fields. If supplied, they must agree.
      this.validateRoute(event, expected, false);
      this.terminals.add(id);
      return;
    }
    if (event.kind === 'adapterSnapshotChanged') {
      if (!this.commands.has(ID.initialize) || event.error || event.adapterState !== 'PoweredOn' || event.scanAuthorization !== 'Authorized') {
        throw new Error('watchdog adapter must be PoweredOn + Authorized');
      }
      if (event.commandId != null) {
        if (event.commandId !== ID.initialize) throw new Error('unexpected adapter snapshot command ID');
        const key = `${ID.initialize}:adapterSnapshotChanged`;
        if (this.domains.has(key)) throw new Error('duplicate initialization snapshot');
        this.domains.add(key);
      }
      return;
    }
    if (event.kind === 'advertisementObserved') {
      if (!this.activeScan || event.commandId != null || event.error ||
          event.scanSessionId !== this.activeScan || !isCanonicalU64(event.peripheralId)) {
        throw new Error('unexpected advertisement route');
      }
      return;
    }
    if (event.kind === 'connectionStateChanged' && event.commandId == null) {
      const loss = event.connectionState === 'Disconnected' && !event.error ||
        event.connectionState == null && event.error?.category === 'connection' &&
        event.error.platformDetails?.platform === 'platform_android';
      if (event.connectionId !== ID.connection || this.retired || !loss ||
          !this.commands.has(ID.active) || !this.commands.has(ID.queued)) {
        throw new Error('unexpected/duplicate passive connection loss');
      }
      this.retired = true;
      return;
    }
    if (this.retired && (event.connectionId === ID.connection || event.gattSessionId === ID.gatt)) {
      throw new Error(`old route event after retirement: ${event.kind}`);
    }
    if (this.recoveryRetired && event.connectionId === ID.recoveryConnection) {
      throw new Error(`recovery route event after disconnect: ${event.kind}`);
    }
    const id = event.commandId ?? '';
    const expected = this.commands.get(id);
    const key = domainKey(event);
    if (!expected || expected.error || event.error || !(expected.domains.includes(key) || expected.optionalDomains?.includes(key))) {
      throw new Error(`unexpected domain event: ${event.kind}:${id}`);
    }
    this.validateRoute(event, expected, true);
    const unique = `${id}:${key}`;
    if (this.domains.has(unique)) throw new Error(`duplicate domain: ${unique}`);
    this.domains.add(unique);
    if (event.kind === 'scanStopped') this.activeScan = undefined;
    if (id === ID.recoveryDisconnect && event.connectionState === 'Disconnected') this.recoveryRetired = true;
  }

  private validateRoute(event: RfScanEvent, expected: CommandExpectation, required: boolean): void {
    for (const field of ['connectionId', 'gattSessionId', 'characteristicId', 'scanSessionId'] as const) {
      if (expected[field] !== undefined && (required || event[field] != null) && event[field] !== expected[field]) {
        throw new Error(`${event.kind} ${field} mismatch: ${event[field] ?? 'missing'}`);
      }
      if (expected[field] === undefined && event[field] != null) throw new Error(`unexpected ${field}`);
    }
  }

  assertHealthy(): void { if (this.failure) throw this.failure; this.externalHealthy(); }
  assertComplete(): void {
    this.assertHealthy();
    if (!this.retired) throw new Error('old connection was not retired');
    for (const [id, expected] of this.commands) {
      if (!this.terminals.has(id)) throw new Error(`missing terminal: ${id}`);
      for (const key of expected.domains) {
        if (!this.domains.has(`${id}:${key}`)) throw new Error(`missing domain: ${id}:${key}`);
      }
    }
  }
  async waitFor(predicate: (event: RfScanEvent) => boolean, label: string, cancelled: () => boolean, timeoutMs = RF_WATCHDOG_WAIT_MS): Promise<RfScanEvent> {
    const deadline = Date.now() + timeoutMs;
    while (true) {
      this.assertHealthy();
      if (cancelled()) throw new Error('watchdog closed by caller');
      const index = this.events.findIndex((event, i) => !this.consumed.has(i) && predicate(event));
      if (index >= 0) {
        this.consumed.add(index);
        return this.events[index];
      }
      if (Date.now() >= deadline) throw new Error(`JS outer wait expired (${label}); no native outcome inferred`);
      await delay(Math.min(50, deadline - Date.now()));
    }
  }
  async observeSilence(cancelled: () => boolean): Promise<void> {
    const deadline = Date.now() + RF_WATCHDOG_SILENCE_MS;
    while (Date.now() < deadline) {
      this.assertHealthy();
      if (cancelled()) throw new Error('watchdog closed by caller');
      await delay(Math.min(50, deadline - Date.now()));
    }
    this.assertHealthy();
  }
  remove(): void { if (!this.removed) { this.removed = true; this.probe.removeRfScanListener(); } }
}

export async function runRfWatchdog(probe: Probe, service: string | undefined, report: Report, cancelled: () => boolean, control?: RfControl, expectedLocalName?: string): Promise<void> {
  let stream: WatchdogEventStream | undefined;
  let barrier: RfSignalBarrier | undefined;
  let failure: unknown;
  let failed = false;
  try {
    const serviceUuid = service;
    if (serviceUuid == null || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(serviceUuid) || serviceUuid.length !== 36) {
      throw new Error('strict canonical service UUID required from launch arguments; installed image attestation belongs to the host');
    }
    if (!control) throw new Error('watchdog requires host ARM control metadata before any BLE command');
    if (control) {
      if (control.mode !== 'rf-watchdog' || control.flow !== 9 || !expectedLocalName) throw new Error('integrated watchdog requires frozen provider identity');
      barrier = new RfSignalBarrier(control, report);
    }
    const healthy = (): void => { barrier?.assertHealthy(); };
    stream = new WatchdogEventStream(probe, report, healthy);
    const ledger = stream;
    const dispatch = (id: string, expected: CommandExpectation, submit: () => DispatchError | undefined): void => {
      if (cancelled()) throw new Error('watchdog closed by caller');
      healthy();
      ledger.expect(id, expected);
      const error = submit();
      if (error) throw new Error(`dispatch rejected: ${id}:${error.category}:${error.detail ?? ''}`);
      ledger.assertHealthy();
    };
    const wait = (kind: string, id: string): Promise<RfScanEvent> => ledger.waitFor(
      (event) => event.kind === kind && event.commandId === id, `${kind}:${id}`, cancelled,
    );
    const terminal = (id: string): Promise<RfScanEvent> => wait('commandCompleted', id);
    dispatch(ID.initialize, { domains: [] }, () => probe.initialize(ID.initialize));
    // Snapshot commandId is optional in native projection; readiness is required independently.
    await ledger.waitFor((event) => event.kind === 'adapterSnapshotChanged', 'adapter readiness', cancelled);
    await terminal(ID.initialize);
    dispatch(ID.scanStart, { domains: ['scanStarted'], scanSessionId: ID.scan }, () => probe.startRfScan(ID.scanStart, ID.scan, serviceUuid));
    await wait('scanStarted', ID.scanStart);
    await terminal(ID.scanStart);
    const advertisement = await ledger.waitFor((event) => event.kind === 'advertisementObserved' &&
      event.advertisedServices?.includes(serviceUuid) === true && (expectedLocalName === undefined || event.localName === undefined || event.localName === expectedLocalName), 'watchdog service advertisement', cancelled);
    const peripheralId = advertisement.peripheralId!;
    dispatch(ID.scanStop, { domains: ['scanStopped'], scanSessionId: ID.scan }, () => probe.stopRfScan(ID.scanStop, ID.scan));
    await wait('scanStopped', ID.scanStop);
    await terminal(ID.scanStop);

    const connect = async (command: string, connection: string): Promise<void> => {
      dispatch(command, { domains: ['connectionStateChanged:Connecting', 'connectionStateChanged:Connected'], connectionId: connection },
        () => probe.connectRfPeripheral(command, connection, peripheralId));
      await ledger.waitFor((event) => event.commandId === command && event.connectionState === 'Connecting', 'Connecting', cancelled);
      await ledger.waitFor((event) => event.commandId === command && event.connectionState === 'Connected', 'Connected', cancelled);
      await terminal(command);
    };
    const discover = async (command: string, connection: string, gatt: string, recovery = false): Promise<string> => {
      dispatch(command, { domains: ['gattDiscovered'], connectionId: connection, gattSessionId: gatt }, () => probe.discoverRfGatt(command, connection, gatt));
      const event = await wait('gattDiscovered', command);
      const database = validateRfGattDatabase(event, serviceUuid, connection);
      await terminal(command);
      // The host ARMs the RW target; recovery reads the unguarded updates value (00).
      return recovery ? database.notificationCharacteristicId : database.readWriteCharacteristicId;
    };
    await connect(ID.connect, ID.connection);
    const characteristic = await discover(ID.discover, ID.connection, ID.gatt);
    if (barrier) {
      report(`rf-watchdog:discovery-ready:connectionId=${ID.connection}:gattSessionId=${ID.gatt}:characteristicId=${characteristic}`);
      barrier.expect('armed');
      barrier.ready('arm');
      await barrier.wait(cancelled, () => ledger.assertHealthy());
      barrier.expect('held');
    }
    const readRoute = { connectionId: ID.connection, gattSessionId: ID.gatt, characteristicId: characteristic };
    dispatch(ID.active, { domains: [], error: 'timeout', ...readRoute }, () => probe.readRfCharacteristic(ID.active, ID.connection, ID.gatt, characteristic));
    if (barrier) {
      report(`rf-watchdog:active-dispatched:commandId=${ID.active}:connectionId=${ID.connection}:gattSessionId=${ID.gatt}:characteristicId=${characteristic}`);
      barrier.ready('read-dispatched');
      await barrier.wait(cancelled, () => ledger.assertHealthy());
    }
    // Production's serialized FIFO owns native entry. The host independently
    // proves the exact v2 HELD receipt; dispatch acceptance is not that evidence.
    dispatch(ID.queued, { domains: [], error: 'operation_aborted', ...readRoute }, () => probe.readRfCharacteristic(ID.queued, ID.connection, ID.gatt, characteristic));
    report(`rf-watchdog:read-dispatched:activeCommandId=${ID.active}:queuedCommandId=${ID.queued}:connectionId=${ID.connection}:gattSessionId=${ID.gatt}:characteristicId=${characteristic}`);
    const watchdogDeadline = Date.now() + RF_WATCHDOG_WAIT_MS;
    await ledger.waitFor((event) => event.kind === 'commandCompleted' && event.commandId === ID.active,
      'native active watchdog terminal', cancelled, Math.max(1, watchdogDeadline - Date.now()));
    report(`rf-watchdog:active-timeout:commandId=${ID.active}:error=timeout`);
    await ledger.waitFor((event) => event.kind === 'commandCompleted' && event.commandId === ID.queued,
      'native queued abort terminal', cancelled, Math.max(1, watchdogDeadline - Date.now()));
    report(`rf-watchdog:queued-aborted:commandId=${ID.queued}:error=operation_aborted`);
    if (barrier) barrier.expect('released');
    report('rf-watchdog:awaiting-release');
    if (barrier) {
      barrier.ready('release');
      await barrier.wait(cancelled, () => ledger.assertHealthy(), 15_000);
    }
    // The host acknowledges the exact retained receipt and zero native owners.
    // This barrier does not authorize RESOLVE on a disconnected ticket.
    // Native retirement can already be buffered (notably on Android).
    await ledger.waitFor((event) => event.kind === 'connectionStateChanged' && event.commandId == null && event.connectionId === ID.connection,
      'real old connection retirement', cancelled);
    report(`rf-watchdog:connection-retired:connectionId=${ID.connection}`);
    await ledger.observeSilence(cancelled);
    report(`rf-watchdog:late-silence:observationMs=${RF_WATCHDOG_SILENCE_MS}`);
    if (barrier) {
      barrier.expect('recovery');
      barrier.ready('recover');
      await barrier.wait(cancelled, () => ledger.assertHealthy(), 12_000);
    }
    // Native retirement can precede the host's GAP-loss/re-advertise sequence.
    // Wait for a fresh advertisement rather than race it or reuse failed connect IDs.
    dispatch(ID.recoveryScanStart, { domains: ['scanStarted'], scanSessionId: ID.recoveryScan },
      () => probe.startRfScan(ID.recoveryScanStart, ID.recoveryScan, serviceUuid));
    await wait('scanStarted', ID.recoveryScanStart);
    await terminal(ID.recoveryScanStart);
    await ledger.waitFor((event) => event.kind === 'advertisementObserved' && event.scanSessionId === ID.recoveryScan &&
      event.peripheralId === peripheralId && event.advertisedServices?.includes(serviceUuid) === true && (expectedLocalName === undefined || event.localName === undefined || event.localName === expectedLocalName),
      'same peripheral re-advertised after host GAP recovery', cancelled);
    dispatch(ID.recoveryScanStop, { domains: ['scanStopped'], scanSessionId: ID.recoveryScan },
      () => probe.stopRfScan(ID.recoveryScanStop, ID.recoveryScan));
    await wait('scanStopped', ID.recoveryScanStop);
    await terminal(ID.recoveryScanStop);
    await connect(ID.recoveryConnect, ID.recoveryConnection);
    const recoveryCharacteristic = await discover(ID.recoveryDiscover, ID.recoveryConnection, ID.recoveryGatt, true);
    dispatch(ID.recoveryRead, { domains: ['characteristicRead'], connectionId: ID.recoveryConnection, gattSessionId: ID.recoveryGatt, characteristicId: recoveryCharacteristic },
      () => probe.readRfCharacteristic(ID.recoveryRead, ID.recoveryConnection, ID.recoveryGatt, recoveryCharacteristic));
    const read = await wait('characteristicRead', ID.recoveryRead);
    const value = new Uint8Array(read.value ?? new ArrayBuffer(0));
    if (value.length !== 1 || value[0] !== 0) throw new Error('recovery read value must be exactly 00');
    await terminal(ID.recoveryRead);
    report(`rf-watchdog:recovery-read:passed:commandId=${ID.recoveryRead}:connectionId=${ID.recoveryConnection}:gattSessionId=${ID.recoveryGatt}:characteristicId=${recoveryCharacteristic}:value=00`);
    dispatch(ID.recoveryDisconnect, { domains: ['connectionStateChanged:Disconnected'], optionalDomains: ['connectionStateChanged:Disconnecting'], connectionId: ID.recoveryConnection },
      () => probe.disconnectRfPeripheral(ID.recoveryDisconnect, ID.recoveryConnection));
    await wait('connectionStateChanged', ID.recoveryDisconnect);
    await terminal(ID.recoveryDisconnect);
    await ledger.observeSilence(cancelled);
    ledger.assertComplete();
  } catch (error: unknown) {
    failure = error;
    failed = true;
  } finally {
    // Keep the ledger live through disposal; a duplicate delivered during close must fail.
    try {
      await probe.close();
      stream?.assertHealthy();
      barrier?.assertHealthy();
      report('rf-watchdog:close:passed');
    } catch (error: unknown) {
      failure = failed ? new Error(`${detail(failure)}; close failed: ${detail(error)}`) : error;
      failed = true;
      report(`rf-watchdog:close:failed:${detail(error)}`);
    } finally {
      for (const cleanup of [() => stream?.remove(), () => barrier?.remove()]) {
        try { cleanup(); } catch (error: unknown) { failure = error; failed = true; }
      }
    }
  }
  if (cancelled() && !failed) { failed = true; failure = new Error('watchdog closed by caller'); }
  if (failed) {
    report(`rf-watchdog:failed:${detail(failure)}`);
    throw failure;
  }
  report('rf-watchdog:passed');
}

/** Diagnostic details inside the workbench; host coordination is still required. */
export function RfWatchdogLab({ rfServiceUuid, rfCancellationControl, rfExpectedLocalName }: { rfServiceUuid?: string; rfCancellationControl?: string; rfExpectedLocalName?: string }): React.JSX.Element {
  const present = useRunReporter();
  const begin = useBeginRun();
  const [lines, setLines] = useState<string[]>([]);
  const [status, setStatus] = useState('ready');
  const mounted = useRef(false);
  const autoStarted = useRef(false);
  const run = useRef<{ cancelled: boolean } | undefined>(undefined);
  const report = (line: string): void => {
    console.log(`BLEHUB_NITRO_RUNTIME ${line}`);
    present(line);
    if (mounted.current && !/:event:|:resources:/.test(line)) setLines((current) => [...current.slice(-7), line]);
  };
  const start = (): void => {
    if (run.current) return;
    begin();
    const token = { cancelled: false };
    run.current = token;
    setLines([]);
    setStatus('running');
    void (async () => {
      let probe: Probe | undefined;
      try {
        const control = parseRfControl(rfCancellationControl, 'rf-watchdog');
        if (!rfExpectedLocalName) throw new Error('watchdog requires the frozen fixture local name');
        probe = createBleHubProbe();
        await runRfWatchdog(probe, rfServiceUuid, report, () => token.cancelled, control, rfExpectedLocalName);
        if (mounted.current) setStatus('passed');
      } catch (error: unknown) {
        if (mounted.current) setStatus(token.cancelled ? 'closed' : 'failed');
        // Construction failures occur before the runner can install its ledger.
        if (!probe) report(`rf-watchdog:failed:${detail(error)}`);
      } finally { run.current = undefined; }
    })();
  };
  useEffect(() => {
    let active = true;
    mounted.current = true;
    // Defer native construction past React's development effect replay: it
    // must not create a probe only to cancel it before the visible run starts.
    void Promise.resolve().then(() => {
      if (active && !autoStarted.current) { autoStarted.current = true; start(); }
    });
    return () => {
      active = false;
      mounted.current = false;
      if (run.current) run.current.cancelled = true;
    };
  }, []);
  return (
    <View testID="rf-watchdog-root" style={styles.safeArea}>
      <ScrollView contentInsetAdjustmentBehavior="automatic" contentContainerStyle={styles.root}>
        <Text style={styles.title}>Native watchdog</Text>
        <Text style={styles.subtitle}>Held-read timeout, queue retirement and recovery. The host proves HELD, retained retirement and resource-zero barriers.</Text>
        <Text style={styles.stepDetail}>Production watchdog defaults are unchanged.</Text>
        <Text testID="rf-watchdog-status" style={styles.statusValue}>rf-watchdog {status}</Text>
        <Pressable testID="rf-watchdog-rerun" accessibilityRole="button" disabled style={[styles.button, styles.secondaryButton]}>
          <Text style={styles.stepDetail}>A new watchdog attempt requires a new host session.</Text>
          <Text style={styles.secondaryButtonText}>Host-controlled</Text>
        </Pressable>
        <Pressable testID="rf-watchdog-close" accessibilityRole="button" onPress={() => {
          if (run.current) run.current.cancelled = true;
          else { setStatus('closed'); setLines([]); }
        }}>
          <Text style={styles.secondaryButtonText}>Close diagnostic</Text>
        </Pressable>
        <View style={styles.card}>
          <Text style={styles.cardTitle}>Recent milestones</Text>
          {lines.map((line, index) => <Text key={index} style={styles.eventLine}>{line}</Text>)}
        </View>
      </ScrollView>
    </View>
  );
}
