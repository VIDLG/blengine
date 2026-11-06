import React from 'react';
import { Linking, Platform } from 'react-native';
import { act, render } from '@testing-library/react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { RfResourceSnapshot, RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import { isRfMode, rfMarkerPrefix } from './rf_contract';
import { RF_BOOT_NONCE } from './rf_runtime';
import { RfPermissionTransition, runRfPermissionTransition } from './rf_permission_transition';
import type { PermissionTransitionFlow, PermissionTransitionProfile } from './rf_permission_transition';

jest.mock('@blehub/react-native-internal', () => ({ createBleHubProbe: jest.fn() }));

// SOFTWARE-ONLY adversarial orchestration fakes, never native/OS/RF evidence.
type Probe = ReturnType<typeof createBleHubProbe>;
const P = 'rf-permission-transition';
const SERVICE = 'fd100000-0000-4000-8000-000000000000';
const NAME = 'BleHubNrf52840';
const zero = (): RfResourceSnapshot => ({
  activeScans: 0, connections: 0, gattAttributes: 0, subscriptions: 0, pendingIo: 0, watchdogs: 0,
  callbackRoutes: 0, ingressRoots: 0, adapterDelegates: 0, peerCacheEntries: 0, backendLeases: 0,
  hubRoots: 0, handoffRoots: 0, handoffPending: 0, listeners: 0, workerThreads: 0, lifecycleOwners: 0,
});
const pending = (): RfResourceSnapshot => ({ ...zero(), pendingIo: 2, watchdogs: 1, connections: 1,
  gattAttributes: 8, callbackRoutes: 2, ingressRoots: 1, adapterDelegates: 1, backendLeases: 1,
  hubRoots: 1, handoffRoots: 1, listeners: 1, workerThreads: 2, lifecycleOwners: 1 });
const terminal = (commandId: string): RfScanEvent => ({ kind: 'commandCompleted', commandId });
const denied: RfScanEvent = { kind: 'adapterSnapshotChanged', commandId: '51000', adapterState: 'PoweredOn', scanAuthorization: 'Denied' };
const permission: RfScanEvent = { ...terminal('51002'), error: { category: 'permission', message: 'scan permission denied',
  platformDetails: { platform: 'platform_android', name: 'AndroidBleBackend', code: 'SecurityException' } } };
