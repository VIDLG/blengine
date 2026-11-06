import React from 'react';
import { act, render, screen } from '@testing-library/react-native';
import { Linking, Platform } from 'react-native';
import App, { createRfEventStream } from './App';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { RfResourceSnapshot, RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';

import {
  RecoveryReadyBarrier, RF_RESOURCE_FIELDS, recoveryTenId, runRfRecoveryTen,
  validateRecoveryResources, type RecoveryStreamFactory,
} from './rf_recovery_ten';

jest.mock('react-native/Libraries/Settings/Settings.ios', () => ({ __esModule: true, default: { get: jest.fn() } }));
jest.mock('@blehub/react-native-internal', () => ({ createBleHubProbe: jest.fn() }));
jest.mock('./multi_rf', () => ({ runRfMulti: jest.fn(), runRfMultiSoak: jest.fn() }));
jest.mock('./multi_fault', () => ({ runRfMultiFault: jest.fn() }));

const SERVICE = 'fd500000-0000-4000-8000-000000000000';
const NAME = 'BleHubNrf52840';
const CHARACTERISTICS = ['fff1', 'fff2', 'fff5', 'fff3', 'fff4'].map((prefix, index) => ({
  uuid: `${prefix}0000-0000-4000-8000-000000000000`,
  readable: index !== 4, writableWithResponse: index === 0 || index === 4,
  writableWithoutResponse: index === 0 || index === 4,
  notifiable: index === 1, indicatable: index === 1 || index === 2,
  descriptorUuids: index === 1 || index === 2 ? ['00002902-0000-1000-8000-00805f9b34fb'] : [],
}));
const RUN = '12345678-1234-4234-8234-123456789abc';
const APPLE_PASSIVE_ERROR = {
  category: 'connection', message: 'CoreBluetooth connection ended unexpectedly',
  platformDetails: { platform: 'platform_apple', code: '7', name: 'CBErrorDomain', message: 'device disconnected' },
} as const;
const signals = new Set<(event: { url: string }) => void>();
const signal = (url: string): void => { for (const listener of signals) listener({ url }); };
const readyUrl = (run: string, cycle: number): string => `blehub-rn-spike://recover/${run}/${cycle}`;
const streamFactory: RecoveryStreamFactory = (probe, reject, connection) =>
  createRfEventStream(probe, true, true, new Set([connection]), 1024, reject, true);
function counts(android: boolean, phase: 'warm' | 'active' | 'closed'): RfResourceSnapshot {
  const result = Object.fromEntries(RF_RESOURCE_FIELDS.map((key) => [key, 0])) as unknown as RfResourceSnapshot;
  if (phase === 'closed') return result;
  Object.assign(result, { hubRoots: 1, handoffRoots: 1, ingressRoots: 1, backendLeases: 1, adapterDelegates: 1, listeners: 1, peerCacheEntries: 1, workerThreads: android ? 2 : 0, lifecycleOwners: android ? 1 : 0 });
  if (phase === 'active') Object.assign(result, { connections: 1, subscriptions: 1, gattAttributes: 8, callbackRoutes: 4 });
  else if (!android) Object.assign(result, { gattAttributes: 8, callbackRoutes: 4 });
  return result;
}

type Fault = 'none' | 'closed-leak' | 'snapshot-error' | 'snapshot-empty-rejection' | 'snapshot-false-rejection' | 'terminal-first' | 'duplicate-terminal' | 'old-epoch' | 'old-callback' | 'late-old-route' | 'wrong-service' | 'wrong-name' | 'read-value' | 'active-zero' | 'close-rejected';
function fakeProbe(android: boolean, epoch: number, fault: Fault = 'none', scanDelayMs = 0, advertisementFields: Partial<RfScanEvent> = {}, passiveFields?: Partial<RfScanEvent>, passiveBeforeValue = false) {
  let listener: ((event: RfScanEvent) => void) | undefined;
  let phase: 'warm' | 'active' | 'closed' = 'warm';
  let snapshotCount = 0;
  let passiveTimer: ReturnType<typeof setTimeout> | undefined;
  const emit = (event: RfScanEvent): void => listener?.(event);
  const complete = (commandId: string): void => emit({ kind: 'commandCompleted', commandId });
  const probe = {
    emit,
    setRfScanListener: jest.fn((value: (event: RfScanEvent) => void) => { listener = value; }),
    // Retain the old callback as an adversarial seam: real native handoff must reject it at close.
    removeRfScanListener: jest.fn(),
    initialize: jest.fn((commandId: string) => {
      if (fault === 'terminal-first') complete(commandId);
      emit({ kind: 'adapterSnapshotChanged', commandId, adapterState: 'PoweredOn', scanAuthorization: 'Authorized' });
      if (fault !== 'terminal-first') complete(commandId);
      if (fault === 'duplicate-terminal') complete(commandId);
      if (fault === 'old-epoch') complete(recoveryTenId(epoch - 1, 12));
      return null;
    }),
    startRfScan: jest.fn((commandId: string, scanSessionId: string) => {
      const start = (): void => {
        emit({ kind: 'scanStarted', commandId, scanSessionId }); complete(commandId);
        emit({ kind: 'advertisementObserved', scanSessionId, peripheralId: '42', advertisedServices: [fault === 'wrong-service' ? RUN : SERVICE], localName: fault === 'wrong-name' ? 'OtherFixture' : NAME, rssi: -42, ...advertisementFields });
      };
      if (scanDelayMs) setTimeout(start, scanDelayMs); else start();
      return null;
    }),
    stopRfScan: jest.fn((commandId: string, scanSessionId: string) => { emit({ kind: 'scanStopped', commandId, scanSessionId }); complete(commandId); return null; }),
    connectRfPeripheral: jest.fn((commandId: string, connectionId: string) => {
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Connecting' });
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Connected' }); complete(commandId);
      if (fault === 'late-old-route' && commandId === recoveryTenId(epoch, 11)) emit({ kind: 'notificationReceived', connectionId: recoveryTenId(epoch, 4), subscriptionId: recoveryTenId(epoch, 8), value: new Uint8Array([0xef]).buffer });
      return null;
    }),
    discoverRfGatt: jest.fn((commandId: string, connectionId: string, gattSessionId: string) => {
      emit({ kind: 'gattDiscovered', commandId, connectionId, gattSessionId, gattServices: [{ id: '19', uuid: SERVICE, primary: true,
        characteristics: CHARACTERISTICS.map((c, i) => ({ ...c, id: String(20 + i), descriptors: c.descriptorUuids.map((uuid, j) => ({ id: String(30 + i * 2 + j), uuid })) })),
      }] }); complete(commandId); return null;
    }),
    setRfNotifications: jest.fn((commandId: string, connectionId: string, gattSessionId: string, subscriptionId: string, characteristicId: string) => {
      phase = 'active';
      emit({ kind: 'notificationStateChanged', commandId, connectionId, gattSessionId, subscriptionId, characteristicId, notificationState: 'Enabled', actualNotificationMode: 'Notification' }); complete(commandId);
      const passive = (): void => {
        if (!passiveBeforeValue) phase = 'warm';
        emit({ kind: 'connectionStateChanged', connectionId, ...(passiveFields ?? (android ? { error: { category: 'connection', message: 'physical device loss' } } : { connectionState: 'Disconnected' })) });
      };
      if (passiveBeforeValue) passive();
      emit({ kind: 'notificationReceived', connectionId, gattSessionId, subscriptionId, characteristicId, value: new Uint8Array([0xef]).buffer });
      if (!passiveBeforeValue) passiveTimer = setTimeout(passive, 100);
      return null;
    }),
    readRfCharacteristic: jest.fn((commandId: string, connectionId: string, gattSessionId: string, characteristicId: string) => {
      emit({ kind: 'characteristicRead', commandId, connectionId, gattSessionId, characteristicId, value: new Uint8Array([fault === 'read-value' ? 1 : 0]).buffer }); complete(commandId); return null;
    }),
    disconnectRfPeripheral: jest.fn((commandId: string, connectionId: string) => { phase = 'warm'; emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Disconnected' }); complete(commandId); return null; }),
    snapshotRfResources: jest.fn(async () => {
      snapshotCount += 1;
      if (fault === 'snapshot-error' && snapshotCount === 2) throw new Error('actual snapshot failed');
      if (fault === 'snapshot-empty-rejection' && snapshotCount === 2) throw undefined;
      if (fault === 'snapshot-false-rejection' && snapshotCount === 2) throw false;
      const result = counts(android, phase);
      if (fault === 'closed-leak' && phase === 'closed') result.callbackRoutes = 1;
      if (fault === 'active-zero' && phase === 'active') result.gattAttributes = 0;
      return result;
    }),
    close: jest.fn(async () => {
      if (fault === 'close-rejected') throw new Error('native dispose rejected');
      if (passiveTimer !== undefined) clearTimeout(passiveTimer);
      phase = 'closed';
    }),
  };
  return probe;
}

afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); });
beforeEach(() => {
  signals.clear(); jest.useFakeTimers();
  jest.spyOn(Linking, 'addEventListener').mockImplementation((_type, listener) => {
    signals.add(listener); return { remove: () => { signals.delete(listener); } } as any;
  });
});
let runSequence = 0;
async function execute(android: boolean, fault: Fault = 'none', signalFault?: 'early' | 'foreign' | 'duplicate' | 'order' | 'missing', options: {
  scanDelayMs?: number;
  advertisementFields?: Partial<RfScanEvent>;
  passiveFields?: Partial<RfScanEvent>;
  passiveBeforeValue?: boolean;
  waitFault?: 'cancel-first' | 'cancel-next' | 'current-stream' | 'retired-stream';
} = {}) {
  Object.defineProperty(Platform, 'OS', { configurable: true, value: android ? 'android' : 'ios' });
  const runId = `12345678-1234-4234-8234-${String(++runSequence).padStart(12, '0')}`;
  const ledger: string[] = [];
  const probes: ReturnType<typeof fakeProbe>[] = [];
  const scanDispatchTimes: number[] = [];
  let cancelled = false;
  const factory = jest.fn(() => {
    if (fault === 'old-callback' && probes.length === 1) probes[0].emit({ kind: 'commandCompleted', commandId: recoveryTenId(0, 12) });
    const probe = fakeProbe(android, probes.length, fault === 'old-epoch' && probes.length === 0 ? 'none' : fault, options.scanDelayMs, options.advertisementFields, options.passiveFields, options.passiveBeforeValue);
    const start = probe.startRfScan.getMockImplementation()!;
    probe.startRfScan.mockImplementation((...args) => { scanDispatchTimes.push(performance.now()); return start(...args); });
    probes.push(probe); return probe as any;
  });
  const report = (line: string): void => {
    ledger.push(line);
    const budget = /^rf-recovery-ten:cycle:(\d):scan-budget-wait:/.exec(line);
    if (budget && options.waitFault) {
      const epoch = Number(budget[1]);
      if (options.waitFault === 'cancel-first' && epoch === 0 || options.waitFault === 'cancel-next' && epoch === 1) {
        setTimeout(() => { cancelled = true; }, 100);
      } else if (options.waitFault === 'current-stream' && epoch === 0 || options.waitFault === 'retired-stream' && epoch === 1) {
        setTimeout(() => probes[0].emit({ kind: 'commandCompleted', commandId: recoveryTenId(0, 12) }), 100);
      }
    }
    if (signalFault === 'early' && line === 'rf-recovery-ten:cycle:0:start') signal(readyUrl(runId, 0));
    const match = /^rf-recovery-ten:cycle:(\d):recovery-awaiting:/.exec(line);
    if (!match || signalFault === 'missing') return;
    const cycle = Number(match[1]);
    signal(readyUrl(signalFault === 'foreign' ? RUN : runId, signalFault === 'order' ? cycle + 1 : cycle));
    if (signalFault === 'duplicate') signal(readyUrl(runId, cycle));
  };
  const done = runRfRecoveryTen(factory, streamFactory, { rfServiceUuid: SERVICE, rfExpectedLocalName: NAME, rfRecoveryRunId: runId }, report, () => cancelled);
  await jest.runAllTimersAsync();
  return { passed: await done, probes, ledger, factory, runId, scanDispatchTimes };
}

