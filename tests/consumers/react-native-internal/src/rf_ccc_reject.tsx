import React, { useEffect, useRef, useState } from 'react';
import { Platform, Text, View } from 'react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { DispatchError, RfResourceSnapshot, RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import { isCanonicalU64, RF_CONNECTION_TIMEOUT_MS, RF_INITIALIZATION_TIMEOUT_MS, RF_POST_DISCONNECT_OBSERVATION_MS, RF_SCAN_TIMEOUT_MS, validateRfGattDatabase } from './rf_contract';
import { RF_RESOURCE_FIELDS, validateRecoveryResources } from './rf_recovery_ten';
import { RF_BOOT_NONCE } from './rf_runtime';
import { parseRfControl, RfSignalBarrier, type RfSignalPhase } from './rf_signal';
import { projectAttRejectEvent } from './rf_write_reject';
import { useBeginRun, useRunReporter } from './rf_lab_session';
import { styles } from './rf_lab_screen';

type Probe = ReturnType<typeof createBleHubProbe>;
export type CccRejectMode = 'rf-ccc-enable-reject' | 'rf-ccc-disable-reject';
export interface CccRejectProfile { rfServiceUuid?: string; rfExpectedLocalName?: string; rfCccEnableRejectControl?: string; rfCccDisableRejectControl?: string }
type Profile = CccRejectProfile;
export type CccRejectRunner = (probe: Probe, profile: Profile, report: (line: string) => void, cancelled: () => boolean) => Promise<void>;
interface Expected {
  domains: readonly string[];
  optional?: readonly string[];
  connectionId?: string;
  gattSessionId?: string;
  characteristicId?: string;
  subscriptionId?: string;
  scanSessionId?: string;
  state?: 'Enabled' | 'Disabled';
}
const ENABLE_ID = {
  initialize: '33000', scan: '33001', start: '33002', stop: '33003', connection: '33004', connect: '33005',
  disconnect: '33006', gatt: '33007', discover: '33008', failedSub: '33009', rejected: '33010', queued: '33011',
  retrySub: '33012', recovery: '33013', disable: '33014', postDisable: '33015', endSilence: '33016', baseline: '33017',
} as const;
export const CCC_ENABLE_REJECT_STEPS = ['initialize', 'scan', 'connected', 'gatt', 'baseline-read', 'enable-dispatched',
  'queued-read-dispatched', 'enable-rejected', 'queued-read', 'recovery-enabled', 'recovery-notified', 'disabled',
  'post-disable-read', 'silence', 'end-silence-read', 'disconnected', 'close'] as const;
const DISABLE_ID: Record<keyof typeof ENABLE_ID, string> = {
  initialize: '34000', scan: '34001', start: '34002', stop: '34003', connection: '34004', connect: '34005',
  disconnect: '34006', gatt: '34007', discover: '34008', failedSub: '34009', recovery: '34010', rejected: '34011',
  queued: '34012', retrySub: '34009', disable: '34013', postDisable: '34014', endSilence: '34015', baseline: '34016',
};
type CccIds = Record<keyof typeof ENABLE_ID, string>;
export const CCC_DISABLE_REJECT_STEPS = ['initialize', 'scan', 'connected', 'gatt', 'baseline-read', 'baseline-enabled',
  'baseline-notified', 'disable-dispatched', 'queued-read-dispatched', 'disable-rejected', 'queued-read', 'continued-notified',
  'disabled', 'post-disable-read', 'silence', 'end-silence-read', 'disconnected', 'close'] as const;
const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
const detail = (error: unknown): string => error instanceof Error ? error.message : String(error);
const attempts = new Set<string>();
function launch(profile: Profile, mode: CccRejectMode) {
  const control = parseRfControl(mode === 'rf-ccc-disable-reject' ? profile.rfCccDisableRejectControl : profile.rfCccEnableRejectControl, mode);
  if (!profile.rfServiceUuid || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(profile.rfServiceUuid) ||
      (mode === 'rf-ccc-enable-reject' && profile.rfExpectedLocalName === undefined) ||
            (profile.rfExpectedLocalName !== undefined && (!profile.rfExpectedLocalName.trim() || profile.rfExpectedLocalName.length > 128))) {
    throw new Error('CCC reject requires canonical service UUID and frozen local name');
  }
  return { control, service: profile.rfServiceUuid, name: profile.rfExpectedLocalName };
}
export function cccRejectSession(raw: string | undefined, mode: CccRejectMode): string | undefined {
  try { return parseRfControl(raw, mode).session; } catch { return undefined; }
}

