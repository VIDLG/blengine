import React, { useEffect, useRef, useState } from 'react';
import { Linking, Platform, Text, View } from 'react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { RfResourceSnapshot, RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import { projectActiveEvent } from './rf_adapter_transition';
import type { AdapterTransitionFlow } from './rf_adapter_transition';
import { isCanonicalU64, validateRfGattDatabase } from './rf_contract';
import { styles } from './rf_lab_screen';
import { useRunReporter } from './rf_lab_session';
import { RF_BOOT_NONCE } from './rf_runtime';

type Probe = ReturnType<typeof createBleHubProbe>;
type Report = (line: string) => void;
export type PermissionTransitionFlow = AdapterTransitionFlow;
export interface PermissionTransitionProfile {
  rfPermissionRunId?: string;
  rfPermissionPhase?: 'denied' | 'active' | 'restored' | 'inspect' | 'authorized';
  rfPermissionNativeIdentity?: string;
  rfServiceUuid?: string;
  rfExpectedLocalName?: string;
}
const PREFIX = 'rf-permission-transition';
const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;
// One phase per JS engine. A reload cannot authorize another attempt; native/host identity
// checks additionally require three independent App processes for the same run UUID.
const attempts = new Set<string>();
const FIELDS = [
  'activeScans', 'connections', 'gattAttributes', 'subscriptions', 'pendingIo', 'watchdogs',
  'callbackRoutes', 'ingressRoots', 'adapterDelegates', 'peerCacheEntries', 'backendLeases',
  'hubRoots', 'handoffRoots', 'handoffPending', 'listeners', 'workerThreads', 'lifecycleOwners',
] as const satisfies readonly (keyof RfResourceSnapshot)[];
const pause = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 25));
const detail = (error: unknown): string => error instanceof Error ? error.message : String(error);
function validResources(snapshot: RfResourceSnapshot): boolean {
  return Object.keys(snapshot).length === 17 && FIELDS.every((key) =>
    Number.isSafeInteger(snapshot[key]) && snapshot[key] >= 0 && !Object.is(snapshot[key], -0));
}