describe('strict ten-cycle native lifecycle orchestration', () => {
  test.each([true, false])('accepts exclusive passive Result branches and marks the actual result (android=%s)', async (android) => {
    for (const passiveFields of [{ error: android ? { ...APPLE_PASSIVE_ERROR, platformDetails: { ...APPLE_PASSIVE_ERROR.platformDetails, platform: 'platform_android' as const } } : APPLE_PASSIVE_ERROR }, { connectionState: 'Disconnected' }]) {
      const result = await execute(android, 'none', undefined, { passiveFields });
      expect(result.passed).toBe(true);
      const error = 'error' in passiveFields ? 'connection' : 'none';
      for (let cycle = 0; cycle < 10; cycle += 1) {
        expect(result.ledger).toContain(`rf-recovery-ten:cycle:${cycle}:passive-disconnect:passed:commandId=none:connectionId=${recoveryTenId(cycle, 4)}:error=${error}`);
      }
      const event = result.ledger.filter((line) => line.startsWith('rf-recovery-ten:event:')).map((line) => JSON.parse(line.slice('rf-recovery-ten:event:'.length))).find((e) => e.kind === 'connectionStateChanged' && e.commandId == null);
      expect(event.error).toEqual('error' in passiveFields ? passiveFields.error : undefined);
      expect(event.connectionState).toBe(error === 'none' ? 'Disconnected' : undefined);
    }
  });
  test.each([
    { error: { ...APPLE_PASSIVE_ERROR, category: 'gatt' } },
    { error: APPLE_PASSIVE_ERROR, connectionState: 'Disconnected' },
    { error: APPLE_PASSIVE_ERROR, connectionState: 'Connected' },
    { error: APPLE_PASSIVE_ERROR, commandId: '10005' },
    { error: APPLE_PASSIVE_ERROR, commandId: '19999' },
    { error: APPLE_PASSIVE_ERROR, connectionId: '10010' },
    { error: APPLE_PASSIVE_ERROR, gattSessionId: '10006' },
    { error: { ...APPLE_PASSIVE_ERROR, platformDetails: { ...APPLE_PASSIVE_ERROR.platformDetails, platform: 'platform_android' } } },
    { error: { ...APPLE_PASSIVE_ERROR, platformDetails: { ...APPLE_PASSIVE_ERROR.platformDetails, message: 'x'.repeat(513) } } },
    { connectionState: 'Disconnecting' },
    { connectionState: 'Disconnected', commandId: '10005' },
    { connectionState: 'Disconnected', commandId: '10000' },
    { connectionState: 'Disconnected', commandId: '10012', connectionId: '10004' },
    {},
  ] as Partial<RfScanEvent>[])('rejects invalid passive Result/route without recovery: %j', async (passiveFields) => {
    const result = await execute(false, 'none', undefined, { passiveFields });
    expect(result.passed).toBe(false);
    expect(result.probes).toHaveLength(1);
    expect(result.probes[0].connectRfPeripheral).toHaveBeenCalledTimes(1);
    expect(result.ledger.some((line) => line.includes(':passive-disconnect:passed:'))).toBe(false);
  });
  test('rejects an otherwise valid passive failure before notification value', async () => {
    const result = await execute(false, 'none', undefined, { passiveFields: { error: APPLE_PASSIVE_ERROR }, passiveBeforeValue: true });
    expect(result.passed).toBe(false);
    expect(result.ledger.at(-1)).toContain('before notification value');
    expect(result.probes[0].connectRfPeripheral).toHaveBeenCalledTimes(1);
  });
  test.each([true, false])('ten fresh probes, real projected ledger and five snapshots each (android=%s)', async (android) => {
    const result = await execute(android);
    expect(result.passed).toBe(true); expect(result.probes).toHaveLength(10);
    for (let cycle = 0; cycle < 10; cycle += 1) {
      const probe = result.probes[cycle];
      expect(probe.initialize).toHaveBeenCalledWith(recoveryTenId(cycle, 0));
      expect(probe.discoverRfGatt.mock.calls).toEqual([[recoveryTenId(cycle, 7), recoveryTenId(cycle, 4), recoveryTenId(cycle, 6)], [recoveryTenId(cycle, 13), recoveryTenId(cycle, 10), recoveryTenId(cycle, 14)]]);
      expect(probe.readRfCharacteristic).toHaveBeenCalledWith(recoveryTenId(cycle, 15), recoveryTenId(cycle, 10), recoveryTenId(cycle, 14), '20');
      expect(probe.close).toHaveBeenCalledTimes(1); expect(probe.snapshotRfResources).toHaveBeenCalledTimes(5);
      const resources = result.ledger.filter((s) => s.startsWith(`rf-recovery-ten:resources:cycle=${cycle}:`));
      expect(resources.map((s) => s.split(':phase=')[1].split(':')[0])).toEqual(['baseline', 'active', 'passive', 'recovery-disconnected', 'closed']);
      expect(Object.values(JSON.parse(resources[4].split(':snapshot=')[1]))).toEqual(Array(17).fill(0));
      const closedIndex = result.ledger.indexOf(resources[4]);
      expect(closedIndex).toBeLessThan(result.ledger.indexOf(`rf-recovery-ten:cycle:${cycle}:passed`));
      if (cycle < 9) expect(closedIndex).toBeLessThan(result.ledger.indexOf(`rf-recovery-ten:cycle:${cycle + 1}:start`));
      const events = result.ledger.filter((s) => s.startsWith('rf-recovery-ten:event:')).map((s) => JSON.parse(s.slice('rf-recovery-ten:event:'.length))).filter((e) => e.cycle === cycle);
      expect(events.filter((e) => e.kind === 'commandCompleted').map((e) => e.commandId)).toEqual([0, 2, 3, 5, 7, 9, 11, 13, 15, 12].map((n) => recoveryTenId(cycle, n)));
      expect(events.find((e) => e.kind === 'notificationReceived').value).toEqual([239]);
      expect(events.find((e) => e.kind === 'characteristicRead').value).toEqual([0]);
      expect(events.find((e) => e.kind === 'advertisementObserved').rssi).toBe(-42);
      expect(events.find((e) => e.kind === 'gattDiscovered').gattServices[0].characteristics).toHaveLength(5);
    }
    const runtime = result.ledger.filter((s) => s.startsWith('rf-recovery-ten:runtime:'));
    expect(runtime).toHaveLength(2); expect(runtime[0].replace('phase=start', 'phase=close')).toBe(runtime[1]);
    expect(result.ledger.at(-1)).toBe(`rf-recovery-ten:passed:cycles=10:runId=${result.runId}`);
    expect(signals.size).toBe(0);
  });
  test.each([true, false])('matching fd50 service accepts absent name/RSSI and peripheral zero (android=%s)', async (android) => {
    const result = await execute(android, 'none', undefined, {
      advertisementFields: { localName: undefined, rssi: undefined, peripheralId: '0' },
    });
    expect(result.passed).toBe(true);
    expect(result.probes).toHaveLength(10);
    for (let cycle = 0; cycle < 10; cycle += 1) {
      const probe = result.probes[cycle];
      expect(probe.connectRfPeripheral.mock.calls).toEqual([
        [recoveryTenId(cycle, 5), recoveryTenId(cycle, 4), '0'],
        [recoveryTenId(cycle, 11), recoveryTenId(cycle, 10), '0'],
      ]);
      expect(probe.snapshotRfResources).toHaveBeenCalledTimes(5);
    }
    const advertisements = result.ledger.filter((line) => line.startsWith('rf-recovery-ten:event:'))
      .map((line) => JSON.parse(line.slice('rf-recovery-ten:event:'.length)))
      .filter((event) => event.kind === 'advertisementObserved');
    expect(advertisements).toHaveLength(10);
    for (const event of advertisements) {
      expect(event.advertisedServices).toEqual([SERVICE]);
      expect(event.peripheralId).toBe('0');
      expect(event).not.toHaveProperty('localName');
      expect(event).not.toHaveProperty('rssi');
    }
  });
  test('explicit wrong name is rejected even with absent RSSI and peripheral zero', async () => {
    const result = await execute(false, 'wrong-name', undefined, {
      advertisementFields: { rssi: undefined, peripheralId: '0' },
    });
    expect(result.passed).toBe(false);
    expect(result.probes).toHaveLength(1);
    expect(result.probes[0].connectRfPeripheral).not.toHaveBeenCalled();
    expect(result.probes[0].close).toHaveBeenCalledTimes(1);
    expect(result.ledger.at(-1)).toContain('fixture advertisement timed out');
  });
  test.each([0, 500])('Android waits 30s then >=7s from observed native start (native delay=%s)', async (scanDelayMs) => {
    const result = await execute(true, 'none', undefined, { scanDelayMs });
    expect(result.passed).toBe(true);
    expect(result.scanDispatchTimes).toHaveLength(10);
    expect(result.scanDispatchTimes[0]).toBeGreaterThanOrEqual(30000);
    for (let cycle = 1; cycle < 10; cycle += 1) {
      expect(result.scanDispatchTimes[cycle] - result.scanDispatchTimes[cycle - 1]).toBeGreaterThanOrEqual(7000 + scanDelayMs);
    }
    expect(result.probes.every((probe) => probe.startRfScan.mock.calls.length === 1)).toBe(true);
    expect(result.ledger.filter((line) => line.includes(':scan-budget-policy:'))).toHaveLength(1);
    for (const phase of ['wait', 'dispatch', 'started']) expect(result.ledger.filter((line) => line.includes(`:scan-budget-${phase}:`))).toHaveLength(10);
  });
  test('iOS has neither budget markers nor a 30s quiet window', async () => {
    const result = await execute(false);
    expect(result.passed).toBe(true);
    expect(result.scanDispatchTimes[0]).toBe(0);
    expect(result.scanDispatchTimes[1] - result.scanDispatchTimes[0]).toBeLessThan(7000);
    expect(result.ledger.some((line) => line.includes('scan-budget'))).toBe(false);
  });
  test.each(['cancel-first', 'cancel-next', 'current-stream', 'retired-stream'] as const)('budget waiting stays cancellable and guards all streams: %s', async (waitFault) => {
    const result = await execute(true, 'none', undefined, { waitFault });
    const next = waitFault === 'cancel-next' || waitFault === 'retired-stream';
    expect(result.passed).toBe(false);
    expect(result.probes).toHaveLength(next ? 2 : 1);
    expect(result.probes.at(-1)!.startRfScan).not.toHaveBeenCalled();
    expect(result.probes.every((probe) => probe.close.mock.calls.length === 1)).toBe(true);
    expect(result.ledger.some((line) => line.startsWith('rf-recovery-ten:passed:'))).toBe(false);
    expect(signals.size).toBe(0);
  });
  test('Android UI and console expose conservative policy and exact quiet wait before scan', async () => {
    Object.defineProperty(Platform, 'OS', { configurable: true, value: 'android' });
    const probe = fakeProbe(true, 0);
    jest.mocked(createBleHubProbe).mockReturnValue(probe as any);
    const log = jest.spyOn(console, 'log').mockImplementation(() => {});
    const view = render(<App runtimeMode="rf-recovery-ten" rfServiceUuid={SERVICE} rfExpectedLocalName={NAME} rfRecoveryRunId="12345678-1234-4234-8234-999999999998" />);
    await act(async () => { await jest.advanceTimersByTimeAsync(29999); });
    expect(probe.startRfScan).not.toHaveBeenCalled();
    const waiting = log.mock.calls.map(([line]) => String(line)).find((line) => line.includes(':scan-budget-wait:'))!;
    expect(waiting).toMatch(/waitMs=30000:monotonicMs=0:policy=android-conservative-test-only-v1:quietMs=30000:minStartIntervalMs=7000$/);
    expect(screen.getByText(waiting.replace('BLEHUB_NITRO_RUNTIME ', ''))).toBeTruthy();
    expect(log.mock.calls.some(([line]) => String(line).includes(':scan-budget-policy:version=1:'))).toBe(true);
    await act(async () => { await jest.advanceTimersByTimeAsync(1); });
    expect(probe.startRfScan).toHaveBeenCalledTimes(1);
    view.unmount();
    await act(async () => { await jest.runAllTimersAsync(); });
  });
  test.each(['closed-leak', 'snapshot-error', 'snapshot-empty-rejection', 'snapshot-false-rejection', 'terminal-first', 'duplicate-terminal', 'late-old-route', 'wrong-service', 'wrong-name', 'read-value', 'active-zero', 'close-rejected'] as const)('fails closed without advancing on %s', async (fault) => {
    const result = await execute(false, fault);
    expect(result.passed).toBe(false); expect(result.probes).toHaveLength(1);
    const reason = {
      'closed-leak': 'closed resource callbackRoutes', 'snapshot-error': 'actual snapshot failed',
      'snapshot-empty-rejection': 'undefined', 'snapshot-false-rejection': 'false',
      'terminal-first': 'terminal duplicate or before domain', 'duplicate-terminal': 'terminal duplicate or before domain',
      'late-old-route': 'late old connection route', 'wrong-service': 'fixture advertisement timed out',
      'wrong-name': 'fixture advertisement timed out', 'read-value': 'recovery RW initial read mismatch',
      'active-zero': 'active GATT positive control missing', 'close-rejected': 'native dispose rejected',
    }[fault];
    expect(result.ledger.at(-1)).toContain(reason);
    expect(result.probes[0].close).toHaveBeenCalledTimes(1);
    expect(result.ledger.some((s) => s === 'rf-recovery-ten:cycle:0:passed' || s.startsWith('rf-recovery-ten:passed:'))).toBe(false);
    expect(signals.size).toBe(0);
  });
  test.each(['old-epoch', 'old-callback'] as const)('rejects late callback from retired cycle: %s', async (fault) => {
    const result = await execute(true, fault);
    expect(result.passed).toBe(false); expect(result.probes).toHaveLength(2);
    expect(result.ledger).not.toContain('rf-recovery-ten:cycle:1:passed');
    expect(result.probes.every((p) => p.close.mock.calls.length === 1)).toBe(true);
    expect(result.ledger.some((s) => s.includes('nativeLate'))).toBe(false);
  });
  test.each(['early', 'foreign', 'duplicate', 'order', 'missing'] as const)('does not reconnect before physical readiness: %s', async (fault) => {
    const result = await execute(false, 'none', fault);
    expect(result.passed).toBe(false); expect(result.probes).toHaveLength(1);
    expect(result.probes[0].connectRfPeripheral.mock.calls.length).toBeLessThanOrEqual(1);
    expect(result.ledger).not.toContain('rf-recovery-ten:cycle:0:passed');
  });
  test('callback after the final result emits a failure, without claiming native late-ingress evidence', async () => {
    const result = await execute(false);
    expect(result.passed).toBe(true);
    result.probes[9].emit({ kind: 'commandCompleted', commandId: recoveryTenId(9, 12) });
    expect(result.ledger.at(-1)).toBe('rf-recovery-ten:failed:JS callback after resolved native close');
  });
  test('same run cannot create a second set of probes in this runtime', async () => {
    const result = await execute(false);
    const factory = jest.fn(); const report = jest.fn();
    expect(await runRfRecoveryTen(factory, streamFactory, { rfServiceUuid: SERVICE, rfRecoveryRunId: result.runId }, report, () => false)).toBe(false);
    expect(factory).not.toHaveBeenCalled();
    expect(report.mock.calls.at(-1)?.[0]).toContain('cannot be rerun');
  });
  test('runId validation occurs before creating a probe', async () => {
    const factory = jest.fn(); const report = jest.fn();
    expect(await runRfRecoveryTen(factory, streamFactory, { rfServiceUuid: SERVICE, rfRecoveryRunId: RUN.toUpperCase() }, report, () => false)).toBe(false);
    expect(factory).not.toHaveBeenCalled();
  });
  test('keeps the results page and disables strict-mode rerun and manual close', async () => {
    Object.defineProperty(Platform, 'OS', { configurable: true, value: 'ios' });
    const runId = '12345678-1234-4234-8234-999999999999';
    let epoch = 0;
    jest.mocked(createBleHubProbe).mockImplementation(() => fakeProbe(false, epoch++) as any);
    jest.spyOn(console, 'log').mockImplementation((line) => {
      const match = /rf-recovery-ten:cycle:(\d):recovery-awaiting:/.exec(String(line));
      if (match) signal(readyUrl(runId, Number(match[1])));
    });
    render(<App runtimeMode="rf-recovery-ten" rfServiceUuid={SERVICE} rfExpectedLocalName={NAME} rfRecoveryRunId={runId} />);
    await act(async () => { await jest.runAllTimersAsync(); });
    expect(screen.getByTestId('runtime-status').props.children).toBe('RF recovery 10 cycles passed');
    expect(screen.getByTestId('recreate-run-button').props.accessibilityState.disabled).toBe(true);
    expect(screen.getByTestId('close-probe-button').props.accessibilityState.disabled).toBe(true);
    expect(epoch).toBe(10);
  });
});

