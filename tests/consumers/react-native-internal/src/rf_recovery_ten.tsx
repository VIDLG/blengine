import { Linking, Platform } from 'react-native';
import type { createBleHubProbe } from '@blehub/react-native-internal';
import type { RfResourceSnapshot, RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import {
  isCanonicalU64, RF_CONNECTION_TIMEOUT_MS, RF_INITIALIZATION_TIMEOUT_MS,
  RF_SCAN_TIMEOUT_MS, validateRfGattDatabase,
} from './rf_contract';

import { RF_BOOT_NONCE } from './rf_runtime';
import type { RfSuiteSignalGuard } from './rf_signal';

type Probe = ReturnType<typeof createBleHubProbe>;
export interface RecoveryEventStream {
  events: RfScanEvent[];
  waitFor(predicate: (event: RfScanEvent) => boolean, timeout: number, label: string, cancelled: () => boolean): Promise<RfScanEvent>;
  assertHealthy(): void;
  remove(): void;
}
export type RecoveryStreamFactory = (probe: Probe, reject: (event: RfScanEvent) => Error | false, connection: string) => RecoveryEventStream;
const NONCE = RF_BOOT_NONCE;
const attempts = new Set<string>();
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// Conservative test-only 5/30s budget plus margin, not a cross-vendor Android guarantee.
// The first quiet window covers unknown scan history from a previous run. No retries.
const SCAN_QUIET_MS = 30000;
const SCAN_INTERVAL_MS = 7000;
const SCAN_POLICY = 'rf-recovery-ten:scan-budget-policy:version=1:platform=android:scope=conservative-test-only:quietMs=30000:minStartIntervalMs=7000:retries=0';
const monotonicMs = (): number => Math.floor(performance.now());
const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
const asError = (error: unknown): Error => error instanceof Error ? error : new Error(String(error));
export const recoveryTenId = (cycle: number, offset: number): string => String(10_000 + cycle * 100 + offset);
export const RF_RESOURCE_FIELDS = [
  'activeScans', 'connections', 'gattAttributes', 'subscriptions', 'pendingIo', 'watchdogs',
  'callbackRoutes', 'ingressRoots', 'adapterDelegates', 'peerCacheEntries', 'backendLeases',
  'hubRoots', 'handoffRoots', 'handoffPending', 'listeners', 'workerThreads', 'lifecycleOwners',
] as const satisfies readonly (keyof RfResourceSnapshot)[];
type Phase = 'baseline' | 'active' | 'passive' | 'recovery-disconnected' | 'closed';

export function validateRecoveryResources(snapshot: RfResourceSnapshot, phase: Phase, android: boolean): void {
  if (Object.keys(snapshot).length !== 17 || RF_RESOURCE_FIELDS.some((key) =>
    !Number.isSafeInteger(snapshot[key]) || snapshot[key] < 0 || Object.is(snapshot[key], -0))) {
    throw new Error('invalid 17-field resource snapshot');
  }
  const expect = (key: keyof RfResourceSnapshot, count: number): void => {
    if (snapshot[key] !== count) throw new Error(`${phase} resource ${key}: expected ${count}, got ${snapshot[key]}`);
  };
  if (phase === 'closed') { RF_RESOURCE_FIELDS.forEach((key) => expect(key, 0)); return; }
  for (const key of ['hubRoots', 'handoffRoots', 'ingressRoots', 'backendLeases', 'adapterDelegates', 'listeners'] as const) expect(key, 1);
  expect('workerThreads', android ? 2 : 0);
  expect('lifecycleOwners', android ? 1 : 0);
  for (const key of ['activeScans', 'pendingIo', 'watchdogs', 'handoffPending'] as const) expect(key, 0);
  if (snapshot.peerCacheEntries > 1) throw new Error(`${phase} peer cache exceeds single fixture`);
  if (phase === 'active') {
    expect('connections', 1); expect('subscriptions', 1);
    if (snapshot.gattAttributes <= 0) throw new Error('active GATT positive control missing');
  } else {
    expect('connections', 0); expect('subscriptions', 0);
    // Apple identity/value barriers are legitimate until backend close, not leaks to erase.
    if (android) { expect('gattAttributes', 0); expect('callbackRoutes', 0); }
  }
}

/** One payload URL per cycle, registered before the first native initialize. */
export class RecoveryReadyBarrier {
  private next = 0;
  private awaiting: number | undefined;
  private received = false;
  private failure: Error | undefined;
  private readonly subscription: ReturnType<typeof Linking.addEventListener>;
  constructor(private readonly runId: string, private readonly report: (line: string) => void, private readonly nonce = NONCE) {
    this.subscription = Linking.addEventListener('url', ({ url }) => {
      if (this.failure) return;
      const expected = `blehub-rn-spike://recover/${this.runId}/${this.next}`;
      if (url !== expected || this.awaiting !== this.next || this.received || this.next >= 10) {
        this.failure = new Error('foreign, early, duplicate or out-of-order recovery URL');
        return;
      }
      this.received = true;
      this.report(`rf-recovery-ten:cycle:${this.next}:recovery-ready:runId=${this.runId}:source=linking`);
    });
  }
  accepts(url: string): boolean {
    return !this.failure && this.next < 10 && this.awaiting === this.next && !this.received &&
      url === `blehub-rn-spike://recover/${this.runId}/${this.next}`;
  }
  assertHealthy(nonce = this.nonce): void {
    if (nonce !== this.nonce) this.failure = new Error('recovery JS runtime nonce changed');
    if (this.failure) throw this.failure;
  }
  async wait(cycle: number, cancelled: () => boolean, healthy: () => void): Promise<void> {
    this.assertHealthy();
    if (cycle !== this.next || this.awaiting !== undefined) throw new Error('recovery readiness cycle order');
    this.awaiting = cycle;
    this.report(`rf-recovery-ten:cycle:${cycle}:recovery-awaiting:runId=${this.runId}`);
    const deadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
    while (!this.received) {
      healthy(); this.assertHealthy();
      if (cancelled()) throw new Error('recovery readiness cancelled');
      if (Date.now() >= deadline) throw new Error('physical fixture recovery readiness timed out');
      await pause(25);
    }
    this.assertHealthy(); this.awaiting = undefined; this.received = false; this.next += 1;
  }
  remove(): void { this.subscription.remove(); }
}

function project(event: RfScanEvent, service: string): object {
  const bounded = (text: string): string => {
    if (text.length > 512) throw new Error('RF event text exceeds 512 characters');
    return text;
  };
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(event)) {
    if (value == null) { result[key] = value; continue; }
    if (key === 'value') {
      const bytes = new Uint8Array(value as ArrayBuffer);
      if (bytes.length > 512) throw new Error('RF value exceeds 512 bytes');
      result[key] = Array.from(bytes);
    } else if (key === 'gattServices') {
      const services = event.gattServices!;
      if (services.length > 128 || services.reduce((n, s) => n + s.characteristics.length, 0) > 512) throw new Error('RF GATT projection exceeds limits');
      result[key] = services.filter((s) => s.uuid === service).map((s) => ({
        id: bounded(s.id), uuid: bounded(s.uuid), primary: s.primary,
        characteristics: s.characteristics.map((c) => {
          if (c.descriptors.length > 512) throw new Error('RF descriptor projection exceeds limits');
          return { ...c, id: bounded(c.id), uuid: bounded(c.uuid), descriptors: c.descriptors.map((d) => ({ id: bounded(d.id), uuid: bounded(d.uuid) })) };
        }),
      }));
    } else if (typeof value === 'string') result[key] = bounded(value);
    else if (key === 'advertisedServices') {
      if ((value as string[]).length > 128) throw new Error('RF advertised services exceeds limits');
      result[key] = (value as string[]).map(bounded);
    } else if (key === 'error') {
      const error = event.error!;
      result[key] = { category: bounded(error.category), message: bounded(error.message), platformDetails: error.platformDetails && {
        ...error.platformDetails,
        name: error.platformDetails.name == null ? error.platformDetails.name : bounded(error.platformDetails.name),
        code: error.platformDetails.code == null ? error.platformDetails.code : bounded(error.platformDetails.code),
      } };
    } else result[key] = value;
  }
  return result;
}