/** CCC-local ledger: only explicitly ready value windows may use the successful enable route. */
class CccRejectStream {
  private readonly commands = new Map<string, Expected>();
  private readonly terminals = new Set<string>();
  private readonly domains = new Set<string>();
  private readonly events: RfScanEvent[] = [];
  private readonly consumed = new Set<number>();
  private failure?: Error;
  private received = 0;
  private scanning = false;
  private disconnected = false;
  rejectRequested = false;
  private valueByte?: number;
  private notifications = 0;
  allowValue(byte: number): void { this.assertHealthy(); this.valueByte = byte; }
  private retryRoute?: Expected;
  constructor(private readonly probe: Probe, report: (line: string) => void, session: string, service: string,
    private readonly healthy: () => void, private readonly ID: CccIds, private readonly mode: CccRejectMode) {
    probe.setRfScanListener((event) => {
      try {
        if (this.received++ >= 256) throw new Error('CCC event ledger overflow');
        report(projectAttRejectEvent(event, session, service, mode));
        this.validate(event);
        this.events.push(event);
      } catch (error) { this.failure ??= new Error(detail(error)); }
    });
  }
  expect(id: string, expected: Expected): void {
    this.assertHealthy();
    if (this.commands.has(id)) throw new Error(`command ID reused: ${id}`);
    this.commands.set(id, expected);
    if (id === this.ID.start) this.scanning = true;
    if (id === this.ID.recovery) this.retryRoute = expected;
  }
  private route(event: RfScanEvent, expected: Expected): void {
    for (const key of ['connectionId', 'gattSessionId', 'characteristicId', 'subscriptionId', 'scanSessionId'] as const) {
      if ((event.kind !== 'commandCompleted' && event[key] !== expected[key]) ||
          (event[key] != null && event[key] !== expected[key])) throw new Error(`${event.kind} ${key} mismatch`);
    }
  }
  private validate(event: RfScanEvent): void {
    const ID = this.ID;
    if (event.kind === 'advertisementObserved') {
      if (!this.scanning || event.commandId != null || event.error || event.scanSessionId !== ID.scan ||
          !isCanonicalU64(event.peripheralId) || event.connectionId != null || event.gattSessionId != null ||
          event.characteristicId != null || event.subscriptionId != null) throw new Error('foreign advertisement route');
      return;
    }
    if (event.kind === 'notificationReceived') {
      if (!this.retryRoute || this.valueByte === undefined || this.disconnected || this.commands.has(ID.disable) ||
          !this.terminals.has(ID.recovery) || !this.domains.has(`${ID.recovery}:notificationStateChanged`) ||
          event.commandId != null || event.error) throw new Error('early, duplicate, disabled or failed-subscription value');
      this.route(event, this.retryRoute);
      const bytes = new Uint8Array(event.value ?? new ArrayBuffer(0));
      if (bytes.length !== 1 || bytes[0] !== this.valueByte) throw new Error('unexpected CCC notification value/phase');
      this.valueByte = undefined;
      this.notifications += 1;
      return;
    }
    const id = event.kind === 'adapterSnapshotChanged' && event.commandId == null ? ID.initialize : event.commandId ?? '';
    const expected = this.commands.get(id);
    if (!expected) throw new Error(`unknown command/domain: ${event.kind}:${id}`);
    this.route(event, expected);
    if ((id === ID.rejected || id === ID.queued) && !this.rejectRequested) throw new Error('held operation completed before REJECT readiness');
    if (id === ID.queued && !this.terminals.has(ID.rejected)) throw new Error('queued read preceded failed CCC terminal');
    if (event.kind === 'commandCompleted') {
      if (this.terminals.has(id)) throw new Error(`duplicate terminal: ${id}`);
      if (id === ID.rejected) {
        const native = event.error?.platformDetails;
        if (event.error?.category !== 'gatt' || native?.code !== '14' ||
            native.platform !== (Platform.OS === 'android' ? 'platform_android' : 'platform_apple') ||
            native.name !== (Platform.OS === 'android' ? 'BluetoothGatt' : 'CBATTErrorDomain')) throw new Error('CCC rejection lacks exact native ATT error 14');
      } else if (event.error) throw new Error(`unexpected terminal error: ${id}`);
      this.terminals.add(id);
      return;
    }
    const domain = event.kind === 'connectionStateChanged' ? `${event.kind}:${event.connectionState}` : event.kind;
    if (this.disconnected || event.error || ![...expected.domains, ...(expected.optional ?? [])].includes(domain)) throw new Error(`unexpected domain: ${event.kind}:${id}`);
    const key = `${id}:${domain}`;
    if (this.domains.has(key)) throw new Error(`duplicate domain: ${key}`);
    this.domains.add(key);
    if (event.kind === 'adapterSnapshotChanged' && (event.adapterState !== 'PoweredOn' || event.scanAuthorization !== 'Authorized')) throw new Error('adapter must be PoweredOn and Authorized');
    if (event.kind === 'characteristicRead') {
      const bytes = new Uint8Array(event.value ?? new ArrayBuffer(0));
      if (bytes.length !== 1 || bytes[0] !== 0) throw new Error(`independent read must be 00: ${id}`);
    }
    if (event.kind === 'notificationStateChanged' && (event.notificationState !== expected.state ||
        (expected.state === 'Enabled' ? event.actualNotificationMode !== 'Notification' : event.actualNotificationMode != null))) throw new Error('unexpected notification state/mode');
    if (event.kind === 'scanStopped') this.scanning = false;
    if (domain === 'connectionStateChanged:Disconnected') this.disconnected = true;
  }
  assertHealthy(): void { if (this.failure) throw this.failure; this.healthy(); }
  assertComplete(): void {
    this.assertHealthy();
    for (const [id, expected] of this.commands) {
      if (!this.terminals.has(id) || expected.domains.some((domain) => !this.domains.has(`${id}:${domain}`))) throw new Error(`missing terminal/domain: ${id}`);
    }
    if (this.notifications !== (this.mode === 'rf-ccc-disable-reject' ? 2 : 1)) throw new Error('missing CCC notification');
  }
  async wait(predicate: (event: RfScanEvent) => boolean, timeout = RF_CONNECTION_TIMEOUT_MS): Promise<RfScanEvent> {
    const deadline = performance.now() + timeout;
    while (true) {
      this.assertHealthy();
      const index = this.events.findIndex((event, i) => !this.consumed.has(i) && predicate(event));
      if (index >= 0) { this.consumed.add(index); return this.events[index]; }
      if (performance.now() >= deadline) throw new Error('JS wait expired; no native outcome inferred');
      await pause(10);
    }
  }
  remove(): void { this.probe.removeRfScanListener(); this.assertHealthy(); }
}