/** Software orchestration only: process death, OS permissions and full-run verdict belong to the host. */
export async function runRfPermissionTransition(
  factory: () => Probe, flow: PermissionTransitionFlow, profile: Readonly<PermissionTransitionProfile>,
  report: Report, cancelled: () => boolean,
): Promise<boolean> {
  const { rfPermissionRunId: runId, rfPermissionPhase: phase, rfServiceUuid: service, rfExpectedLocalName: name } = profile;
  let failure: Error | undefined;
  const fail = (error: unknown): void => {
    if (failure) return;
    failure = new Error(detail(error));
    report(`${PREFIX}:failed:phase=${phase ?? 'invalid'}:runId=${runId ?? 'invalid'}:${failure.message}`);
  };
  const healthy = (): void => {
    if (failure) throw failure;
    if (cancelled()) throw new Error('permission transition cancelled');
  };
  const wait = async (ready: () => boolean, label: string, deadline = Date.now() + 30_000): Promise<void> => {
    while (true) {
      healthy();
      if (Date.now() >= deadline) throw new Error(`${label} timed out; no process death inferred`);
      if (ready()) return;
      await pause();
    }
  };
  let armReady = false;
  let armed = false;
  let started = false;
  let completed = false;
  let current: Probe | undefined;
  let holding = false;
  let closeLabel = phase ?? 'invalid';
  const retired = new Set<Probe>();
  const linking = Linking.addEventListener('url', ({ url }) => {
    if (failure) return;
    if (phase !== 'active' || !armReady || armed || url !== `blehub-rn-spike://permission/${runId}/active/arm`) {
      fail('foreign, early or duplicate arm URL');
      return;
    }
    armed = true;
    armReady = false;
    report(`${PREFIX}:active:arm-signal:runId=${runId}:source=linking`);
  });
  const audit = (event: RfScanEvent): void => {
    const projected = projectActiveEvent(event, service!, '52005');
    const line = `${PREFIX}:${phase}:event:${JSON.stringify(projected)}`;
    // Hermes-safe UTF-8 length; reject instead of emitting a truncated/duplicate GATT record.
    if (encodeURIComponent(`BLEHUB_NITRO_RUNTIME ${line}`).replace(/%[0-9A-F]{2}|./g, 'x').length > 3500) {
      throw new Error('audit event exceeds 3500-byte UTF-8 budget; no truncated event emitted');
    }
    report(line);
  };
  const closeCurrent = async (): Promise<void> => {
    const probe = current;
    if (!probe) return;
    current = undefined;
    retired.add(probe);
    try { await probe.close(); }
    catch (error) { throw new Error(`${closeLabel} close failed: ${detail(error)}`); }
    const snapshot = await probe.snapshotRfResources();
    report(`${PREFIX}:${closeLabel}:resources:${JSON.stringify(snapshot)}`);
    if (!validResources(snapshot) || FIELDS.some((key) => snapshot[key] !== 0)) {
      throw new Error(`${closeLabel} resources must contain exactly 17 zero counters`);
    }
  };
  const runIo = async (probe: Probe, label: string): Promise<void> => {
    let passes = 0;
    await flow('rf-io', probe, service, (line) => {
      report(`${PREFIX}:${phase}:flow:${label}:${line}`);
      if (/:failed(?::|$)|:close:rejected:/.test(line)) fail(`${label} rf-io failed: ${line}`);
      if (/^rf-io:passed(?::|$)/.test(line) && ++passes !== 1) fail('duplicate rf-io:passed terminal');
    }, () => cancelled() || failure !== undefined, name);
    healthy();
    if (passes !== 1) throw new Error(`${label} missing exact rf-io:passed terminal`);
    await closeCurrent();
    healthy();
  };
  try {
    const iosAuthorization = Platform.OS === 'ios';
    if (Platform.OS !== 'android' && !iosAuthorization) throw new Error('unsupported permission platform');
    if (iosAuthorization ? phase !== 'denied' && phase !== 'authorized'
      : phase !== 'denied' && phase !== 'active' && phase !== 'restored') throw new Error('invalid platform permission phase');
    if (!runId || !UUID.test(runId)) throw new Error('rfPermissionRunId must be a canonical UUID');
    if (!service || !UUID.test(service)) throw new Error('rfServiceUuid must be a canonical UUID');
    if (phase === 'active' && !name) throw new Error('active requires frozen rfExpectedLocalName');
    if (attempts.has(runId)) throw new Error('permission run requires a fresh App process, not another phase/reload');
    attempts.add(runId);
    healthy();
    started = true;
    report(`${PREFIX}:runtime:runId=${runId}:phase=${phase}:nonce=${RF_BOOT_NONCE}:state=start`);
    healthy();
    const first = factory();
    current = first;
    if (phase === 'denied') {
      let initializing = false;
      let snapshotSeen = false;
      let initialized = false;
      let scanning = false;
      let rejected = false;
      first.setRfScanListener((event) => {
        try {
          audit(event);
          healthy();
          if (retired.has(first)) throw new Error('late denied callback after close began');
          if (event.kind === 'adapterSnapshotChanged' && event.commandId === '51000' && initializing &&
              !snapshotSeen && !initialized && event.scanAuthorization === 'Denied' && !event.error) {
            // CONNECT denial has no Nitro DTO field; only host OS-state evidence can prove it.
            // In particular, PoweredOn + Denied is valid and is not an adapter-state failure.
            snapshotSeen = true;
          } else if (event.kind === 'commandCompleted' && event.commandId === '51000' &&
              snapshotSeen && !initialized && !event.error) {
            initialized = true;
          } else if (event.kind === 'commandCompleted' && event.commandId === '51002' && initialized &&
              scanning && !rejected && event.error?.category === 'permission') {
            rejected = true;
          } else throw new Error(`unexpected denied event (state/order/duplicate): ${event.kind}:${event.commandId ?? 'none'}`);
        } catch (error) { fail(error); }
      });
      initializing = true;
      const initError = first.initialize('51000');
      if (initError) throw new Error(`denied initialize dispatch rejected: ${initError.category}`);
      await wait(() => initialized, 'denied initialize');
      scanning = true;
      const scanError = first.startRfScan('51002', '51001', service);
      if (scanError) throw new Error(`denied scan dispatch rejected: ${scanError.category}`);
      await wait(() => rejected, 'denied Permission terminal');
      await closeCurrent();
      healthy();
      report(`${PREFIX}:denied:passed`);
      completed = true;
    } else if (phase === 'authorized') {
      let snapshotSeen = false;
      let initialized = false;
      first.setRfScanListener((event) => {
        try {
          audit(event); healthy();
          if (retired.has(first)) throw new Error('late authorized callback after close began');
          if (event.kind === 'adapterSnapshotChanged' && event.commandId === '53000' && !snapshotSeen && !initialized &&
              event.adapterState === 'PoweredOn' && event.scanAuthorization === 'Authorized' && !event.error) {
            snapshotSeen = true;
          } else if (event.kind === 'commandCompleted' && event.commandId === '53000' && snapshotSeen && !initialized && !event.error) {
            initialized = true;
          } else throw new Error(`unexpected authorized event: ${event.kind}:${event.commandId ?? 'none'}`);
        } catch (error) { fail(error); }
      });
      const error = first.initialize('53000');
      if (error) throw new Error(`authorized initialize dispatch rejected: ${error.category}`);
      await wait(() => initialized, 'authorized initialize');
      await closeCurrent(); healthy();
      report(`${PREFIX}:authorized:passed`);
      completed = true;
    } else if (phase === 'restored') {
      await runIo(first, 'restored');
      report(`${PREFIX}:restored:passed`);
      completed = true;
    } else {
      closeLabel = 'active:baseline';
      await runIo(first, 'baseline');
      report(`${PREFIX}:active:baseline-passed`);
      healthy();
      const probe = factory();
      if (probe === first) throw new Error('active Probe B must be fresh');
      current = probe;
      closeLabel = 'active:setup';
      const expected = new Map<string, readonly string[]>();
      const domains = new Map<string, string[]>();
      const terminals = new Set<string>();
      let advertisement: RfScanEvent | undefined;
      let database: RfScanEvent | undefined;
      probe.setRfScanListener((event) => {
        try {
          audit(event);
          healthy();
          if (retired.has(probe)) throw new Error('late active callback after close began');
          if (holding) throw new Error(`unexpected surviving active event before OS kill: ${event.kind}:${event.commandId ?? 'none'}`);
          const id = event.commandId;
          if (event.kind === 'advertisementObserved') {
            if (!terminals.has('52002') || domains.has('52003') || event.scanSessionId !== '52001' ||
                id != null || !isCanonicalU64(event.peripheralId) || event.localName !== name ||
                !event.advertisedServices?.includes(service) || event.error) throw new Error('foreign/late advertisement');
            advertisement ??= event;
            return;
          }
          if (!id || !expected.has(id) || terminals.has(id) || event.error) throw new Error('foreign/duplicate/failed active event');
          const seen = domains.get(id) ?? [];
          const kinds = expected.get(id)!;
          if (event.kind === 'commandCompleted') {
            if (seen.length !== kinds.length) throw new Error('active terminal before domain');
            terminals.add(id);
            return;
          }
          const kind = event.kind === 'connectionStateChanged' ? event.connectionState : event.kind;
          if (kind !== kinds[seen.length]) throw new Error('active domain order mismatch');
          if (id === '52000' && (event.adapterState !== 'PoweredOn' || event.scanAuthorization !== 'Authorized')) throw new Error('active requires PoweredOn + Authorized');
          if ((id === '52002' || id === '52003') && event.scanSessionId !== '52001') throw new Error('foreign scan route');
          if ((id === '52004' || id === '52006') && event.connectionId !== '52005') throw new Error('foreign connection route');
          if (id === '52006') {
            if (event.gattSessionId !== '52007') throw new Error('foreign GATT route');
            validateRfGattDatabase(event, service, '52005'); // Validate the full source, never the audit projection.
            database = event;
          }
          domains.set(id, [...seen, kind!]);
        } catch (error) { fail(error); }
      });
      const dispatch = (id: string, kinds: readonly string[], submit: () => unknown): void => {
        healthy();
        expected.set(id, kinds);
        const error = submit();
        if (error) throw new Error(`active dispatch rejected: ${id}:${JSON.stringify(error)}`);
        healthy();
      };
      const terminal = (id: string): Promise<void> => wait(() => terminals.has(id), `active terminal ${id}`);
      dispatch('52000', ['adapterSnapshotChanged'], () => probe.initialize('52000'));
      await terminal('52000');
      dispatch('52002', ['scanStarted'], () => probe.startRfScan('52002', '52001', service));
      await terminal('52002');
      await wait(() => advertisement !== undefined, 'active advertisement');
      dispatch('52003', ['scanStopped'], () => probe.stopRfScan('52003', '52001'));
      await terminal('52003');
      dispatch('52004', ['Connecting', 'Connected'], () => probe.connectRfPeripheral('52004', '52005', advertisement!.peripheralId!));
      await terminal('52004');
      dispatch('52006', ['gattDiscovered'], () => probe.discoverRfGatt('52006', '52005', '52007'));
      await terminal('52006');
      const characteristicId = validateRfGattDatabase(database!, service, '52005').readWriteCharacteristicId;
      const route = `characteristicId=${characteristicId}:connectionId=52005:gattSessionId=52007`;
      armReady = true;
      report(`${PREFIX}:active:discovery-ready:${route}:runId=${runId}`);
      await wait(() => armed, 'canonical active arm URL');
      healthy();
      // From here onward there is intentionally no automatic cancel/close. A live JS process
      // cannot certify its own OS death, old terminals, finally execution or old 17-zero state.
      holding = true;
      const deadline = Date.now() + 8_000;
      dispatch('52008', [], () => probe.readRfCharacteristic('52008', '52005', '52007', characteristicId));
      dispatch('52009', [], () => probe.readRfCharacteristic('52009', '52005', '52007', characteristicId));
      let snapshot: RfResourceSnapshot | undefined;
      let snapshotFailure: unknown;
      let snapshotSettled = false;
      void probe.snapshotRfResources().then((value) => { snapshot = value; snapshotSettled = true; },
        (error: unknown) => { snapshotFailure = error; snapshotSettled = true; });
      await wait(() => snapshotSettled, 'active native snapshot', deadline);
      if (snapshotFailure !== undefined) throw snapshotFailure;
      if (!snapshot || !validResources(snapshot) || snapshot.pendingIo !== 2 || snapshot.watchdogs !== 1 ||
          snapshot.connections !== 1 || snapshot.activeScans !== 0 || snapshot.subscriptions !== 0 || snapshot.gattAttributes <= 0) {
        throw new Error('active native snapshot must show pendingIo=2 watchdogs=1 connections=1 with discovered GATT');
      }
      report(`${PREFIX}:active:reads-accepted:activeCommandId=52008:queuedCommandId=52009:${route}:runId=${runId}`);
      healthy();
      report(`${PREFIX}:active:pending-resources:${JSON.stringify(snapshot)}`);
      await wait(() => false, 'OS kill not observed within 8000ms (surviving App)', deadline);
    }
  } catch (error) {
    fail(error);
  } finally {
    if (!holding) {
      try { await closeCurrent(); healthy(); } catch (error) { fail(error); }
      if (started) report(`${PREFIX}:runtime:runId=${runId}:phase=${phase}:nonce=${RF_BOOT_NONCE}:state=close`);
    }
    linking.remove();
  }
  if (completed && !failure) {
    report(`${PREFIX}:passed:phase=${phase}:runId=${runId}`);
    return true;
  }
  return false;
}