const signals = new Set<(event: { url: string }) => void>();
const signal = (url: string): void => { for (const listener of signals) listener({ url }); };
const arm = (id: string): string => `blehub-rn-spike://permission/${id}/active/arm`;
let sequence = 0;
const profile = (phase: PermissionTransitionProfile['rfPermissionPhase'] = 'denied'): PermissionTransitionProfile => ({
  rfPermissionRunId: `12345678-1234-4234-8234-${String(++sequence).padStart(12, '0')}`,
  rfPermissionPhase: phase, rfServiceUuid: SERVICE, rfExpectedLocalName: NAME,
});
function deniedProbe(init = [denied, terminal('51000')], scan = [permission]) {
  let listener: (event: RfScanEvent) => void = () => {};
  const emit = (event: RfScanEvent): void => listener(event);
  return {
    emit,
    setRfScanListener: jest.fn((callback: typeof listener) => { listener = callback; }),
    initialize: jest.fn(() => { init.forEach(emit); return undefined; }),
    startRfScan: jest.fn(() => { scan.forEach(emit); return undefined; }),
    close: jest.fn(async () => {}), snapshotRfResources: jest.fn(async () => zero()),
  };
}
function activeProbe() {
  let listener: (event: RfScanEvent) => void = () => {};
  const emit = (event: RfScanEvent): void => listener(event);
  const complete = (id: string): void => emit(terminal(id));
  const gattServices: NonNullable<RfScanEvent['gattServices']> = [{
    id: '19', uuid: SERVICE, primary: true,
    characteristics: ['fff1', 'fff2', 'fff5', 'fff3', 'fff4'].map((prefix, index) => ({
      id: String(20 + index), uuid: `${prefix}0000-0000-4000-8000-000000000000`, readable: index !== 4,
      writableWithResponse: index === 0 || index === 4, writableWithoutResponse: index === 0 || index === 4,
      notifiable: index === 1, indicatable: index === 1 || index === 2,
      descriptors: index === 1 || index === 2 ? [{ id: String(30 + index), uuid: '00002902-0000-1000-8000-00805f9b34fb' }] : [],
    })),
  }];
  return {
    emit, gattServices,
    setRfScanListener: jest.fn((callback: typeof listener) => { listener = callback; }),
    initialize: jest.fn((commandId: string) => {
      emit({ kind: 'adapterSnapshotChanged', commandId, adapterState: 'PoweredOn', scanAuthorization: 'Authorized' }); complete(commandId);
    }),
    startRfScan: jest.fn((commandId: string, scanSessionId: string) => {
      emit({ kind: 'scanStarted', commandId, scanSessionId }); complete(commandId);
      emit({ kind: 'advertisementObserved', scanSessionId, peripheralId: '42', localName: NAME, advertisedServices: [SERVICE] });
    }),
    stopRfScan: jest.fn((commandId: string, scanSessionId: string) => { emit({ kind: 'scanStopped', commandId, scanSessionId }); complete(commandId); }),
    connectRfPeripheral: jest.fn((commandId: string, connectionId: string) => {
      for (const connectionState of ['Connecting', 'Connected']) emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState });
      complete(commandId);
    }),
    discoverRfGatt: jest.fn((commandId: string, connectionId: string, gattSessionId: string) => {
      emit({ kind: 'gattDiscovered', commandId, connectionId, gattSessionId, gattServices }); complete(commandId);
    }),
    readRfCharacteristic: jest.fn((_commandId: string, _connectionId: string, _gattSessionId: string, _characteristicId: string) => undefined),
    close: jest.fn(async () => {}), snapshotRfResources: jest.fn(async () => pending()),
  };
}
const asProbe = (probe: object): Probe => probe as Probe;
const passingFlow: PermissionTransitionFlow = async (_mode, _probe, _service, report) => { report('rf-io:passed:software-only'); };
function start(options: {
  props?: PermissionTransitionProfile; first?: ReturnType<typeof deniedProbe>; active?: ReturnType<typeof activeProbe>;
  flow?: PermissionTransitionFlow; autoArm?: boolean; onReport?: (line: string, props: PermissionTransitionProfile) => void;
  cancelled?: () => boolean;
} = {}) {
  const props = options.props ?? profile();
  const first = options.first ?? deniedProbe();
  const active = options.active ?? activeProbe();
  const probes = [asProbe(first), asProbe(active)];
  const factory = jest.fn(() => probes.shift()!);
  const lines: string[] = [];
  const report = (line: string): void => {
    lines.push(line);
    options.onReport?.(line, props);
    if (options.autoArm !== false && line.startsWith(`${P}:active:discovery-ready:`)) signal(arm(props.rfPermissionRunId!));
  };
  const result = runRfPermissionTransition(factory, options.flow ?? passingFlow, props, report, options.cancelled ?? (() => false));
  return { props, first, active, factory, lines, result };
}
async function finish(run: ReturnType<typeof start>): Promise<boolean> {
  await jest.runAllTimersAsync();
  return run.result;
}
function expectFailed(run: ReturnType<typeof start>): void {
  expect(run.lines.some((line) => line.startsWith(`${P}:failed:phase=`))).toBe(true);
  expect(run.lines.some((line) => line.startsWith(`${P}:passed:`))).toBe(false);
}
beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  signals.clear();
  jest.replaceProperty(Platform, 'OS', 'android');
  jest.spyOn(console, 'log').mockImplementation(() => {});
  jest.spyOn(Linking, 'addEventListener').mockImplementation((_type, callback) => {
    signals.add(callback);
    return { remove: () => { signals.delete(callback); } } as ReturnType<typeof Linking.addEventListener>;
  });
});
afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); });