function failedCccResources(snapshot: RfResourceSnapshot, disable: boolean): void {
  if (Object.keys(snapshot).length !== 17 || RF_RESOURCE_FIELDS.some((key) => !Number.isSafeInteger(snapshot[key]) || snapshot[key] < 0 || Object.is(snapshot[key], -0))) throw new Error('invalid 17-field resource snapshot');
  for (const [key, count] of [['connections', 1], ['subscriptions', disable ? 1 : 0], ['pendingIo', 0], ['watchdogs', 0]] as const) {
    if (snapshot[key] !== count) throw new Error(`failed-CCC resource ${key}: expected ${count}`);
  }
  // Apple callback identity barriers legitimately survive the failed CCC callback until close.
}

export async function runRfCccReject(probe: Probe, profile: Profile, report: (line: string) => void,
  cancelled: () => boolean, mode: CccRejectMode): Promise<void> {
  const PREFIX = mode;
  const disable = mode === 'rf-ccc-disable-reject';
  const ID: CccIds = disable ? DISABLE_ID : ENABLE_ID;
  let stream: CccRejectStream | undefined;
  let barrier: RfSignalBarrier | undefined;
  let failure: unknown;
  let session: string | undefined;
  try {
    const config = launch(profile, mode);
    session = config.control.session;
    const attempt = `${session}:${config.control.token}`;
    if (attempts.has(attempt)) throw new Error('CCC reject attempt already used; new host launch required');
    attempts.add(attempt);
    const nonce = RF_BOOT_NONCE;
    const healthy = (): void => {
      if (cancelled()) throw new Error('CCC reject cancelled');
      if (RF_BOOT_NONCE !== nonce) throw new Error('JS runtime identity changed');
      barrier?.assertHealthy();
    };
    report(`${PREFIX}:runtime:session=${session}:nonce=${nonce}:phase=start`);
    barrier = new RfSignalBarrier(config.control, report);
    stream = new CccRejectStream(probe, report, session, config.service, healthy, ID, mode);
    const ledger = stream;
    const signals = barrier;
    const step = (name: string, route: string): void => report(`${PREFIX}:step:${name}:passed:${route}`);
    const dispatch = (id: string, expected: Expected, submit: () => DispatchError | undefined): void => {
      ledger.expect(id, expected);
      const error = submit();
      if (error) throw new Error(`dispatch rejected: ${id}:${error.category}`);
      ledger.assertHealthy();
    };
    const wait = (id: string, kind: string, timeout = RF_CONNECTION_TIMEOUT_MS) => ledger.wait((event) => event.kind === kind &&
      (event.commandId === id || kind === 'adapterSnapshotChanged' && event.commandId == null), timeout);
    const settle = async (id: string, kind?: string): Promise<RfScanEvent | undefined> => {
      const event = kind ? await wait(id, kind) : undefined;
      await wait(id, 'commandCompleted');
      return event;
    };
    const signal = async (phase: RfSignalPhase, request: Parameters<RfSignalBarrier['ready']>[0]): Promise<void> => {
      signals.expect(phase); signals.ready(request);
      await signals.wait(cancelled, () => ledger.assertHealthy());
    };
    dispatch(ID.initialize, { domains: ['adapterSnapshotChanged'] }, () => probe.initialize(ID.initialize));
    await wait(ID.initialize, 'adapterSnapshotChanged', RF_INITIALIZATION_TIMEOUT_MS);
    await wait(ID.initialize, 'commandCompleted', RF_INITIALIZATION_TIMEOUT_MS);
    step('initialize', `commandId=${ID.initialize}`);
    dispatch(ID.start, { domains: ['scanStarted'], scanSessionId: ID.scan }, () => probe.startRfScan(ID.start, ID.scan, config.service));
    await settle(ID.start, 'scanStarted');
    const advertisement = await ledger.wait((event) => event.kind === 'advertisementObserved' && event.advertisedServices?.includes(config.service) === true &&
      (event.localName == null || event.localName === config.name), RF_SCAN_TIMEOUT_MS);
    dispatch(ID.stop, { domains: ['scanStopped'], scanSessionId: ID.scan }, () => probe.stopRfScan(ID.stop, ID.scan));
    await settle(ID.stop, 'scanStopped');
    step('scan', `commandId=${ID.stop}:scanSessionId=${ID.scan}:peripheralId=${advertisement.peripheralId}`);
    const connection = { connectionId: ID.connection };
    dispatch(ID.connect, { domains: ['connectionStateChanged:Connecting', 'connectionStateChanged:Connected'], ...connection }, () => probe.connectRfPeripheral(ID.connect, ID.connection, advertisement.peripheralId!));
    await ledger.wait((event) => event.commandId === ID.connect && event.connectionState === 'Connected');
    await wait(ID.connect, 'commandCompleted');
    step('connected', `commandId=${ID.connect}:connectionId=${ID.connection}:peripheralId=${advertisement.peripheralId}`);
    dispatch(ID.discover, { domains: ['gattDiscovered'], ...connection, gattSessionId: ID.gatt }, () => probe.discoverRfGatt(ID.discover, ID.connection, ID.gatt));
    const database = validateRfGattDatabase((await settle(ID.discover, 'gattDiscovered'))!, config.service, ID.connection);
    const characteristic = database.notificationCharacteristicId;
    const readCharacteristic = database.readWriteCharacteristicId;
    const route = { ...connection, gattSessionId: ID.gatt, characteristicId: characteristic };
    const routeText = `connectionId=${ID.connection}:gattSessionId=${ID.gatt}:characteristicId=${characteristic}`;
    const readText = `connectionId=${ID.connection}:gattSessionId=${ID.gatt}:characteristicId=${readCharacteristic}`;
    const subText = (subscription: string): string => `${routeText}:subscriptionId=${subscription}`;
    step('gatt', `commandId=${ID.discover}:${routeText}:readCharacteristicId=${readCharacteristic}`);
    const read = (id: string): void => dispatch(id, { domains: ['characteristicRead'], ...route, characteristicId: readCharacteristic }, () => probe.readRfCharacteristic(id, ID.connection, ID.gatt, readCharacteristic));
    const notifications = (id: string, subscription: string, enabled: boolean, rejected = false): void => dispatch(id,
      { domains: rejected ? [] : ['notificationStateChanged'], ...route, subscriptionId: subscription, state: enabled ? 'Enabled' : 'Disabled' },
      () => probe.setRfNotifications(id, ID.connection, ID.gatt, subscription, characteristic, enabled, 'platform_default'));
    read(ID.baseline); await settle(ID.baseline, 'characteristicRead');
    step('baseline-read', `commandId=${ID.baseline}:${readText}:value=00`);
    if (disable) {
      notifications(ID.recovery, ID.retrySub, true);
      await settle(ID.recovery, 'notificationStateChanged');
      step('baseline-enabled', `commandId=${ID.recovery}:${subText(ID.retrySub)}:actualMode=Notification`);
      signals.expect('baseline-value-sent'); ledger.allowValue(0xef); signals.ready('baseline-enabled');
      await signals.wait(cancelled, () => ledger.assertHealthy());
      await ledger.wait((event) => event.kind === 'notificationReceived');
      step('baseline-notified', `${subText(ID.retrySub)}:value=ef`);
    }
    await signal('armed', 'arm');
    const rejectDeadline = performance.now() + 6500;
    const budget = (): number => {
      const remaining = rejectDeadline - performance.now();
      if (remaining <= 0) throw new Error('CCC reject exceeded prompt REJECT budget (<7s)');
      return remaining;
    };
    signals.expect('held');
    notifications(ID.rejected, ID.failedSub, !disable, true);
    step(disable ? 'disable-dispatched' : 'enable-dispatched', `commandId=${ID.rejected}:${subText(ID.failedSub)}:requestedMode=platform_default`);
    signals.ready(disable ? 'disable-dispatched' : 'enable-dispatched');
    await signals.wait(cancelled, () => { ledger.assertHealthy(); budget(); }, budget());
    read(ID.queued);
    step('queued-read-dispatched', `commandId=${ID.queued}:${readText}`);
    signals.expect('rejected'); ledger.rejectRequested = true; signals.ready('reject');
    await signals.wait(cancelled, () => { ledger.assertHealthy(); budget(); }, budget());
    budget();
    await settle(ID.rejected);
    step(disable ? 'disable-rejected' : 'enable-rejected', `commandId=${ID.rejected}:${subText(ID.failedSub)}:error=gatt:code=14`);
    await settle(ID.queued, 'characteristicRead');
    step('queued-read', `commandId=${ID.queued}:${readText}:value=00`);
    const warm = await probe.snapshotRfResources(); ledger.assertHealthy(); failedCccResources(warm, disable);
    report(`${PREFIX}:resources:phase=failed-${disable ? 'disable' : 'enable'}:snapshot=${JSON.stringify(warm)}`);
    if (!disable) {
      notifications(ID.recovery, ID.retrySub, true);
      await settle(ID.recovery, 'notificationStateChanged');
      step('recovery-enabled', `commandId=${ID.recovery}:${subText(ID.retrySub)}:actualMode=Notification`);
    }
    signals.expect(disable ? 'continued-value-sent' : 'value-sent'); ledger.allowValue(disable ? 0xe1 : 0xef);
    signals.ready(disable ? 'reject-observed' : 'recovery-enabled');
    await signals.wait(cancelled, () => ledger.assertHealthy());
    await ledger.wait((event) => event.kind === 'notificationReceived');
    step(disable ? 'continued-notified' : 'recovery-notified', `${subText(ID.retrySub)}:value=${disable ? 'e1' : 'ef'}`);
    await signal(disable ? 'retry-allowed' : 'disable-allowed', disable ? 'continued-notified' : 'recovery-notified');
    notifications(ID.disable, ID.retrySub, false);
    await settle(ID.disable, 'notificationStateChanged');
    step('disabled', `commandId=${ID.disable}:${subText(ID.retrySub)}`);
    read(ID.postDisable); await settle(ID.postDisable, 'characteristicRead');
    step('post-disable-read', `commandId=${ID.postDisable}:${readText}:value=00`);
    await signal('silence-started', 'disabled');
    const started = performance.now();
    while (performance.now() - started < 1000) { ledger.assertHealthy(); await pause(25); }
    step('silence', `${subText(ID.retrySub)}:elapsedMs=${Math.floor(performance.now() - started)}`);
    read(ID.endSilence); await settle(ID.endSilence, 'characteristicRead');
    step('end-silence-read', `commandId=${ID.endSilence}:${readText}:value=00`);
    await signal('close-allowed', 'silence-done');
    dispatch(ID.disconnect, { domains: ['connectionStateChanged:Disconnected'], optional: ['connectionStateChanged:Disconnecting'], ...connection }, () => probe.disconnectRfPeripheral(ID.disconnect, ID.connection));
    await ledger.wait((event) => event.commandId === ID.disconnect && event.connectionState === 'Disconnected');
    await wait(ID.disconnect, 'commandCompleted');
    step('disconnected', `commandId=${ID.disconnect}:connectionId=${ID.connection}`);
    const until = performance.now() + RF_POST_DISCONNECT_OBSERVATION_MS;
    while (performance.now() < until) { ledger.assertHealthy(); await pause(25); }
    ledger.assertComplete();
  } catch (error) { failure = error; }
  finally {
    const record = (error: unknown): void => { failure = failure === undefined ? error : new Error(`${detail(failure)}; cleanup: ${detail(error)}`); };
    try { await probe.close(); stream?.assertHealthy(); barrier?.assertHealthy(); } catch (error) { record(error); }
    try {
      const snapshot = await probe.snapshotRfResources();
      stream?.assertHealthy(); barrier?.assertHealthy();
      validateRecoveryResources(snapshot, 'closed', Platform.OS === 'android');
      report(`${PREFIX}:resources:phase=closed:snapshot=${JSON.stringify(snapshot)}`);
    } catch (error) { record(error); }
    for (const remove of [() => stream?.remove(), () => barrier?.remove()]) { try { remove(); } catch (error) { record(error); } }
  }
  if (cancelled() && failure === undefined) failure = new Error('CCC reject cancelled');
  if (failure !== undefined) { report(`${PREFIX}:failed:${detail(failure)}`); throw failure; }
  report(`${PREFIX}:step:close:passed:session=${session}`);
  report(`${PREFIX}:runtime:session=${session}:nonce=${RF_BOOT_NONCE}:phase=close`);
  report(`${PREFIX}:passed`);
}

