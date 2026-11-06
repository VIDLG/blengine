import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Linking, Platform, Pressable, Text, View } from 'react-native';
import { styles } from './rf_lab_screen';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { RfResourceSnapshot, RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import { RF_BOOT_NONCE } from './rf_runtime';
import { isCanonicalU64, validateRfGattDatabase } from './rf_contract';
import { useRunReporter } from './rf_lab_session';

type Probe = ReturnType<typeof createBleHubProbe>;
type Report = (line: string) => void;
export type AdapterTransitionFlow = (
  mode: 'rf-io', probe: Probe, service: string | undefined, report: Report,
  cancelled: () => boolean, expectedLocalName?: string,
) => Promise<void>;
interface Profile {
  rfAdapterRunId?: string;
  rfAdapterMode?: 'startup-off' | 'active-loss';
  rfAdapterNativeIdentity?: string;
  rfServiceUuid?: string;
  rfExpectedLocalName?: string;
}
const PREFIX = 'rf-adapter-transition';
const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;
const WAIT_MS = 30_000;
const attempts = new Set<string>();
const RESOURCE_FIELDS = [
  'activeScans', 'connections', 'gattAttributes', 'subscriptions', 'pendingIo', 'watchdogs',
  'callbackRoutes', 'ingressRoots', 'adapterDelegates', 'peerCacheEntries', 'backendLeases',
  'hubRoots', 'handoffRoots', 'handoffPending', 'listeners', 'workerThreads', 'lifecycleOwners',
] as const satisfies readonly (keyof RfResourceSnapshot)[];
const pause = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 25));
const detail = (error: unknown): string => error instanceof Error ? error.message : String(error);
const ACTIVE_AUDIT_BYTES = 3500; // Includes the BLEHUB prefix; leave headroom for Android's tag/payload overhead.

interface AdapterNativeIdentity {
  run_id: string;
  adapter_mode: 'startup-off';
  pid: number;
  boot_id: string;
  bundle_id: string;
  executable: string;
  source: 'launch';
}