test('denied accepts PoweredOn + Denied, domain-before-terminal, exact Permission; closes 17 zero before phase verdict', async () => {
  const run = start();
  expect(await finish(run)).toBe(true);
  const id = run.props.rfPermissionRunId;
  expect(run.first.initialize).toHaveBeenCalledWith('51000');
  expect(run.first.startRfScan).toHaveBeenCalledWith('51002', '51001', SERVICE);
  expect(run.factory).toHaveBeenCalledTimes(1);
  expect(run.lines).toEqual([
    `${P}:runtime:runId=${id}:phase=denied:nonce=${RF_BOOT_NONCE}:state=start`,
    ...[denied, terminal('51000'), permission].map((e) => `${P}:denied:event:${JSON.stringify(e)}`),
    `${P}:denied:resources:${JSON.stringify(zero())}`, `${P}:denied:passed`,
    `${P}:runtime:runId=${id}:phase=denied:nonce=${RF_BOOT_NONCE}:state=close`,
    `${P}:passed:phase=denied:runId=${id}`,
  ]);
  expect(signals.size).toBe(0);
  expect(isRfMode('rf-permission-transition')).toBe(true);
  expect(rfMarkerPrefix('rf-permission-transition')).toBe(P);
});

test.each([
  ['wrong init order', [terminal('51000'), denied], [permission]],
  ['forged scan success', [denied, terminal('51000')], [terminal('51002')]],
  ['wrong error', [denied, terminal('51000')], [{ ...permission, error: { category: 'adapter_unavailable', message: 'off' } }]],
  ['duplicate terminal', [denied, terminal('51000')], [permission, permission]],
  ['scan domain forbidden', [denied, terminal('51000')], [{ kind: 'scanStarted', commandId: '51002', scanSessionId: '51001' }, permission]],
  ['advertisement forbidden', [denied, terminal('51000')], [permission, { kind: 'advertisementObserved', scanSessionId: '51001' }]],
] as [string, RfScanEvent[], RfScanEvent[]][])('%s fails denied audit', async (_label, init, scan) => {
  const run = start({ first: deniedProbe(init, scan) });
  expect(await finish(run)).toBe(false); expectFailed(run);
});

test('dispatch rejection is not an eventual Permission terminal', async () => {
  const first = deniedProbe();
  first.startRfScan.mockImplementation(() => ({ category: 'invalid_request', message: 'rejected' }) as never);
  const run = start({ first });
  expect(await finish(run)).toBe(false); expectFailed(run);
});

test.each(['close', 'counter', 'missing-counter'] as const)('denied %s failure cannot pass', async (fault) => {
  const first = deniedProbe();
  if (fault === 'close') first.close.mockRejectedValue(new Error('close blocked'));
  if (fault === 'counter') first.snapshotRfResources.mockResolvedValue({ ...zero(), listeners: 1 });
  if (fault === 'missing-counter') first.snapshotRfResources.mockResolvedValue({ ...zero(), pendingIo: undefined } as unknown as RfResourceSnapshot);
  const run = start({ first });
  expect(await finish(run)).toBe(false); expectFailed(run);
  expect(first.close).toHaveBeenCalledTimes(1);
});

test.each(['missing', 'unknown', 'uppercase-id', 'non-android'] as const)('phase/launch validation: %s', async (fault) => {
  const props = profile();
  if (fault === 'missing') delete props.rfPermissionPhase;
  if (fault === 'unknown') props.rfPermissionPhase = 'on' as never;
  if (fault === 'uppercase-id') props.rfPermissionRunId = 'ABCDEF00-1234-4234-8234-123456789012';
  if (fault === 'non-android') jest.replaceProperty(Platform, 'OS', 'macos');
  const run = start({ props });
  expect(await finish(run)).toBe(false); expectFailed(run); expect(run.factory).not.toHaveBeenCalled();
});