export function RfCccRejectLab({ mode, ...profile }: Profile & { mode: CccRejectMode }): React.JSX.Element {
  const PREFIX = mode;
  const operation = mode === 'rf-ccc-disable-reject' ? 'disable' : 'enable';
  const present = useRunReporter();
  const begin = useBeginRun();
  const [status, setStatus] = useState('ready');
  const started = useRef(false);
  useEffect(() => {
    let active = true;
    const token = { cancelled: false };
    const report = (line: string): void => { console.log(`BLEHUB_NITRO_RUNTIME ${line}`); present(line); };
    void Promise.resolve().then(async () => {
      if (!active || started.current) return;
      started.current = true; begin(); setStatus('running');
      let probe: Probe | undefined;
      try {
        launch(profile, mode); probe = createBleHubProbe();
        await runRfCccReject(probe, profile, report, () => token.cancelled, mode);
        if (active) setStatus('passed');
      } catch (error) {
        if (!probe) report(`${PREFIX}:failed:${detail(error)}`);
        if (active) setStatus('failed');
      }
    });
    return () => { active = false; token.cancelled = true; };
  }, []);
  return <View testID={`${PREFIX}-root`} style={styles.root}>
    <Text style={styles.title}>CCC {operation} REJECT</Text>
    <Text testID={`${PREFIX}-status`} style={styles.statusValue}>{status}</Text>
    <Text style={styles.subtitle}>Host-controlled CCC rejection, exact Notification continuity/recovery and post-disable silence. App checks are not the host verdict.</Text>
  </View>;
}