describe('snapshot and Linking trust boundaries', () => {
  test('Apple warm barrier roots remain observed, Android requires zero and closed requires all zero', () => {
    const apple = counts(false, 'warm');
    expect(() => validateRecoveryResources(apple, 'passive', false)).not.toThrow();
    expect(() => validateRecoveryResources({ ...counts(true, 'warm'), callbackRoutes: 1 }, 'passive', true)).toThrow('callbackRoutes');
    for (const key of RF_RESOURCE_FIELDS) expect(() => validateRecoveryResources({ ...counts(false, 'closed'), [key]: 1 }, 'closed', false)).toThrow(key);
    for (const value of [-1, -0, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) expect(() => validateRecoveryResources({ ...apple, gattAttributes: value }, 'passive', false)).toThrow('invalid');
    expect(() => validateRecoveryResources({ ...apple, extra: 0 } as any, 'passive', false)).toThrow('17-field');
  });
  test('nonce changes permanently fail closed', () => {
    const barrier = new RecoveryReadyBarrier(RUN, () => {}, 'boot');
    expect(() => barrier.assertHealthy('other-boot')).toThrow('nonce');
    expect(() => barrier.assertHealthy('boot')).toThrow('nonce');
    barrier.remove();
  });
  test('late duplicate readiness is rejected', async () => {
    const barrier = new RecoveryReadyBarrier(RUN, () => {}, 'boot');
    const waiting = barrier.wait(0, () => false, () => {});
    signal(readyUrl(RUN, 0)); await jest.runAllTimersAsync(); await waiting;
    signal(readyUrl(RUN, 0)); expect(() => barrier.assertHealthy()).toThrow('duplicate');
    barrier.remove(); expect(signals.size).toBe(0);
  });
});