export function RfPermissionTransition(props: PermissionTransitionProfile & { runFlow: PermissionTransitionFlow }): React.JSX.Element {
  const profile = useRef(Object.freeze({ ...props })).current;
  const present = useRunReporter();
  const started = useRef(false);
  const cancelled = useRef(false);
  const [state, setState] = useState({
    progress: profile.rfPermissionPhase === 'inspect' ? 'Native authorization inspection only; no Probe or BLE commands' : 'Preparing current phase',
    result: profile.rfPermissionPhase === 'inspect' ? 'Inspection only' : 'Running', failure: '',
  });
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    if (Platform.OS === 'ios' && profile.rfPermissionPhase === 'inspect') return;
    const report = (line: string): void => {
      console.log(`BLEHUB_NITRO_RUNTIME ${line}`);
      present(line);
      if (line.startsWith(`${PREFIX}:failed:`)) setState((s) => ({ ...s, result: 'App checks failed', failure: s.failure || line }));
      else if (line.startsWith(`${PREFIX}:passed:`)) setState((s) => s.failure ? s : { ...s, result: 'Current phase App checks passed' });
      else if (line === `${PREFIX}:active:baseline-passed`) setState((s) => ({ ...s, progress: 'Baseline passed; preparing Probe B' }));
      else if (line.startsWith(`${PREFIX}:active:discovery-ready:`)) setState((s) => ({ ...s, progress: 'Discovered; awaiting canonical arm URL' }));
      else if (line.startsWith(`${PREFIX}:active:reads-accepted:`)) setState((s) => ({ ...s, progress: 'Two reads pending; awaiting host OS kill (8s)' }));
    };
    void runRfPermissionTransition(createBleHubProbe, profile.runFlow, profile, report, () => cancelled.current)
      .catch((error: unknown) => report(`${PREFIX}:failed:phase=${profile.rfPermissionPhase}:runId=${profile.rfPermissionRunId}:${detail(error)}`));
    return () => { cancelled.current = true; };
  }, [profile, present]);
  return (
    <View testID="rf-permission-transition-page" collapsable={false} style={styles.header}>
      <View style={styles.card} collapsable={false}>
        <Text style={styles.cardTitle}>{Platform.OS === 'ios' ? 'iOS authorization (non-RF)' : 'Android permission'} · {profile.rfPermissionPhase ?? 'invalid'}</Text>
                {Platform.OS === 'ios' ? <Text testID="permission-native-identity" accessibilityLabel={profile.rfPermissionNativeIdentity ?? 'Missing native identity'} style={styles.eventLine}>Native launch identity · included in XCTest evidence</Text> : null}
        <Text style={styles.stepLabel}>{state.progress}</Text>
      </View>
      <View style={styles.statusCard} collapsable={false} accessibilityLiveRegion="polite">
        <Text testID="rf-permission-transition-result" style={styles.statusValue}>{state.result}</Text>
        <Text testID="rf-permission-transition-failure" style={styles.eventLine}>{state.failure || 'No failure reported.'}</Text>
      </View>
      <Text style={styles.footer}>{Platform.OS === 'ios'
              ? 'Authorization checks only: no authorized scan, connection or RF recovery. XCUITest owns permission reset, consent and restoration. This is not the Android process-death gate.'
              : 'Only this phase is checked here, not the full host run. After active process death, old terminals and close are unobservable. A new App proves only its own phase. CONNECT denial requires host OS-state evidence.'}</Text>
    </View>
  );
}