test('iOS denied uses the same strict native audit without Android process-death claims', async () => {
  jest.replaceProperty(Platform, 'OS', 'ios');
  const first = deniedProbe([{ ...denied, adapterState: 'Unavailable' }, terminal('51000')], [{ ...permission, error: { category: 'permission', message: 'CoreBluetooth scan authorization is not allowed' } }]);
  const flow = jest.fn(passingFlow);
  const run = start({ first, flow });
  expect(await finish(run)).toBe(true);
  expect(first.close).toHaveBeenCalledTimes(1);
  expect(flow).not.toHaveBeenCalled();
});

test('iOS authorized only initializes and closes, with no scan, I/O or RF claim', async () => {
  jest.replaceProperty(Platform, 'OS', 'ios');
  const first = deniedProbe([{ ...denied, commandId: '53000', scanAuthorization: 'Authorized' }, terminal('53000')]);
  const flow = jest.fn(passingFlow);
  const run = start({ props: profile('authorized'), first, flow });
  expect(await finish(run)).toBe(true);
  expect(first.initialize).toHaveBeenCalledWith('53000');
  expect(first.startRfScan).not.toHaveBeenCalled();
  expect(flow).not.toHaveBeenCalled();
  expect(first.close).toHaveBeenCalledTimes(1);
  expect(run.lines).toContain(`${P}:authorized:passed`);
});

test('native launch metadata cannot substitute for an authorized backend snapshot', async () => {
  jest.replaceProperty(Platform, 'OS', 'ios');
  const first = deniedProbe([{ ...denied, commandId: '53000' }, terminal('53000')]);
  const run = start({ props: { ...profile('authorized'), rfPermissionNativeIdentity: '{"authorization_at_launch":"authorized"}' }, first });
  expect(await finish(run)).toBe(false);
  expectFailed(run);
  expect(first.startRfScan).not.toHaveBeenCalled();
});

test.each(['inspect', 'authorized'] as const)('Android rejects iOS-only %s before creating a Probe', async (phase) => {
  const run = start({ props: profile(phase) });
  expect(await finish(run)).toBe(false);
  expect(run.factory).not.toHaveBeenCalled();
});

test.each(['active', 'restored'] as const)('iOS rejects Android %s before creating a Probe', async (phase) => {
  jest.replaceProperty(Platform, 'OS', 'ios');
  const run = start({ props: profile(phase) });
  expect(await finish(run)).toBe(false);
  expect(run.factory).not.toHaveBeenCalled();
});

test('iOS native authorization inspection does not create a Probe or listener', async () => {
  jest.replaceProperty(Platform, 'OS', 'ios');
  const props = { ...profile('inspect'), rfPermissionNativeIdentity: '{"authorization_at_launch":"authorized"}' };
  const ui = render(<RfPermissionTransition {...props} runFlow={passingFlow} />);
  await act(async () => { await jest.runAllTimersAsync(); });
  expect(ui.getByTestId('permission-native-identity').props.accessibilityLabel).toBe(props.rfPermissionNativeIdentity);
  expect(createBleHubProbe).not.toHaveBeenCalled();
  expect(Linking.addEventListener).not.toHaveBeenCalled();
  ui.unmount();
});

test('another phase in the same JS runtime is not a fresh App', async () => {
  const props = profile();
  expect(await finish(start({ props }))).toBe(true);
  const run = start({ props: { ...props, rfPermissionPhase: 'restored' } });
  expect(await finish(run)).toBe(false); expectFailed(run); expect(run.factory).not.toHaveBeenCalled();
});