function parseAdapterNativeIdentity(profile: Readonly<Profile>): AdapterNativeIdentity {
  try {
    const identity = JSON.parse(profile.rfAdapterNativeIdentity ?? 'null') as Partial<AdapterNativeIdentity> | null;
    const keys = ['run_id', 'adapter_mode', 'pid', 'boot_id', 'bundle_id', 'executable', 'source'];
    if (!identity || typeof identity !== 'object' || Array.isArray(identity) ||
        Object.keys(identity).length !== keys.length || !keys.every((key) => Object.hasOwn(identity, key)) ||
        typeof identity.run_id !== 'string' || !UUID.test(identity.run_id) || identity.run_id !== profile.rfAdapterRunId ||
        identity.adapter_mode !== 'startup-off' || identity.source !== 'launch' ||
        typeof identity.pid !== 'number' || !Number.isSafeInteger(identity.pid) || identity.pid <= 0 ||
        typeof identity.boot_id !== 'string' || !UUID.test(identity.boot_id) ||
        identity.bundle_id !== 'org.reactjs.native.example.BleHubNitroHost' ||
        typeof identity.executable !== 'string' || !/^file:\/\/\/[^\s?#\\]+$/.test(identity.executable) ||
        identity.executable.endsWith('/') || /[\u0000-\u001f\u007f]/.test(decodeURI(identity.executable))) {
      throw new Error('invalid identity');
    }
    return identity as AdapterNativeIdentity;
  } catch {
    throw new Error('invalid iOS adapter native identity');
  }
}

/** Audit-only projection, like watchdog logging; never replace the runtime event.
 * active-gatt-canonical-v1 retains the complete canonical subtree and full-database
 * counts, not the detailed GAP/GATT/Device Information service lists.
 */
export function projectActiveEvent(event: RfScanEvent, service: string, connectionId = '32005'): object {
  if (event.kind !== 'gattDiscovered') return event;
  validateRfGattDatabase(event, service, connectionId);
  const services = event.gattServices!;
  const canonical = services.find((candidate) => candidate.uuid === service)!;
  return {
    ...event,
    auditSchema: 'active-gatt-canonical-v1',
    gattSummary: {
      serviceCount: services.length,
      characteristicCount: services.reduce((count, entry) => count + entry.characteristics.length, 0),
      descriptorCount: services.reduce((count, entry) => count + entry.characteristics.reduce((total, characteristic) => total + characteristic.descriptors.length, 0), 0),
    },
    gattServices: [{
      id: canonical.id, uuid: canonical.uuid, primary: canonical.primary,
      characteristics: canonical.characteristics.map((characteristic) => ({
        id: characteristic.id, uuid: characteristic.uuid, readable: characteristic.readable,
        writableWithResponse: characteristic.writableWithResponse,
        writableWithoutResponse: characteristic.writableWithoutResponse,
        notifiable: characteristic.notifiable, indicatable: characteristic.indicatable,
        descriptors: characteristic.descriptors.map((descriptor) => ({ id: descriptor.id, uuid: descriptor.uuid })),
      })),
    }],
  };
}

/** Finite active-read branch; only the host/provider can authorize the real radio fault. */
async function runActiveLoss(
  probe: Probe, service: string, name: string, report: Report, healthy: () => void,
  awaitArm: () => Promise<void>, retired: () => boolean, fail: (error: unknown) => void,
): Promise<void> {
  const events: RfScanEvent[] = [];
  const consumed = new Set<RfScanEvent>();
  const domains = new Set<string>();
  const terminals = new Map<string, string | undefined>();
  const expected = new Map<string, readonly string[]>();
  let accepted = 0;
  let passive = false;
  let settled = false;
  let failure: Error | undefined;
  const check = (): void => { healthy(); if (failure) throw failure; };
  probe.setRfScanListener((event) => {
    try {
      const line = `${PREFIX}:active:event:${JSON.stringify(projectActiveEvent(event, service))}`;
      // Count UTF-8 bytes without a Node Buffer/TextEncoder dependency in Hermes.
      if (encodeURIComponent(`BLEHUB_NITRO_RUNTIME ${line}`).replace(/%[0-9A-F]{2}|./g, 'x').length > ACTIVE_AUDIT_BYTES) {
        throw new Error('active audit event exceeds 3500-byte log budget; no truncated event emitted');
      }
      report(line);
      check();
      if (retired() || settled) throw new Error('old active callback after settlement/close');
      if (events.length >= 128) throw new Error('active event ledger overflow');
      const id = event.commandId;
      if (event.kind === 'commandCompleted') {
        if (!id || !expected.has(id) || terminals.has(id)) throw new Error('unknown/duplicate terminal');
        const category = event.error?.category;
        if (id === '32008' || id === '32009') {
          if (accepted !== 2 || !['connection', 'gatt'].includes(category ?? '') ||
              event.error?.platformDetails?.platform !== 'platform_android') {
            throw new Error('read succeeded, failed before queued acceptance, or watchdog-first');
          }
          if (id === '32009' && (!terminals.has('32008') || terminals.get('32008') === 'connection' && category !== 'connection')) {
            throw new Error('invalid queued retirement order/category');
          }
        } else if (category || expected.get(id)!.some((key) => !domains.has(`${id}:${key}`))) {
          throw new Error('setup failed or terminal before domain');
        }
        terminals.set(id, category);
      } else if (event.kind === 'advertisementObserved') {
        if (!terminals.has('32002') || domains.has('32003:scanStopped') || event.scanSessionId !== '32001' ||
            event.commandId != null || !isCanonicalU64(event.peripheralId) || event.localName !== name ||
            !event.advertisedServices?.includes(service) || event.error) throw new Error('foreign/late advertisement');
      } else if (event.kind === 'connectionStateChanged' && id == null) {
        if (passive || !terminals.has('32008') || !terminals.has('32009') || event.connectionId !== '32005' ||
            !(event.connectionState === 'Disconnected' && !event.error || event.connectionState == null &&
              event.error?.category === 'connection' && event.error.platformDetails?.platform === 'platform_android')) {
          throw new Error('unexpected passive connection loss');
        }
        passive = true;
      } else {
        const command = id;
        const kind = event.kind === 'connectionStateChanged' ? event.connectionState : event.kind;
        const key = `${command}:${kind}`;
        if (!command || !kind || event.error || !expected.get(command)?.includes(kind) || domains.has(key) || terminals.has(command)) {
          throw new Error('unknown/duplicate/late domain');
        }
        if (event.kind === 'adapterSnapshotChanged' && (event.adapterState !== 'PoweredOn' || event.scanAuthorization !== 'Authorized')) throw new Error('adapter not authorized/ON');
        if ((event.kind === 'scanStarted' || event.kind === 'scanStopped') && event.scanSessionId !== '32001') throw new Error('wrong scan route');
        if ((event.kind === 'connectionStateChanged' || event.kind === 'gattDiscovered') && event.connectionId !== '32005') throw new Error('wrong connection route');
        if (event.kind === 'gattDiscovered' && event.gattSessionId !== '32007') throw new Error('wrong GATT route');
        if (kind === 'Connected' && !domains.has('32004:Connecting')) throw new Error('Connected before Connecting');
        domains.add(key);
      }
      events.push(event);
    } catch (error) { failure ??= new Error(detail(error)); fail(failure); }
  });
  const dispatch = (id: string, kinds: readonly string[], submit: () => unknown): void => {
    check(); expected.set(id, kinds);
    const error = submit();
    if (error) throw new Error(`active dispatch rejected: ${id}:${JSON.stringify(error)}`);
  };
  const wait = async (predicate: (event: RfScanEvent) => boolean, label: string, deadline = Date.now() + WAIT_MS): Promise<RfScanEvent> => {
    while (true) {
      check();
      if (Date.now() >= deadline) throw new Error(`active outer wait expired: ${label}; no native outcome inferred`);
      const event = events.find((e) => !consumed.has(e) && predicate(e));
      if (event) { consumed.add(event); return event; }
      await pause();
    }
  };
  const terminal = (id: string): Promise<RfScanEvent> => wait((e) => e.kind === 'commandCompleted' && e.commandId === id, id);
  dispatch('32000', ['adapterSnapshotChanged'], () => probe.initialize('32000'));
  await terminal('32000');
  dispatch('32002', ['scanStarted'], () => probe.startRfScan('32002', '32001', service));
  await terminal('32002');
  const advertisement = await wait((e) => e.kind === 'advertisementObserved', 'advertisement');
  dispatch('32003', ['scanStopped'], () => probe.stopRfScan('32003', '32001'));
  await terminal('32003');
  dispatch('32004', ['Connecting', 'Connected'], () => probe.connectRfPeripheral('32004', '32005', advertisement.peripheralId!));
  await terminal('32004');
  dispatch('32006', ['gattDiscovered'], () => probe.discoverRfGatt('32006', '32005', '32007'));
  const database = await wait((e) => e.kind === 'gattDiscovered', 'GATT');
  const characteristic = validateRfGattDatabase(database, service, '32005').readWriteCharacteristicId;
  await terminal('32006');
  check();
  // Arm the URL listener before publishing readiness; host signals are not BLE events.
  const arm = awaitArm();
  report(`${PREFIX}:active:discovery-ready:${JSON.stringify({ connectionId: '32005', gattSessionId: '32007', characteristicId: characteristic })}`);
  await arm;
  check();
  const lossDeadline = Date.now() + 8_000; // Test budget, never a production watchdog override.
  for (const id of ['32008', '32009']) {
    dispatch(id, [], () => probe.readRfCharacteristic(id, '32005', '32007', characteristic));
    accepted += 1;
    report(`${PREFIX}:active:accepted:${id}`);
  }
  await wait((e) => e.kind === 'commandCompleted' && e.commandId === '32008', 'active loss terminal', lossDeadline);
  await wait((e) => e.kind === 'commandCompleted' && e.commandId === '32009', 'queued loss terminal', lossDeadline);
  await wait((e) => e.kind === 'connectionStateChanged' && e.commandId == null, 'passive loss', lossDeadline);
  check(); settled = true;
  report(`${PREFIX}:active:settled:${terminals.get('32008') === 'connection' ? 'disconnect-first' : 'read-error-first'}`);
  const snapshot = await probe.snapshotRfResources();
  check();
  if (Object.keys(snapshot).length !== 17 || RESOURCE_FIELDS.some((k) => !Number.isSafeInteger(snapshot[k]) || snapshot[k] < 0) ||
      (['activeScans', 'connections', 'gattAttributes', 'subscriptions', 'pendingIo', 'watchdogs', 'callbackRoutes', 'handoffPending'] as const).some((k) => snapshot[k] !== 0) ||
      (['ingressRoots', 'adapterDelegates', 'backendLeases', 'hubRoots', 'handoffRoots', 'listeners', 'lifecycleOwners'] as const).some((k) => snapshot[k] !== 1) || snapshot.workerThreads !== 2) {
    throw new Error('active resources must retire before closing the live Probe');
  }
  report(`${PREFIX}:active:retired:${JSON.stringify(snapshot)}`);
  const end = Date.now() + 1_000;
  while (Date.now() < end) { check(); await pause(); }
  check(); report(`${PREFIX}:active:silence:passed`);
}

/** Software orchestration only: real adapter state and I/O must come from the native Probes. */
export async function runRfAdapterTransition(
  factory: () => Probe, flow: AdapterTransitionFlow, profile: Readonly<Profile>,
  report: Report, cancelled: () => boolean,
  bindOnRequest?: (request: () => void) => void,
): Promise<boolean> {
  const { rfAdapterRunId: runId, rfServiceUuid: service, rfExpectedLocalName: localName } = profile;
  let failure: Error | undefined;
  const fail = (error: unknown): void => {
    if (failure) return;
    failure = new Error(detail(error));
    report(`${PREFIX}:failed:${failure.message}`);
  };
  const mode = profile.rfAdapterMode ?? 'startup-off';
  const ios = Platform.OS === 'ios';
  const onUrl = `blehub-rn-spike://adapter/${runId}/${mode === 'active-loss' ? 'active-loss/on' : 'on'}`;
  let hostReady = false;
  let onRequested = false;
  let awaiting = false;
  let signalled = false;
  let armAwaiting = false;
  let armSignalled = false;
  // Register before any report, Probe construction or initialize dispatch, including the off phase.
  const linking = Linking.addEventListener('url', ({ url }) => {
    if (failure) return;
    if (mode === 'active-loss' && armAwaiting && !armSignalled && url === `blehub-rn-spike://adapter/${runId}/active-loss/arm`) {
      armSignalled = true; armAwaiting = false;
      report(`${PREFIX}:active:arm-signal:runId=${runId}:source=linking`);
      return;
    }
    if (ios && url === `blehub-rn-spike://adapter/${runId}/ready`) {
      if (!awaiting || hostReady || signalled) {
        fail('foreign, early or duplicate ready URL');
        return;
      }
      hostReady = true;
      report(`${PREFIX}:host-window-ready:runId=${runId}:source=linking`);
      return;
    }
    if (!awaiting || signalled || url !== onUrl || ios && (!hostReady || !onRequested)) {
      fail('foreign, early or duplicate on URL');
      return;
    }
    signalled = true;
    awaiting = false;
    report(`${PREFIX}:on-signal:runId=${runId}:source=linking`);
  });
  const healthy = (): void => {
    if (failure) throw failure;
    if (cancelled()) throw new Error('adapter transition cancelled');
  };
  const wait = async (ready: () => boolean, label: string): Promise<void> => {
    const deadline = Date.now() + WAIT_MS;
    while (true) {
      healthy();
      if (Date.now() >= deadline) throw new Error(`${label} timed out after ${WAIT_MS}ms`);
      if (ready()) return;
      await pause();
    }
  };
  let current: Probe | undefined;
  let phase: 'off' | 'on' = 'off';
  let retiredOff = false;
  let started = false;
  let completed = false;
  const closeCurrent = async (): Promise<void> => {
    const probe = current;
    if (!probe) return;
    // Do not retry a failed close or authorize a second Probe after it.
    current = undefined;
    if (phase === 'off') retiredOff = true;
    try { await probe.close(); }
    catch (error) { throw new Error(`${phase} close failed: ${detail(error)}`); }
    const snapshot = await probe.snapshotRfResources();
    report(`${PREFIX}:${phase}:resources:${JSON.stringify(snapshot)}`);
    if (Object.keys(snapshot).length !== 17 || RESOURCE_FIELDS.some((key) =>
      !Number.isSafeInteger(snapshot[key]) || snapshot[key] !== 0 || Object.is(snapshot[key], -0))) {
      throw new Error(`${phase} resources must contain exactly 17 zero counters`);
    }
  };
  try {
    if (Platform.OS !== 'android' && !ios) throw new Error('unsupported adapter platform');
    if (mode !== 'startup-off' && mode !== 'active-loss') throw new Error('unknown adapter mode');
    if (ios && mode !== 'startup-off') throw new Error('iOS supports startup-off only');
    if (ios) parseAdapterNativeIdentity(profile);
    if (mode === 'active-loss' && !localName) throw new Error('active loss requires frozen local name');
    if (!runId || !UUID.test(runId)) throw new Error('rfAdapterRunId must be a canonical UUID');
    if (!service || !UUID.test(service)) throw new Error('rfServiceUuid must be a canonical UUID');
    if (attempts.has(runId)) throw new Error('adapter transition run cannot be rerun');
    attempts.add(runId);
    if (ios) bindOnRequest?.(() => {
      try {
        healthy();
        if (!awaiting || !hostReady || onRequested || signalled) throw new Error('early or duplicate ON request');
        onRequested = true;
        report(`${PREFIX}:on-request:runId=${runId}:source=ui`);
        healthy();
        // Opening requests native validation; only its Linking callback may advance the run.
        void Linking.openURL(onUrl).catch(fail);
      } catch (error) { fail(error); }
    });
    healthy();
    started = true;
    report(`${PREFIX}:runtime:runId=${runId}:nonce=${RF_BOOT_NONCE}:phase=start`);
    healthy();
    const off = factory();
    current = off;
    if (mode === 'active-loss') {
      await runActiveLoss(off, service, localName!, report, healthy, async () => {
        armAwaiting = true;
        await wait(() => armSignalled, 'ARM signal');
      }, () => retiredOff, fail);
    } else {
      let domain = false;
      let initialized = false;
      let scanDispatched = false;
      let scanTerminal = false;
      off.setRfScanListener((event: RfScanEvent) => {
        // Preserve the original event, including native error details; never project a synthetic one.
        report(`${PREFIX}:off:event:${JSON.stringify(event)}`);
        if (failure) return;
        if (retiredOff) { fail('late off callback after close began'); return; }
        if (event.kind === 'adapterSnapshotChanged' && event.commandId === '31000' && !domain && !initialized &&
            event.adapterState === 'PoweredOff' && event.scanAuthorization === 'Authorized' && !event.error) {
          domain = true;
        } else if (event.kind === 'commandCompleted' && event.commandId === '31000' && domain && !initialized && !event.error) {
          initialized = true;
        } else if (event.kind === 'commandCompleted' && event.commandId === '31002' && initialized && scanDispatched &&
            !scanTerminal && event.error?.category === 'adapter_unavailable') {
          scanTerminal = true;
        } else {
          fail(`unexpected off event (state/order/duplicate): ${event.kind}:${event.commandId ?? 'none'}`);
        }
      });
      healthy();
      const initError = off.initialize('31000');
      if (initError) throw new Error(`off initialize dispatch rejected: ${initError.category}`);
      await wait(() => initialized, 'off initialize');
      scanDispatched = true;
      const scanError = off.startRfScan('31002', '31001', service);
      if (scanError) throw new Error(`off scan dispatch rejected: ${scanError.category}`);
      await wait(() => scanTerminal, 'off scan terminal');
    }
    await closeCurrent();
    healthy();
    report(`${PREFIX}:off:passed`);
    healthy();
    awaiting = true;
    report(`${PREFIX}:awaiting-on:runId=${runId}`);
    await wait(() => signalled, 'on signal');
    healthy();
    phase = 'on';
    const on = factory();
    if (on === off) throw new Error('on Probe must be fresh');
    current = on;
    report(`${PREFIX}:on:started`);
    healthy();
    let passedCount = 0;
    await flow('rf-io', on, service, (line) => {
      report(`${PREFIX}:on:flow:${line}`);
      if (/:failed(?::|$)|:close:rejected:/.test(line)) fail(`on flow failed: ${line}`);
      if (/^rf-io:passed(?::|$)/.test(line)) {
        passedCount += 1;
        if (passedCount > 1) fail('duplicate rf-io:passed terminal');
      }
    }, () => cancelled() || failure !== undefined, localName);
    healthy();
    if (passedCount !== 1) throw new Error('missing exact rf-io:passed terminal');
    // runRfLabFlow already closes; explicitly require its idempotent close and final accounting too.
    await closeCurrent();
    healthy();
    report(`${PREFIX}:on:passed`);
    completed = true;
  } catch (error) {
    fail(error);
  } finally {
    try { await closeCurrent(); } catch (error) { fail(error); }
    try { healthy(); } catch (error) { fail(error); }
    if (started) report(`${PREFIX}:runtime:runId=${runId}:nonce=${RF_BOOT_NONCE}:phase=close`);
    linking.remove();
  }
  if (completed && !failure) {
    report(`${PREFIX}:passed:runId=${runId}`);
    return true;
  }
  return false;
}

export function RfAdapterTransition(
  props: Profile & { runFlow: AdapterTransitionFlow },
): React.JSX.Element {
  const profile = useRef(Object.freeze({ ...props })).current;
  const present = useRunReporter();
  const started = useRef(false);
  const cancelled = useRef(false);
  const requestOn = useRef<(() => void) | undefined>(undefined);
  const identity = useMemo(() => {
    if (Platform.OS !== 'ios') return;
    try {
      return JSON.stringify({ native: parseAdapterNativeIdentity(profile), run_id: profile.rfAdapterRunId, nonce: RF_BOOT_NONCE });
    } catch { return; }
  }, [profile]);
  const [state, setState] = useState({ off: 'Waiting', signal: 'Waiting', on: 'Waiting', result: 'Running', failure: '', hostReady: false });
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const report = (line: string): void => {
      console.log(`BLEHUB_NITRO_RUNTIME ${line}`);
      present(line);
      // Event/flow/resource diagnostics belong in the log, never an expanding Fabric node list.
      if (line === `${PREFIX}:off:passed`) setState((s) => ({ ...s, off: 'Passed' }));
      else if (line.startsWith(`${PREFIX}:awaiting-on:`)) setState((s) => ({ ...s, signal: 'Awaiting on URL (30s)' }));
      else if (line.startsWith(`${PREFIX}:host-window-ready:`)) setState((s) => ({ ...s, hostReady: true, signal: 'Host ready · confirm after Settings ON readback' }));
      else if (line.startsWith(`${PREFIX}:on-request:`)) setState((s) => ({ ...s, signal: 'ON requested · awaiting native URL' }));
      else if (line.startsWith(`${PREFIX}:on-signal:`)) setState((s) => ({ ...s, signal: 'Received' }));
      else if (line === `${PREFIX}:on:started`) setState((s) => ({ ...s, on: 'Running rf-io' }));
      else if (line === `${PREFIX}:on:passed`) setState((s) => ({ ...s, on: 'Passed' }));
      else if (line.startsWith(`${PREFIX}:failed:`)) setState((s) => ({ ...s, result: 'Failed', failure: s.failure || line }));
      else if (line.startsWith(`${PREFIX}:passed:`)) setState((s) => s.failure ? s : { ...s, result: 'Passed' });
    };
    void runRfAdapterTransition(createBleHubProbe, profile.runFlow, profile, report, () => cancelled.current,
      (request) => { requestOn.current = request; })
      .catch((error: unknown) => report(`${PREFIX}:failed:${detail(error)}`));
    return () => { cancelled.current = true; };
  }, [profile, present]);
  const running = state.result === 'Running';
  const failed = state.result === 'Failed';
  return (
    <View testID="rf-adapter-transition-page" collapsable={false} style={styles.header}>
      <View style={styles.card} collapsable={false}>
        <Text style={styles.cardTitle}>{profile.rfAdapterMode === 'active-loss' ? 'Read retirement & recovery' : Platform.OS === 'ios' ? 'iOS startup-off foundation' : 'Android adapter transition'}</Text>
        <Text style={styles.badge}>{profile.rfAdapterMode === 'active-loss' ? 'Held read + queued read / radio OFF → ON' : 'Opt-in / Bluetooth OFF → ON'}</Text>
        {Platform.OS === 'ios' ? <>
          <Text style={styles.stepDetail}>Bluetooth must be OFF before backend initialization. Host transport / RF integration is pending.</Text>
          <Text testID="rf-adapter-transition-identity" accessibilityLabel={identity ?? 'Invalid native identity'} style={styles.eventLine}>
            Native / JS session identity · included in test evidence
          </Text>
        </> : null}
        <Text testID="rf-adapter-transition-off" style={styles.stepLabel}>{profile.rfAdapterMode === 'active-loss' ? 'Active read loss' : 'Off'}: {state.off}</Text>
        <Text testID="rf-adapter-transition-signal" style={styles.stepLabel}>Signal: {state.signal}</Text>
        <Text testID="rf-adapter-transition-on" style={styles.stepLabel}>On: {state.on}</Text>
      </View>
      <View style={styles.statusCard} collapsable={false} accessibilityLiveRegion="polite">
        <Text testID="rf-adapter-transition-result"
          style={[styles.statusValue, failed ? styles.errorText : !running ? styles.successText : undefined]}>{state.result}</Text>
        <Text testID="rf-adapter-transition-failure" style={[styles.eventLine, failed ? styles.errorText : undefined]}>
          {state.failure || 'No failure reported.'}
        </Text>
      </View>
      {Platform.OS === 'ios' ? <Pressable testID="rf-adapter-transition-confirm-on" accessibilityRole="button"
        style={[styles.button, styles.primaryButton]} disabled={!running || !state.hostReady}
        accessibilityState={{ disabled: !running || !state.hostReady }} onPress={() => requestOn.current?.()}>
        <Text style={styles.primaryButtonText}>Confirm Settings Bluetooth is ON</Text>
      </Pressable> : null}
      <Pressable testID="rf-adapter-transition-cancel" accessibilityRole="button"
        style={[styles.button, styles.secondaryButton]}
        disabled={!running} accessibilityState={{ disabled: !running }}
        onPress={() => { cancelled.current = true; }}>
        <Text style={styles.secondaryButtonText}>{running ? 'Cancel scenario' : 'Scenario finished'}</Text>
      </Pressable>
      <Text style={styles.footer}>{Platform.OS === 'ios'
              ? 'App checks are not a host RF verdict. Process death or runner completion does not prove session continuity or a persistent result page.'
              : 'The host independently verifies radio state and evidence. This result page stays open after the app checks finish.'}</Text>
    </View>
  );
}