function passiveDisconnectError(event: RfScanEvent, connection: string, android: boolean): 'connection' | 'none' {
  if (event.kind !== 'connectionStateChanged' || event.commandId != null || event.connectionId !== connection ||
      Object.entries(event).some(([key, value]) => value != null && !['kind', 'commandId', 'connectionId', 'connectionState', 'error'].includes(key))) {
    throw new Error('invalid passive disconnect route');
  }
  if (event.error == null) {
    if (event.connectionState !== 'Disconnected') throw new Error('invalid passive disconnect success');
    return 'none';
  }
  const error = event.error;
  if (event.connectionState != null || error.category !== 'connection' || typeof error.message !== 'string' || error.message.length > 512 ||
      Object.keys(error).some((key) => !['category', 'message', 'platformDetails'].includes(key))) {
    throw new Error('invalid passive disconnect error result');
  }
  const details = error.platformDetails;
  if (details != null && (details.platform !== (android ? 'platform_android' : 'platform_apple') ||
      Object.keys(details).some((key) => !['platform', 'code', 'name', 'message'].includes(key)) ||
      Object.values(details).some((value) => value != null && (typeof value !== 'string' || value.length > 512)))) {
    throw new Error('invalid passive disconnect platform details');
  }
  return 'connection';
}

export async function runRfRecoveryTen(
  factory: () => Probe, streamFactory: RecoveryStreamFactory,
  profile: Readonly<{ rfServiceUuid?: string; rfExpectedLocalName?: string; rfRecoveryRunId?: string }>,
  report: (line: string) => void, cancelled: () => boolean,
  signalGuard?: RfSuiteSignalGuard,
): Promise<boolean> {
  const { rfServiceUuid: service, rfExpectedLocalName: localName, rfRecoveryRunId: runId } = Object.freeze({ ...profile });
  const android = Platform.OS === 'android';
  let barrier: RecoveryReadyBarrier | undefined;
  let failure: unknown;
  let eventCount = 0;
  let cycle = 0;
  let started = false;
  let verdictEmitted = false;
  let lastScanStartedMs: number | undefined;
  const retired: RecoveryEventStream[] = [];
  try {
    if (!runId || !UUID.test(runId)) throw new Error('rfRecoveryRunId must be a canonical UUID');
    if (!service || !UUID.test(service)) throw new Error('rfServiceUuid must be a canonical UUID');
    if (localName !== undefined && (!localName.trim() || localName.length > 512)) throw new Error('invalid rfExpectedLocalName');
    if (attempts.has(runId)) throw new Error('strict recovery run cannot be rerun');
    attempts.add(runId);
    report(`rf-recovery-ten:runtime:runId=${runId}:nonce=${NONCE}:phase=start`);
    started = true;
    barrier = new RecoveryReadyBarrier(runId, report);
    signalGuard?.useRecovery((url) => barrier!.accepts(url));
    if (android) report(SCAN_POLICY);
    const healthy = (): void => {
      signalGuard?.assertHealthy();
      barrier!.assertHealthy(NONCE);
      retired.forEach((s) => s.assertHealthy());
      if (cancelled()) throw new Error('strict recovery cancelled');
    };
    for (cycle = 0; cycle < 10; cycle += 1) {
      healthy();
      report(`rf-recovery-ten:cycle:${cycle}:start`);
      const epoch = cycle;
      const id = (offset: number): string => recoveryTenId(epoch, offset);
      const probe = factory();
      let stream: RecoveryEventStream | undefined;
      let closed = false;
      let cycleFailure: unknown;
      let scanRetired = false;
      let oldRouteRetired = false;
      const step = (name: string, fields: string): void => report(`rf-recovery-ten:cycle:${cycle}:${name}:passed:${fields}`);
      try {
        const commandOffsets = [0, 2, 3, 5, 7, 9, 11, 13, 15, 12];
        const domains = new Map<string, string>([[id(0), 'adapterSnapshotChanged'], [id(2), 'scanStarted'], [id(3), 'scanStopped'], [id(5), 'connectionStateChanged'], [id(7), 'gattDiscovered'], [id(9), 'notificationStateChanged'], [id(11), 'connectionStateChanged'], [id(13), 'gattDiscovered'], [id(15), 'characteristicRead'], [id(12), 'connectionStateChanged']]);
        const seenDomains = new Set<string>();
        const terminals = new Set<string>();
        const expectedRoutes = { scanSessionId: [id(1)], connectionId: [id(4), id(10)], gattSessionId: [id(6), id(14)], subscriptionId: [id(8)] };
        const reject = (event: RfScanEvent): Error | false => {
          try {
            if (++eventCount > 1024) throw new Error('strict recovery exceeded 1024 events');
            report(`rf-recovery-ten:event:${JSON.stringify({ ...project(event, service), cycle: epoch, runId, nonce: NONCE })}`);
            if (closed) throw new Error('JS callback after resolved native close');
            if (scanRetired && event.kind === 'advertisementObserved') throw new Error('advertisement after scan stop');
            if (oldRouteRetired && (event.connectionId === id(4) || event.gattSessionId === id(6) || event.subscriptionId === id(8))) throw new Error('late old connection route');
            for (const [key, allowed] of Object.entries(expectedRoutes)) {
              const route = event[key as keyof typeof expectedRoutes];
              if (route != null && !allowed.includes(route)) throw new Error(`previous cycle or foreign ${key}: ${route}`);
            }
            if (event.commandId != null && !commandOffsets.some((offset) => event.commandId === id(offset))) throw new Error('previous cycle or foreign command ID');
            if (event.kind === 'connectionStateChanged' && event.commandId == null) {
              passiveDisconnectError(event, id(4), android);
            } else if (event.error != null) throw new Error('unexpected native error');
            else if (event.kind === 'connectionStateChanged') {
              const connecting = event.commandId === id(5) || event.commandId === id(11);
              const disconnecting = event.commandId === id(12);
              if ((!connecting && !disconnecting) || event.connectionId !== (event.commandId === id(5) ? id(4) : id(10)) ||
                  !(connecting ? ['Connecting', 'Connected'] : ['Disconnecting', 'Disconnected']).includes(event.connectionState ?? '')) {
                throw new Error('unexpected connection command result');
              }
            }
            if (event.commandId != null && event.kind === domains.get(event.commandId) &&
                (event.kind !== 'connectionStateChanged' || event.connectionState === (event.commandId === id(12) ? 'Disconnected' : 'Connected'))) seenDomains.add(event.commandId);
            if (event.kind === 'commandCompleted') {
              const command = event.commandId!;
              if (!seenDomains.has(command) || terminals.has(command)) throw new Error(`terminal duplicate or before domain: ${command}`);
              terminals.add(command);
            }
            return false;
          } catch (error) {
            const rejection = asError(error);
            // A callback arriving after the result remains visible to the parent checker;
            // this is JS-route evidence, never a claim that native late ingress was observed.
            if (closed && verdictEmitted) report(`rf-recovery-ten:failed:${rejection.message}`);
            return rejection;
          }
        };
        stream = streamFactory(probe, reject, id(4));
        const assert = (): void => { healthy(); stream!.assertHealthy(); };
        const wait = (predicate: (e: RfScanEvent) => boolean, label: string, timeout = RF_CONNECTION_TIMEOUT_MS): Promise<RfScanEvent> =>
          stream!.waitFor(predicate, timeout, label, () => { assert(); return cancelled(); });
        const terminal = async (offset: number): Promise<RfScanEvent> => {
          const event = await wait((e) => e.kind === 'commandCompleted' && e.commandId === id(offset), `terminal ${id(offset)}`);
          assert(); if (event.error) throw new Error(`command ${id(offset)} failed`); return event;
        };
        const state = (offset: number, route: string, value: string): Promise<RfScanEvent> => wait((e) => e.kind === 'connectionStateChanged' && e.commandId === id(offset) && e.connectionId === route && e.connectionState === value && !e.error, value);
        const snapshot = async (phase: Phase): Promise<void> => {
          assert(); const counts = await probe.snapshotRfResources(); assert();
          report(`rf-recovery-ten:resources:cycle=${epoch}:phase=${phase}:snapshot=${JSON.stringify(counts)}`);
          validateRecoveryResources(counts, phase, android);
        };
        const silence = async (index: number, ms: number, forbidden: (e: RfScanEvent) => boolean): Promise<void> => {
          await pause(ms); assert();
          if (stream!.events.slice(index + 1).some(forbidden)) throw new Error('late event during route silence');
        };
        report('rf-recovery-ten:listener:registered-before-initialize');
        if (probe.initialize(id(0))) throw new Error('initialize dispatch rejected');
        await wait((e) => e.kind === 'adapterSnapshotChanged' && e.adapterState === 'PoweredOn' && e.scanAuthorization === 'Authorized', 'authorized adapter', RF_INITIALIZATION_TIMEOUT_MS);
        await terminal(0); step('initialize', `commandId=${id(0)}`);
        await snapshot('baseline');
        if (android) {
          const now = monotonicMs();
          // Anchor later starts to observed native start, not just JS dispatch:
          // delayed native registration must not compress the next interval.
          const deadline = lastScanStartedMs === undefined ? now + SCAN_QUIET_MS : lastScanStartedMs + SCAN_INTERVAL_MS;
          report(`rf-recovery-ten:cycle:${cycle}:scan-budget-wait:waitMs=${Math.max(0, deadline - now)}:monotonicMs=${now}:policy=android-conservative-test-only-v1:quietMs=30000:minStartIntervalMs=7000`);
          while (monotonicMs() < deadline) {
            assert();
            await pause(Math.min(25, deadline - monotonicMs()));
          }
          assert();
          report(`rf-recovery-ten:cycle:${cycle}:scan-budget-dispatch:monotonicMs=${monotonicMs()}`);
        }
        if (probe.startRfScan(id(2), id(1), service)) throw new Error('scan dispatch rejected');
        const scanStarted = await wait((e) => e.kind === 'scanStarted' && e.commandId === id(2) && e.scanSessionId === id(1), 'scan start', RF_SCAN_TIMEOUT_MS);
        await terminal(2);
        if (android) {
          lastScanStartedMs = monotonicMs();
          report(`rf-recovery-ten:cycle:${cycle}:scan-budget-started:monotonicMs=${lastScanStartedMs}`);
        }
        const advertisement = await wait((e) => e.kind === 'advertisementObserved' && e.scanSessionId === id(1) && e.advertisedServices?.includes(service) === true && (localName === undefined || e.localName === undefined || e.localName === localName), 'fixture advertisement', RF_SCAN_TIMEOUT_MS);
        const peripheral = advertisement.peripheralId;
        if (!isCanonicalU64(peripheral)) throw new Error('invalid peripheral ID');
        if (probe.stopRfScan(id(3), id(1))) throw new Error('stop dispatch rejected');
        const stopped = await wait((e) => e.kind === 'scanStopped' && e.commandId === id(3) && e.scanSessionId === id(1), 'scan stop');
        await terminal(3);
        scanRetired = true;
        await silence(stream.events.indexOf(stopped), 2000, (e) => e.kind === 'advertisementObserved');
        step('scan', `scanSessionId=${id(1)}:startCommandId=${scanStarted.commandId}:stopCommandId=${id(3)}:peripheralId=${peripheral}:serviceUuid=${service}:silenceMs=2000`);
        const connect = async (offset: number, connection: string): Promise<void> => {
          if (probe.connectRfPeripheral(id(offset), connection, peripheral!)) throw new Error('connect dispatch rejected');
          await state(offset, connection, 'Connecting'); await state(offset, connection, 'Connected'); await terminal(offset);
        };
        const discover = async (offset: number, connection: string, session: string) => {
          if (probe.discoverRfGatt(id(offset), connection, session)) throw new Error('GATT dispatch rejected');
          const event = await wait((e) => e.kind === 'gattDiscovered' && e.commandId === id(offset) && e.connectionId === connection && e.gattSessionId === session, 'GATT database');
          const database = validateRfGattDatabase(event, service, connection);
          await terminal(offset);
          step(offset === 7 ? 'gatt' : 'recovery-gatt', `commandId=${id(offset)}:connectionId=${connection}:gattSessionId=${session}:serviceCount=${database.serviceCount}:characteristicCount=${database.characteristicCount}:descriptorCount=${database.descriptorCount}`);
          return database;
        };
        await connect(5, id(4)); step('connected', `commandId=${id(5)}:connectionId=${id(4)}:peripheralId=${peripheral}`);
        const database = await discover(7, id(4), id(6));
        const characteristic = database.notificationCharacteristicId;
        if (probe.setRfNotifications(id(9), id(4), id(6), id(8), characteristic, true, 'platform_default')) throw new Error('notification enable rejected');
        const enabled = await wait((e) => e.kind === 'notificationStateChanged' && e.commandId === id(9) && e.connectionId === id(4) && e.gattSessionId === id(6) && e.subscriptionId === id(8) && e.characteristicId === characteristic && e.notificationState === 'Enabled', 'notification enabled');
        if (enabled.actualNotificationMode !== 'Notification') throw new Error('unexpected notification mode');
        await terminal(9);
        step('notification-enable', `commandId=${id(9)}:connectionId=${id(4)}:gattSessionId=${id(6)}:subscriptionId=${id(8)}:characteristicId=${characteristic}:actualMode=Notification`);
        await snapshot('active');
        const value = await wait((e) => e.kind === 'notificationReceived' && e.connectionId === id(4) && e.gattSessionId === id(6) && e.subscriptionId === id(8) && e.characteristicId === characteristic, 'notification value');
        const bytes = new Uint8Array(value.value ?? new ArrayBuffer(0));
        if (bytes.length !== 1 || bytes[0] !== 0xef) throw new Error('notification value mismatch');
        step('notification-value', `connectionId=${id(4)}:gattSessionId=${id(6)}:subscriptionId=${id(8)}:value=ef`);
        const passive = await wait((e) => e.kind === 'connectionStateChanged' && e.commandId == null && e.connectionId === id(4), 'native passive disconnect');
        const passiveError = passiveDisconnectError(passive, id(4), android);
        if (stream.events.indexOf(passive) <= stream.events.indexOf(value)) throw new Error('passive disconnect before notification value');
        oldRouteRetired = true;
        step('passive-disconnect', `commandId=none:connectionId=${id(4)}:error=${passiveError}`);
        const oldRoute = (e: RfScanEvent): boolean => e.connectionId === id(4) || e.gattSessionId === id(6) || e.subscriptionId === id(8) || e.kind === 'commandCompleted';
        await silence(stream.events.indexOf(passive), 1000, oldRoute);
        step('old-route-silence', `connectionId=${id(4)}:gattSessionId=${id(6)}:subscriptionId=${id(8)}:observationMs=1000`);
        await snapshot('passive');
        await barrier.wait(epoch, cancelled, assert);
        await connect(11, id(10)); step('recovery-connected', `commandId=${id(11)}:connectionId=${id(10)}:peripheralId=${peripheral}`);
        const recovered = await discover(13, id(10), id(14));
        if (probe.readRfCharacteristic(id(15), id(10), id(14), recovered.readWriteCharacteristicId)) throw new Error('recovery read rejected');
        const read = await wait((e) => e.kind === 'characteristicRead' && e.commandId === id(15) && e.connectionId === id(10) && e.gattSessionId === id(14) && e.characteristicId === recovered.readWriteCharacteristicId, 'recovery read');
        const readBytes = new Uint8Array(read.value ?? new ArrayBuffer(0));
        if (readBytes.length !== 1 || readBytes[0] !== 0) throw new Error('recovery RW initial read mismatch');
        await terminal(15);
        step('recovery-read', `commandId=${id(15)}:connectionId=${id(10)}:gattSessionId=${id(14)}:characteristicId=${recovered.readWriteCharacteristicId}:value=00`);
        if (probe.disconnectRfPeripheral(id(12), id(10))) throw new Error('recovery disconnect rejected');
        await state(12, id(10), 'Disconnected'); const end = await terminal(12);
        await silence(stream.events.indexOf(end), 1000, (e) => oldRoute(e) || e.connectionId === id(10) || e.gattSessionId === id(14));
        step('recovery-disconnected', `commandId=${id(12)}:connectionId=${id(10)}:observationMs=1000`);
        await snapshot('recovery-disconnected');
        if (terminals.size !== commandOffsets.length || commandOffsets.some((offset) => !terminals.has(id(offset)))) throw new Error('incomplete terminal ledger');
        for (const kind of ['scanStarted', 'scanStopped', 'notificationStateChanged', 'notificationReceived']) {
          if (stream.events.filter((e) => e.kind === kind).length !== 1) throw new Error(`duplicate ${kind}`);
        }
        if (stream.events.filter((e) => e.kind === 'connectionStateChanged' && e.commandId == null).length !== 1) throw new Error('duplicate passive disconnect');
        for (const [offset, route] of [[5, id(4)], [11, id(10)]] as const) {
          for (const connectionState of ['Connecting', 'Connected']) {
            if (stream.events.filter((e) => e.kind === 'connectionStateChanged' && e.commandId === id(offset) && e.connectionId === route && e.connectionState === connectionState).length !== 1) throw new Error('duplicate or missing connection domain');
          }
        }
        for (const offset of [7, 13, 15, 12]) {
          if (stream.events.filter((e) => e.kind === domains.get(id(offset)) && e.commandId === id(offset) && (offset !== 12 || e.connectionState === 'Disconnected')).length !== 1) throw new Error('duplicate recovery domain');
        }
        assert();
      } catch (error) { cycleFailure = asError(error); }
      finally {
        try {
          stream?.assertHealthy();
        } catch (error) { cycleFailure ??= asError(error); }
        try {
          // Keep the listener guard armed across actual native disposal, then sample its retained close evidence.
          await probe.close(); closed = true;
          stream?.assertHealthy(); healthy();
          const counts = await probe.snapshotRfResources();
          stream?.assertHealthy(); healthy();
          report(`rf-recovery-ten:resources:cycle=${cycle}:phase=closed:snapshot=${JSON.stringify(counts)}`);
          validateRecoveryResources(counts, 'closed', android);
        } catch (error) { cycleFailure ??= asError(error); }
        finally { stream?.remove(); if (stream) retired.push(stream); }
      }
      if (cycleFailure) throw cycleFailure;
      step('cycle', `scanSessionId=${id(1)}:connectionId=${id(4)}:recoveryConnectionId=${id(10)}`);
      report(`rf-recovery-ten:cycle:${cycle}:passed`);
    }
    healthy();
    report('rf-recovery-ten:close:passed:listener-removed=true:probe-closed=true');
  } catch (error) {
    failure = asError(error);
    report(`rf-recovery-ten:cycle:${cycle}:failed:${error instanceof Error ? error.message : String(error)}`);
  } finally {
    barrier?.remove();
    signalGuard?.useRecovery(undefined);
    if (started) report(`rf-recovery-ten:runtime:runId=${runId}:nonce=${NONCE}:phase=close`);
  }
  report(failure ? `rf-recovery-ten:failed:${failure instanceof Error ? failure.message : String(failure)}` : `rf-recovery-ten:passed:cycles=10:runId=${runId}`);
  verdictEmitted = true;
  return !failure;
}