test('restored uses injected real-flow entrypoint and fresh probe, then 17-zero close; does not claim previous phases', async () => {
  const flow = jest.fn(passingFlow);
  const run = start({ props: profile('restored'), flow });
  expect(await finish(run)).toBe(true);
  expect(flow).toHaveBeenCalledWith('rf-io', run.first, SERVICE, expect.any(Function), expect.any(Function), NAME);
  expect(run.first.close).toHaveBeenCalledTimes(1);
  expect(run.lines).toContain(`${P}:restored:flow:restored:rf-io:passed:software-only`);
  expect(run.lines.at(-1)).toBe(`${P}:passed:phase=restored:runId=${run.props.rfPermissionRunId}`);
  expect(run.lines.some((line) => /:denied:|:active:/.test(line))).toBe(false);
});

test.each(['missing', 'duplicate', 'failure', 'close'] as const)('restored rejects %s evidence', async (fault) => {
  const first = deniedProbe();
  if (fault === 'close') first.close.mockRejectedValue(new Error('close failed'));
  const flow: PermissionTransitionFlow = async (_m, _p, _s, report) => {
    if (fault !== 'missing') report('rf-io:passed:software-only');
    if (fault === 'duplicate') report('rf-io:passed:software-only');
    if (fault === 'failure') report('rf-io:failed:injected');
  };
  const run = start({ props: profile('restored'), first, flow });
  expect(await finish(run)).toBe(false); expectFailed(run);
});

test('active baseline closes before Probe B; canonical route/two accepted reads/actual pending snapshot; surviving 8s fails without closing B', async () => {
  const flow = jest.fn(passingFlow);
  const run = start({ props: profile('active'), flow });
  expect(await finish(run)).toBe(false);
  expect(flow).toHaveBeenCalledTimes(1);
  expect(run.first.close).toHaveBeenCalledTimes(1);
  expect(run.factory).toHaveBeenCalledTimes(2);
  const route = 'characteristicId=20:connectionId=52005:gattSessionId=52007';
  expect(run.lines).toContain(`${P}:active:discovery-ready:${route}:runId=${run.props.rfPermissionRunId}`);
  expect(run.active.readRfCharacteristic.mock.calls).toEqual([
    ['52008', '52005', '52007', '20'], ['52009', '52005', '52007', '20'],
  ]);
  expect(run.lines).toContain(`${P}:active:reads-accepted:activeCommandId=52008:queuedCommandId=52009:${route}:runId=${run.props.rfPermissionRunId}`);
  expect(run.lines).toContain(`${P}:active:pending-resources:${JSON.stringify(pending())}`);
  expect(run.lines.findIndex((line) => line === `${P}:active:baseline-passed`)).toBeLessThan(run.lines.findIndex((line) => line.includes(':active:event:')));
  expect(run.active.snapshotRfResources).toHaveBeenCalledTimes(1);
  expect(run.active.close).not.toHaveBeenCalled();
  expect(run.lines.some((line) => line.includes('state=close'))).toBe(false);
  expect(run.lines.some((line) => line.includes('8000ms'))).toBe(true);
  expectFailed(run);
});

test('no pre-arm reads; a foreign or duplicate canonical arm fails rather than authorize more reads', async () => {
  for (const kind of ['timeout', 'foreign', 'duplicate']) {
    const active = activeProbe();
    const run = start({ props: profile('active'), active, autoArm: false, onReport: (line, props) => {
      if (!line.startsWith(`${P}:active:discovery-ready:`)) return;
      expect(active.readRfCharacteristic).not.toHaveBeenCalled();
      if (kind === 'foreign') signal(arm('foreign'));
      if (kind === 'duplicate') { signal(arm(props.rfPermissionRunId!)); signal(arm(props.rfPermissionRunId!)); }
    } });
    expect(await finish(run)).toBe(false); expectFailed(run);
    expect(active.readRfCharacteristic).not.toHaveBeenCalled();
  }
});

test('early arm before discovery fails closed', async () => {
  const run = start({ props: profile('active'), onReport: (line, props) => {
    if (line.includes(':state=start')) signal(arm(props.rfPermissionRunId!));
  } });
  expect(await finish(run)).toBe(false); expectFailed(run); expect(run.factory).not.toHaveBeenCalled();
});

test.each(['pendingIo', 'watchdogs', 'connections'] as const)('actual native %s count must match held + queued, not inferred from two dispatches', async (key) => {
  const active = activeProbe();
  active.snapshotRfResources.mockResolvedValue({ ...pending(), [key]: 0 });
  const run = start({ props: profile('active'), active });
  expect(await finish(run)).toBe(false); expectFailed(run);
  expect(run.lines.some((line) => line.includes(':reads-accepted:') || line.includes(':pending-resources:'))).toBe(false);
  expect(active.close).not.toHaveBeenCalled();
});

test.each(['commandCompleted', 'characteristicRead', 'adapterSnapshotChanged'] as const)('surviving %s is audited and fails, never passes or cancels/closes', async (kind) => {
  const active = activeProbe();
  const event: RfScanEvent = { kind, commandId: '52008', connectionId: '52005', gattSessionId: '52007', characteristicId: '20' };
  const run = start({ props: profile('active'), active, onReport: (line) => {
    if (line.startsWith(`${P}:active:pending-resources:`)) active.emit(event);
  } });
  expect(await finish(run)).toBe(false); expectFailed(run);
  expect(run.lines).toContain(`${P}:active:event:${JSON.stringify(event)}`);
  expect(active.close).not.toHaveBeenCalled();
});

test('read completion synchronously inside first accepted dispatch prevents queued read and readiness', async () => {
  const active = activeProbe();
  active.readRfCharacteristic.mockImplementation((id) => { active.emit(terminal(id)); return undefined; });
  const run = start({ props: profile('active'), active });
  expect(await finish(run)).toBe(false); expectFailed(run);
  expect(active.readRfCharacteristic).toHaveBeenCalledTimes(1);
  expect(active.snapshotRfResources).not.toHaveBeenCalled();
});

test('GATT audit emits exactly one compact canonical subtree within 3500 UTF-8 bytes while validating original target', async () => {
  const active = activeProbe();
  active.gattServices.push({ id: '100', uuid: '180a0000-0000-4000-8000-000000000000', primary: true,
    characteristics: Array.from({ length: 100 }, (_, i) => ({ ...active.gattServices[0].characteristics[0], id: String(1000 + i) })) });
  const run = start({ props: profile('active'), active });
  await finish(run);
  const records = run.lines.filter((line) => line.includes(':active:event:') && line.includes('gattDiscovered'));
  expect(records).toHaveLength(1);
  const record = JSON.parse(records[0].split(':active:event:')[1]);
  expect(record.auditSchema).toBe('active-gatt-canonical-v1');
  expect(record.gattServices).toEqual([active.gattServices[0]]);
  expect(record.gattSummary).toEqual({ serviceCount: 2, characteristicCount: 105, descriptorCount: 2 });
  expect(Buffer.byteLength(`BLEHUB_NITRO_RUNTIME ${records[0]}`, 'utf8')).toBeLessThanOrEqual(3500);
  expect(active.gattServices).toHaveLength(2);
  expect(active.readRfCharacteristic).toHaveBeenCalledTimes(2);
});

test('incorrect full-source ordinary RW properties cannot authorize active reads', async () => {
  const active = activeProbe();
  active.gattServices[0].characteristics[0].writableWithResponse = false;
  const run = start({ props: profile('active'), active });
  expect(await finish(run)).toBe(false); expectFailed(run);
  expect(active.readRfCharacteristic).not.toHaveBeenCalled();
});

test('active waits for actual native snapshot resolution before publishing accepted/pending evidence', async () => {
  const active = activeProbe();
  let resolveSnapshot!: (value: RfResourceSnapshot) => void;
  active.snapshotRfResources.mockReturnValue(new Promise((resolve) => { resolveSnapshot = resolve; }));
  const run = start({ props: profile('active'), active });
  await jest.advanceTimersByTimeAsync(100);
  expect(active.readRfCharacteristic).toHaveBeenCalledTimes(2);
  expect(run.lines.some((line) => line.includes(':reads-accepted:') || line.includes(':pending-resources:'))).toBe(false);
  resolveSnapshot(pending());
  await jest.advanceTimersByTimeAsync(25);
  expect(run.lines).toContain(`${P}:active:pending-resources:${JSON.stringify(pending())}`);
  expect(await finish(run)).toBe(false); expectFailed(run);
});

test('a hung native snapshot is also bounded by the eight-second surviving-App deadline', async () => {
  const active = activeProbe();
  active.snapshotRfResources.mockReturnValue(new Promise(() => {}));
  const run = start({ props: profile('active'), active });
  expect(await finish(run)).toBe(false); expectFailed(run);
  expect(run.lines.some((line) => line.includes('active native snapshot timed out'))).toBe(true);
  expect(active.close).not.toHaveBeenCalled();
});

test('native watchdog Timeout is logged unchanged and is a failure, not proof of process death', async () => {
  const active = activeProbe();
  const event: RfScanEvent = { ...terminal('52008'), error: { category: 'timeout', message: 'native watchdog',
    platformDetails: { platform: 'platform_android', name: 'AndroidBleBackend', code: 'read-timeout' } } };
  const run = start({ props: profile('active'), active, onReport: (line) => {
    if (line.startsWith(`${P}:active:pending-resources:`)) active.emit(event);
  } });
  expect(await finish(run)).toBe(false); expectFailed(run);
  expect(run.lines).toContain(`${P}:active:event:${JSON.stringify(event)}`);
  expect(active.close).not.toHaveBeenCalled();
});

test('baseline close failure prevents Probe B construction and baseline-passed marker', async () => {
  const first = deniedProbe();
  first.close.mockRejectedValue(new Error('baseline close failed'));
  const run = start({ props: profile('active'), first });
  expect(await finish(run)).toBe(false); expectFailed(run);
  expect(run.factory).toHaveBeenCalledTimes(1);
  expect(run.lines).not.toContain(`${P}:active:baseline-passed`);
});

test('oversized UTF-8 canonical GATT fails without logging truncated or duplicate database records', async () => {
  const active = activeProbe();
  active.discoverRfGatt.mockImplementation((commandId, connectionId, gattSessionId) => {
    active.emit({ kind: 'gattDiscovered', commandId, connectionId, gattSessionId,
      gattServices: active.gattServices, localName: '界'.repeat(1500) });
    active.emit(terminal(commandId));
  });
  const run = start({ props: profile('active'), active });
  expect(await finish(run)).toBe(false); expectFailed(run);
  expect(run.lines.some((line) => line.includes('3500-byte UTF-8 budget'))).toBe(true);
  expect(run.lines.some((line) => line.includes(':active:event:') && line.includes('gattDiscovered'))).toBe(false);
  expect(active.readRfCharacteristic).not.toHaveBeenCalled();
});

test('late denied callback during close prevents phase success', async () => {
  const first = deniedProbe();
  first.close.mockImplementation(async () => { first.emit(terminal('51002')); });
  const run = start({ first });
  expect(await finish(run)).toBe(false); expectFailed(run);
});

test('current-phase UI exposes App checks and death-observability disclaimer without an App import', async () => {
  const props = profile('restored');
  jest.mocked(createBleHubProbe).mockReturnValue(asProbe(deniedProbe()));
  const view = render(<RfPermissionTransition {...props} runFlow={passingFlow} />);
  await act(async () => { await jest.runAllTimersAsync(); });
  expect(view.getByTestId('rf-permission-transition-page')).toBeTruthy();
  expect(view.getByTestId('rf-permission-transition-result').props.children).toBe('Current phase App checks passed');
  expect(view.getByTestId('rf-permission-transition-failure').props.children).toBe('No failure reported.');
  expect(view.getByText(/old terminals and close are unobservable/)).toBeTruthy();
});
