import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import App, { runRfLabSuite, runRfLabFlow, runRfPendingReadReload } from './App';
import { Linking, Platform, Settings } from 'react-native';
import { RfSignalBarrier, rfSignalUri, type RfControl } from './rf_signal';
import { runRfLabFull } from './rf_full';
import { RF_RESOURCE_FIELDS, runRfRecoveryTen } from './rf_recovery_ten';
import { createRfEventStream } from './App';
import { RF_BOOT_NONCE } from './rf_runtime';

const CONTROL: RfControl = { session: '12345678-1234-4234-8234-123456789abc', flow: 8, mode: 'rf-cancellation', token: '11111111-1111-4111-8111-111111111111' };
const WATCHDOG_CONTROL: RfControl = { ...CONTROL, flow: 9, mode: 'rf-watchdog', token: '22222222-2222-4222-8222-222222222222' };
const CONTROL_JSON = JSON.stringify(CONTROL);
const BUNDLE_JSON = JSON.stringify({ controls: [CONTROL, WATCHDOG_CONTROL] });
let mockWatchdogRelease = (): void => {};
const signalListeners = new Set<(event: { url: string }) => void>();
let mockEnterHold = (): void => {};
let mockReleaseHold = (): void => {};
function signal(url: string): void { for (const listener of signalListeners) listener({ url }); }
function controlledReport(ledger: string[], cancellation = CONTROL, watchdog = WATCHDOG_CONTROL): (line: string) => void {
  return (line) => {
    ledger.push(line);
    if (!line.startsWith('rf-control:ready:')) return;
    const ready = line.split(':phase=')[1];
    setTimeout(() => {
      const control = line.includes(':flow=9:') ? watchdog : cancellation;
      if (control.flow === 8 && ready === 'read-dispatched') mockEnterHold();
      if (ready === 'release') { if (control.flow === 9) mockWatchdogRelease(); else mockReleaseHold(); }
      signal(rfSignalUri(control, ready === 'arm' ? 'armed' : ready === 'release' ? 'released' : ready === 'recover' ? 'recovery' : 'held'));
    }, 0);
  };
}
beforeEach(() => {
  signalListeners.clear();
  jest.spyOn(Linking, 'addEventListener').mockImplementation((_type, listener) => {
    signalListeners.add(listener);
    return { remove: () => { signalListeners.delete(listener); } } as any;
  });
});
afterEach(() => { jest.restoreAllMocks(); });

const SERVICE_UUID = 'fd100000-0000-4000-8000-000000000000';
const CHARACTERISTIC_UUIDS = [
  'fff10000-0000-4000-8000-000000000000',
  'fff20000-0000-4000-8000-000000000000',
  'fff50000-0000-4000-8000-000000000000',
  'fff30000-0000-4000-8000-000000000000',
  'fff40000-0000-4000-8000-000000000000',
];
const CCCD_UUID = '00002902-0000-1000-8000-00805f9b34fb';

let mockActiveMode: string = 'rf';

function complete(listener: ((event: object) => void) | undefined, commandId: string, error?: object): void {
  listener?.({ kind: 'commandCompleted', commandId, error });
}

function mockFakeProbe(passive: boolean, cancellation = false, localName: string | undefined = 'BleHubNrf52840', serviceUuid = SERVICE_UUID, strictRecovery = false, duplicateAdvertisement = false): Record<string, unknown> {
  let resourcesClosed = false;
  let subscriptionActive = false;
  let passiveTimer: ReturnType<typeof setTimeout> | undefined;
  let listener: ((event: any) => void) | undefined;
  let cancellationQueued = false;
  let cancellationActiveCompleted = false;
  let cancellationNativeEntered = false;
  if (cancellation) {
    mockEnterHold = () => { cancellationNativeEntered = true; };
    mockReleaseHold = () => {
      cancellationActiveCompleted = true;
      listener?.({ kind: 'characteristicRead', commandId: '9027', connectionId: '9007', gattSessionId: '9009', characteristicId: '20', value: new Uint8Array([0x00]).buffer });
      complete(listener, '9027');
    };
  }
  const emit = (event: object): void => listener?.(event);
  const characteristic = (index: number): object => ({
    id: String(20 + index),
    uuid: CHARACTERISTIC_UUIDS[index],
    readable: index !== 4,
    writableWithResponse: index === 0 || index === 4,
    writableWithoutResponse: index === 0 || index === 4,
    notifiable: index === 1,
    indicatable: index === 1 || index === 2,
    descriptors: index === 1 || index === 2 ? [{ id: String(30 + index), uuid: CCCD_UUID }] : [],
  });

  return {
    awaitPendingReloadReady: jest.fn(() => Promise.resolve()),
    setRfScanListener(callback: (event: any) => void): void { listener = callback; },
    removeRfScanListener(): void { listener = undefined; },
    initialize(commandId: string): null {
      emit({ kind: 'adapterSnapshotChanged', ...(strictRecovery ? { commandId } : {}), adapterState: 'PoweredOn', scanAuthorization: 'Authorized' });
      complete(listener, commandId);
      return null;
    },
    startRfScan(commandId: string, session: string): null {
      emit({ kind: 'scanStarted', commandId, scanSessionId: session });
      complete(listener, commandId);
      emit({ kind: 'advertisementObserved', scanSessionId: session, peripheralId: '42', localName, advertisedServices: [serviceUuid] });
      if (duplicateAdvertisement) {
        emit({ kind: 'advertisementObserved', scanSessionId: session, peripheralId: '42', localName, advertisedServices: [serviceUuid] });
      }
      return null;
    },
    stopRfScan(commandId: string, session: string): null {
      emit({ kind: 'scanStopped', commandId, scanSessionId: session });
      complete(listener, commandId);
      return null;
    },
    connectRfPeripheral(commandId: string, connectionId: string): null {
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Connecting' });
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Connected' });
      complete(listener, commandId);
      return null;
    },
    discoverRfGatt(commandId: string, connectionId: string, session: string): null {
      emit({
        kind: 'gattDiscovered',
        commandId,
        connectionId,
        gattSessionId: session,
        gattServices: [{
          id: '19',
          uuid: serviceUuid,
          primary: true,
          characteristics: CHARACTERISTIC_UUIDS.map((_, index) => characteristic(index)),
        }],
      });
      complete(listener, commandId);
      return null;
    },
    readRfRssi(commandId: string, connectionId: string): null {
      emit({ kind: 'rssiRead', commandId, connectionId, rssi: -42 });
      complete(listener, commandId);
      return null;
    },
    readRfCharacteristic(commandId: string, connectionId: string, session: string, characteristicId: string): null {
      if (commandId === '21010') {
        mockWatchdogRelease = () => emit({ kind: 'connectionStateChanged', connectionId, connectionState: 'Disconnected' });
        return null;
      }
      if (commandId === '21011') {
        setTimeout(() => {
          complete(listener, '21011', { category: 'operation_aborted', message: 'native retired FIFO' });
          complete(listener, '21010', { category: 'timeout', message: 'Apple GATT operation watchdog expired: read characteristic' });
        }, 10_000);
        return null;
      }
      if (cancellation && (commandId === '9027' || commandId === '9028')) return null;
      const bytes = commandId === '9012' ? [0x10] : commandId === '9014' ? [0xab] : [0x00];
      emit({
        kind: 'characteristicRead',
        commandId,
        connectionId,
        gattSessionId: session,
        characteristicId,
        value: new Uint8Array(bytes).buffer,
      });
      complete(listener, commandId);
      return null;
    },
    writeRfCharacteristic(commandId: string, connectionId: string, session: string, characteristicId: string, value: ArrayBuffer, writeType: string): null {
      // Canonical success projection; no-response models local submission, not remote ACK.
      emit({ kind: 'characteristicWritten', commandId, connectionId, gattSessionId: session, characteristicId, value });
      complete(listener, commandId);
      return null;
    },
    setRfNotifications(commandId: string, connectionId: string, session: string, subscriptionId: string, characteristicId: string, enabled: boolean, mode: string): null {
      if (commandId === '9018') {
        complete(listener, commandId, { category: 'not_supported', message: 'strict Indication unavailable' });
        return null;
      }
      emit({
        kind: 'notificationStateChanged',
        commandId,
        connectionId,
        gattSessionId: session,
        subscriptionId,
        characteristicId,
        notificationState: enabled ? 'Enabled' : 'Disabled',
        actualNotificationMode: mode === 'require_indication' ? 'Indication' : 'Notification',
      });
      complete(listener, commandId);
      if (enabled) {
        subscriptionActive = true;
        emit({ kind: 'notificationReceived', connectionId, gattSessionId: session, subscriptionId, characteristicId, value: new Uint8Array([0xef]).buffer });
        if (strictRecovery) {
          passiveTimer = setTimeout(() => {
            subscriptionActive = false;
            emit({ kind: 'connectionStateChanged', connectionId, connectionState: 'Disconnected' });
          }, 100);
        } else if (passive) {
          emit({ kind: 'connectionStateChanged', commandId: undefined, connectionId: '9007', connectionState: null, error: { category: 'connection', message: 'simulated passive disconnect' } });
        }
      }
      return null;
    },
    disconnectRfPeripheral(commandId: string, connectionId: string): null {
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Disconnected' });
      complete(listener, commandId);
      return null;
    },
    cancelOperation(targetCommandId: string): object | null {
      if (!cancellation) return null;
      if (targetCommandId === '9027') return cancellationActiveCompleted
        ? { category: 'operation_not_found', detail: 'operation is not active' }
        : cancellationNativeEntered ? { category: 'operation_not_cancellable', detail: 'active operation is not cancellable' } : null;
      if (targetCommandId === '9028') {
        if (cancellationQueued) return { category: 'operation_not_found', detail: 'operation is no longer active' };
        cancellationQueued = true;
        // Android may deliver the queued cancellation terminal before the
        // active native read callback returns through the Nitro handoff. The
        // RF event stream must retain that out-of-order terminal.
        complete(listener, '9028', { category: 'cancelled', message: 'queued operation cancelled' });
        return null;
      }
      return { category: 'operation_not_found', detail: 'operation is not active' };
    },
    snapshotRfResources: jest.fn(async () => {
      const counts = Object.fromEntries(RF_RESOURCE_FIELDS.map((key) => [key, 0]));
      if (!resourcesClosed) {
        Object.assign(counts, { hubRoots: 1, handoffRoots: 1, ingressRoots: 1, backendLeases: 1, adapterDelegates: 1, listeners: 1, peerCacheEntries: 1,
          workerThreads: Platform.OS === 'android' ? 2 : 0, lifecycleOwners: Platform.OS === 'android' ? 1 : 0 });
        if (subscriptionActive) Object.assign(counts, { connections: 1, subscriptions: 1, gattAttributes: 8, callbackRoutes: 4 });
      }
      return counts;
    }),
    close: jest.fn(async () => { resourcesClosed = true; if (passiveTimer) clearTimeout(passiveTimer); }),
  };
}

async function runWithFakeTime(run: () => Promise<unknown>): Promise<void> {
  jest.useFakeTimers();
  try {
    const done = run();
    // Execute the production silence/settle windows without wall-clock waits;
    // only the probe is synthetic, and the flow's event assertions stay intact.
    await jest.runAllTimersAsync();
    await done;
  } finally {
    jest.useRealTimers();
  }
}

jest.mock('react-native/Libraries/Settings/Settings.ios', () => ({ __esModule: true, default: { get: jest.fn() } }));
jest.mock('./multi_rf', () => ({ runRfMulti: jest.fn(), runRfMultiSoak: jest.fn() }));
jest.mock('./multi_fault', () => ({ runRfMultiFault: jest.fn() }));
jest.mock('@blehub/react-native-internal', () => ({
  createBleHubProbe: jest.fn(() => mockFakeProbe(mockActiveMode === 'rf-passive-disconnect', mockActiveMode === 'rf-cancellation')),
}));

describe('multifunction cancellation barrier', () => {
  test.each(['early', 'before-ready', 'wrong-token', 'duplicate'] as const)('fails closed on %s signals', async (fault) => {
    const barrier = new RfSignalBarrier(CONTROL, () => {});
    if (fault !== 'early') barrier.expect('armed');
    if (fault !== 'early' && fault !== 'before-ready') barrier.ready('arm');
    signal(rfSignalUri(fault === 'wrong-token' ? { ...CONTROL, token: CONTROL.session } : CONTROL, 'armed'));
    if (fault === 'duplicate') signal(rfSignalUri(CONTROL, 'armed'));
    await expect(barrier.wait(() => false, () => {})).rejects.toThrow(/early|duplicate|wrong/);
    expect(() => barrier.remove()).toThrow();
  });

  test.each(['missing-held', 'foreign-held'] as const)('dispatch stays legally queued: %s never calls cancel', async (fault) => {
    const ledger: string[] = [];
    const probe = mockFakeProbe(false, true) as any;
    const cancel = jest.spyOn(probe, 'cancelOperation');
    const read = jest.spyOn(probe, 'readRfCharacteristic');
    const remove = jest.spyOn(probe, 'removeRfScanListener');
    const report = (line: string): void => {
      ledger.push(line);
      if (line.endsWith(':phase=arm')) signal(rfSignalUri(CONTROL, 'armed'));
      if (fault === 'foreign-held' && line.endsWith(':phase=read-dispatched')) {
        signal(rfSignalUri({ ...CONTROL, token: CONTROL.session }, 'held'));
      }
    };
    await runWithFakeTime(() => runRfLabFlow('rf-cancellation', probe, SERVICE_UUID, report, () => false, undefined, CONTROL_JSON));
    expect(read.mock.calls.map((call) => call[0])).toEqual(['9027']);
    expect(cancel).not.toHaveBeenCalled();
    expect(remove).toHaveBeenCalledTimes(1);
    expect(signalListeners.size).toBe(0);
    expect(ledger.some((line) => line.startsWith('rf-cancellation:passed:'))).toBe(false);
    expect(ledger.some((line) => line.startsWith('rf-cancellation:failed:'))).toBe(true);
  });

  test('requires queued terminal before release and retains ledger through close', async () => {
    const ledger: string[] = [];
    const probe = mockFakeProbe(false, true) as any;
    const cancel = jest.spyOn(probe, 'cancelOperation');
    const report = controlledReport(ledger);
    await runWithFakeTime(() => runRfLabFlow('rf-cancellation', probe, SERVICE_UUID, report, () => false, undefined, CONTROL_JSON));
    expect(cancel.mock.calls.map((call) => call[0])).toEqual(['9027', '9028', '9030', '9027']);
    const at = (prefix: string): number => ledger.findIndex((line) => line.startsWith(prefix));
    expect(at('rf:step:cancellation-terminal:passed:')).toBeLessThan(at('rf-control:ready:session=' + CONTROL.session + ':flow=8:mode=rf-cancellation:token=' + CONTROL.token + ':phase=release'));
    expect(at('rf-cancellation:event:')).toBeGreaterThan(at('rf:step:connection-silence:passed:'));
    expect(ledger.find((line) => line.startsWith('rf-cancellation:event:'))).toContain('commandId=9028|9027|9027|9029|9029');
    expect(ledger.at(-1)).toMatch(/^rf-cancellation:passed:/);
  });

  test('missing metadata refuses cancellation before native initialization', async () => {
    const probe = mockFakeProbe(false, true) as any;
    const initialize = jest.spyOn(probe, 'initialize');
    const ledger: string[] = [];
    await runRfLabFlow('rf-cancellation', probe, SERVICE_UUID, (line) => ledger.push(line), () => false);
    expect(initialize).not.toHaveBeenCalled();
    expect(ledger.at(-1)).toMatch(/^rf-cancellation:failed:/);
  });

  test.each(['domain', 'terminal'] as const)('rejects late queued %s delivered during close', async (kind) => {
    const ledger: string[] = [];
    const probe = mockFakeProbe(false, true) as any;
    let nativeListener: (event: any) => void = () => {};
    const original = probe.setRfScanListener;
    probe.setRfScanListener = (listener: (event: any) => void): void => { nativeListener = listener; original(listener); };
    probe.close = async (): Promise<void> => {
      nativeListener(kind === 'terminal'
        ? { kind: 'commandCompleted', commandId: '9028', error: { category: 'cancelled' } }
        : { kind: 'characteristicRead', commandId: '9028', connectionId: '9007', gattSessionId: '9009', characteristicId: '20', value: new Uint8Array([0]).buffer });
    };
    await runWithFakeTime(() => runRfLabFlow('rf-cancellation', probe, SERVICE_UUID, controlledReport(ledger), () => false, undefined, CONTROL_JSON));
    expect(ledger.at(-1)).toMatch(/^rf-cancellation:failed:/);
    expect(ledger.some((line) => line.startsWith('rf-cancellation:passed:'))).toBe(false);
  });
});

// Synthetic JS protocol/lifecycle tests only: no native-entry, DK WAITING, or RF evidence.
describe('Android host-controlled pending-read reload', () => {
  let attempt = 0;
  const fixtureName = 'Frozen ATT fixture';
  const fixtureService = 'fd300000-0000-4000-8000-000000000000';
  const nextRunId = (): string => `12345678-1234-4234-8234-${String(++attempt).padStart(12, '0')}`;
  const notification = (runId: string): string => `dev.blehub.rn.reload.${runId}`;
  const arm = (runId: string): void => signal(`blehub-rn-spike://arm/${runId}`);
  function pendingProbe(): any {
    const probe = mockFakeProbe(false, false, fixtureName, fixtureService) as any;
    let listener: (event: any) => void = () => {};
    const set = probe.setRfScanListener;
    probe.setRfScanListener = jest.fn((callback) => { listener = callback; set(callback); });
    probe.inject = (event: object): void => listener(event);
    probe.readRfCharacteristic = jest.fn((commandId, connectionId, gattSessionId, characteristicId) => {
      if (commandId === '13008') {
        probe.inject({ kind: 'characteristicRead', commandId, connectionId, gattSessionId, characteristicId, value: new Uint8Array([0]).buffer });
        probe.inject({ kind: 'commandCompleted', commandId });
      }
      return null;
    });
    jest.spyOn(probe, 'initialize');
    jest.spyOn(probe, 'startRfScan');
    jest.spyOn(probe, 'close');
    return probe;
  }
  function launch(probe: any, runId: string, generation: number | undefined = 2): React.JSX.Element {
    (createBleHubProbe as jest.Mock).mockReturnValueOnce(probe);
    return <App runtimeMode="rf-pending-read-reload" rfServiceUuid={fixtureService} rfExpectedLocalName={fixtureName}
      rfReloadNotificationName={notification(runId)} rfPendingReadRunId={runId} rfPendingReadGeneration={generation} />;
  }
  beforeEach(() => { jest.replaceProperty(Platform, 'OS', 'android'); });

  test.each([undefined, 0, 3, 1.5, '2'])('rejects missing/non-native generation %s without Settings fallback', async (generation) => {
    const probe = pendingProbe();
    const settings = jest.spyOn(Settings, 'get').mockReturnValue(2);
    const log = jest.spyOn(console, 'log').mockImplementation(() => {});
    const runId = nextRunId();
    const element = launch(probe, runId);
    const view = render(React.cloneElement(element, { rfPendingReadGeneration: generation } as any));
    await act(async () => {});
    expect(settings).not.toHaveBeenCalled();
    expect(probe.awaitPendingReloadReady).not.toHaveBeenCalled();
    expect(probe.initialize).not.toHaveBeenCalled();
    expect(log.mock.calls.flat().join('\n')).toContain('failed:invalid generation');
    view.unmount();
  });

  test.each(['missing', 'uppercase', 'wrong-token'] as const)('rejects %s run metadata before native barrier', async (fault) => {
    const probe = pendingProbe();
    const ledger: string[] = [];
    const runId = nextRunId();
    await runRfPendingReadReload(probe, fixtureService,
      notification(fault === 'wrong-token' ? nextRunId() : runId), 2,
      (line) => ledger.push(line), () => false,
      fault === 'missing' ? undefined : fault === 'uppercase' ? 'ABCDEFAB-1234-4234-8234-123456789abc' : runId, fixtureName);
    expect(probe.awaitPendingReloadReady).not.toHaveBeenCalled();
    expect(probe.initialize).not.toHaveBeenCalled();
    expect(ledger.at(-1)).toContain(':failed:');
  });

  test('registers both listeners before native readiness; cannot initialize until the barrier resolves', async () => {
    const probe = pendingProbe();
    const ledger: string[] = [];
    const runId = nextRunId();
    let ready = (): void => {};
    probe.awaitPendingReloadReady.mockImplementation(() => new Promise<void>((resolve) => { ready = resolve; }));
    const done = runRfPendingReadReload(probe, fixtureService, notification(runId), 2, (line) => {
      ledger.push(line);
      if (line.includes(':awaiting-arm:')) arm(runId);
    }, () => false, runId, fixtureName);
    expect(signalListeners.size).toBe(1);
    expect(probe.setRfScanListener).toHaveBeenCalledTimes(1);
    expect(probe.awaitPendingReloadReady).toHaveBeenCalledWith(runId, 2);
    expect(probe.initialize).not.toHaveBeenCalled();
    await runWithFakeTime(async () => { ready(); await done; });
    expect(ledger[0]).toMatch(new RegExp(`^rf-pending-read-reload:generation=2:runtime-start:runId=${runId}:nonce=.+`));
    expect(ledger.indexOf('rf-pending-read-reload:generation=2:native-ready:passed')).toBeLessThan(ledger.findIndex((line) => line.includes(':initialize:passed:')));
    expect(probe.startRfScan).toHaveBeenCalledWith('13002', '13001', fixtureService);
    expect(ledger.at(-1)).toContain(':read:passed:commandId=13008:connectionId=13004:gattSessionId=13006:characteristicId=20:value=00');
    expect(ledger.some((line) => line.startsWith('rf-pending-read-reload:passed:'))).toBe(false);
    expect(signalListeners.size).toBe(0);
  });

  test.each(['arm', 'old-route'])('guards %s before even publishing runtime-start', async (fault) => {
    const probe = pendingProbe();
    const runId = nextRunId();
    const ledger: string[] = [];
    await runRfPendingReadReload(probe, fixtureService, notification(runId), 2, (line) => {
      ledger.push(line);
      if (line.includes(':runtime-start:')) {
        if (fault === 'arm') arm(runId);
        else probe.inject({ kind: 'commandCompleted', commandId: '12008' });
      }
    }, () => false, runId, fixtureName);
    expect(probe.initialize).not.toHaveBeenCalled();
    expect(probe.awaitPendingReloadReady).not.toHaveBeenCalled();
    expect(ledger.at(-1)).toMatch(/:failed:(arm URL before awaiting-arm|previous generation event leaked)/);
  });

  test.each(['foreign', 'absent'])('does not substitute the legacy iOS name for a %s Android advertisement', async (fault) => {
    const probe = pendingProbe();
    const runId = nextRunId();
    const ledger: string[] = [];
    probe.startRfScan.mockImplementation((commandId: string, scanSessionId: string) => {
      probe.inject({ kind: 'scanStarted', commandId, scanSessionId });
      probe.inject({ kind: 'commandCompleted', commandId });
      probe.inject({ kind: 'advertisementObserved', scanSessionId, peripheralId: '42', localName: fault === 'foreign' ? 'BleHubAttHoldDK' : undefined, advertisedServices: [fixtureService] });
      return null;
    });
    await runWithFakeTime(async () => {
      await runRfPendingReadReload(probe, fixtureService, notification(runId), 2, (line) => ledger.push(line), () => false, runId, fixtureName);
    });
    expect(probe.readRfCharacteristic).not.toHaveBeenCalled();
    expect(ledger.at(-1)).toContain(':failed:advertisement timed out');
  });

  test('generation 2 native barrier rejection cannot initialize or claim pass', async () => {
    const probe = pendingProbe();
    const runId = nextRunId();
    const ledger: string[] = [];
    probe.awaitPendingReloadReady.mockRejectedValue(new Error('old native close not ready'));
    expect(await runRfPendingReadReload(probe, fixtureService, notification(runId), 2,
      (line) => ledger.push(line), () => false, runId, fixtureName)).toBe(false);
    expect(probe.initialize).not.toHaveBeenCalled();
    expect(ledger.at(-1)).toContain(':failed:old native close not ready');
    expect(ledger.some((line) => line.includes(':native-ready:passed'))).toBe(false);
    expect(signalListeners.size).toBe(0);
  });

  test.each([1, 2])('generation %i requires host ARM after GATT ready', async (generation) => {
    const probe = pendingProbe();
    const runId = nextRunId();
    const ledger: string[] = [];
    let cancelled = false;
    await runWithFakeTime(async () => {
      await runRfPendingReadReload(probe, fixtureService, notification(runId), generation, (line) => {
        ledger.push(line);
        if (line.includes(':awaiting-arm:')) {
          expect(probe.readRfCharacteristic).not.toHaveBeenCalled();
          expect(ledger.some((entry) => entry.includes(':gatt:passed:'))).toBe(true);
          setTimeout(() => arm(runId), 200);
        }
        if (generation === 1 && line.includes(':read-dispatched:')) cancelled = true;
      }, () => cancelled, runId, fixtureName);
    });
    expect(probe.readRfCharacteristic.mock.calls).toEqual([[generation === 1 ? '12008' : '13008', generation === 1 ? '12004' : '13004', generation === 1 ? '12006' : '13006', '20']]);
    expect(ledger.some((line) => line.includes(':failed:'))).toBe(false);
    expect(probe.close).not.toHaveBeenCalled();
  });

  test.each([1, 2])('generation %i rejects early/wrong/duplicate ARM before read dispatch', async (generation) => {
    for (const fault of ['early', 'wrong', 'duplicate']) {
      const probe = pendingProbe();
      const runId = nextRunId();
      const ledger: string[] = [];
      if (fault === 'early') probe.awaitPendingReloadReady.mockImplementation(async () => { arm(runId); });
      await runWithFakeTime(async () => {
        await runRfPendingReadReload(probe, fixtureService, notification(runId), generation, (line) => {
          ledger.push(line);
          if (line.includes(':awaiting-arm:')) {
            arm(fault === 'wrong' ? nextRunId() : runId);
            if (fault === 'duplicate') arm(runId);
          }
        }, () => false, runId, fixtureName);
      });
      expect(probe.readRfCharacteristic).not.toHaveBeenCalled();
      expect(ledger.at(-1)).toMatch(/:failed:.*arm URL/);
      if (fault === 'early') expect(probe.initialize).not.toHaveBeenCalled();
    }
  });

  test.each(['commandId', 'scanSessionId', 'connectionId', 'gattSessionId', 'subscriptionId'])('rejects old %s routing even before generation-2 native-ready', async (field) => {
    const probe = pendingProbe();
    const ledger: string[] = [];
    const runId = nextRunId();
    probe.awaitPendingReloadReady.mockImplementation(async () => {
      probe.inject({ kind: 'characteristicRead', [field]: '12099' });
    });
    await runRfPendingReadReload(probe, fixtureService, notification(runId), 2,
      (line) => ledger.push(line), () => false, runId, fixtureName);
    expect(probe.initialize).not.toHaveBeenCalled();
    expect(ledger.at(-1)).toContain('previous generation event leaked');
    expect(ledger.some((line) => line.endsWith(':native-ready:passed'))).toBe(false);
  });

  test.each(['route', 'value', 'terminal', 'late-old', 'silence'])('generation 2 fails closed on read %s faults', async (fault) => {
    const probe = pendingProbe();
    const runId = nextRunId();
    const ledger: string[] = [];
    probe.readRfCharacteristic.mockImplementation((commandId: string, connectionId: string, gattSessionId: string, characteristicId: string) => {
      probe.inject({ kind: 'characteristicRead', commandId, connectionId: fault === 'route' ? '13099' : connectionId,
        gattSessionId, characteristicId, value: new Uint8Array([fault === 'value' ? 1 : 0]).buffer });
      probe.inject({ kind: 'commandCompleted', commandId: fault === 'terminal' ? '13099' : commandId });
      if (fault === 'late-old' || fault === 'silence') setTimeout(() => probe.inject({ kind: 'commandCompleted', commandId: fault === 'late-old' ? '12008' : '13008' }), 100);
      return null;
    });
    await runWithFakeTime(async () => {
      await runRfPendingReadReload(probe, fixtureService, notification(runId), 2,
        (line) => { ledger.push(line); if (line.includes(':awaiting-arm:')) arm(runId); }, () => false, runId, fixtureName);
    });
    expect(ledger.at(-1)).toContain(':failed:');
    expect(ledger.some((line) => line.includes(':read:passed:'))).toBe(false);
  });

  test('generation 1 stays pending, fails on an error before cancellation, and cleanup never closes', async () => {
    const probe = pendingProbe();
    const runId = nextRunId();
    const log = jest.spyOn(console, 'log').mockImplementation((line: string) => {
      if (line.includes(':awaiting-arm:')) arm(runId);
    });
    jest.useFakeTimers();
    const view = render(launch(probe, runId, 1));
    try {
      await act(async () => { await jest.advanceTimersByTimeAsync(3_000); });
      expect(probe.readRfCharacteristic).toHaveBeenCalledWith('12008', '12004', '12006', '20');
      expect(probe.close).not.toHaveBeenCalled();
      probe.inject({ kind: 'commandCompleted', commandId: '12008', error: { category: 'gatt', message: 'before cancel' } });
      await act(async () => { await jest.advanceTimersByTimeAsync(100); });
      expect(log.mock.calls.flat().join('\n')).toContain(':failed:commandCompleted.error:12008:gatt:before cancel');
      expect(probe.close).not.toHaveBeenCalled();
      await fireEvent.press(screen.getByTestId('recreate-run-button'));
      expect(probe.awaitPendingReloadReady).toHaveBeenCalledTimes(1);
      view.unmount();
      await act(async () => { await jest.advanceTimersByTimeAsync(100); });
      expect(probe.close).not.toHaveBeenCalled();
    } finally { view.unmount(); jest.useRealTimers(); }
  });

  test('generation 1 unmount while held leaves first native close to beforeDestroy', async () => {
    const probe = pendingProbe();
    const runId = nextRunId();
    jest.spyOn(console, 'log').mockImplementation((line: string) => { if (line.includes(':awaiting-arm:')) arm(runId); });
    jest.useFakeTimers();
    const view = render(launch(probe, runId, 1));
    try {
      await act(async () => { await jest.advanceTimersByTimeAsync(3_000); });
      view.unmount();
      await act(async () => { await jest.advanceTimersByTimeAsync(100); });
      expect(probe.close).not.toHaveBeenCalled();
      expect(signalListeners.size).toBe(0);
    } finally { view.unmount(); jest.useRealTimers(); }
  });

  test.each([false, true])('outer generation 2 close settles before final pass (reject=%s)', async (rejectClose) => {
    const probe = pendingProbe();
    const runId = nextRunId();
    const ledger: string[] = [];
    let finishClose = (): void => {};
    probe.close.mockImplementation(() => new Promise<void>((resolve, reject) => {
      finishClose = () => rejectClose ? reject(new Error('close failed')) : resolve();
    }));
    jest.spyOn(console, 'log').mockImplementation((line: string) => {
      ledger.push(line.replace('BLEHUB_NITRO_RUNTIME ', ''));
      if (line.includes(':awaiting-arm:')) arm(runId);
    });
    jest.useFakeTimers();
    const view = render(launch(probe, runId));
    try {
      await act(async () => { await jest.runAllTimersAsync(); });
      expect(ledger.some((line) => line.includes(':read:passed:'))).toBe(true);
      expect(probe.close).toHaveBeenCalledTimes(1);
      expect(ledger).not.toContain(`rf-pending-read-reload:passed:runId=${runId}`);
      await act(async () => { finishClose(); });
      if (rejectClose) {
        expect(ledger.at(-1)).toBe('rf-pending-read-reload:generation=2:failed:close failed');
        expect(ledger).not.toContain('rf-pending-read-reload:generation=2:close:passed');
        expect(ledger).not.toContain(`rf-pending-read-reload:passed:runId=${runId}`);
      } else {
        expect(ledger.slice(-2)).toEqual(['rf-pending-read-reload:generation=2:close:passed', `rf-pending-read-reload:passed:runId=${runId}`]);
        expect(screen.getByTestId('runtime-status')).toHaveTextContent('RF pending read reload passed');
      }
      await fireEvent.press(screen.getByTestId('recreate-run-button'));
      expect(probe.awaitPendingReloadReady).toHaveBeenCalledTimes(1);
      expect(screen.getByText(/Reload two RN engines in the same App/)).toBeTruthy();
    } finally { view.unmount(); jest.useRealTimers(); }
  });

  test('freezes launch profile/generation across surface rerenders and closes generation 2 on cleanup', async () => {
    const probe = pendingProbe();
    const runId = nextRunId();
    let ready = (): void => {};
    probe.awaitPendingReloadReady.mockImplementation(() => new Promise<void>((resolve) => { ready = resolve; }));
    jest.spyOn(console, 'log').mockImplementation((line: string) => { if (line.includes(':awaiting-arm:')) arm(runId); });
    jest.useFakeTimers();
    const view = render(launch(probe, runId));
    try {
      view.rerender(<App runtimeMode="rf-pending-read-reload" rfServiceUuid={SERVICE_UUID} rfExpectedLocalName="changed"
        rfReloadNotificationName={notification(nextRunId())} rfPendingReadRunId={nextRunId()} rfPendingReadGeneration={1} />);
      await act(async () => { ready(); await jest.runAllTimersAsync(); });
      expect(probe.awaitPendingReloadReady).toHaveBeenCalledTimes(1);
      expect(probe.awaitPendingReloadReady).toHaveBeenCalledWith(runId, 2);
      expect(probe.startRfScan).toHaveBeenCalledWith('13002', '13001', fixtureService);
      expect(screen.getByTestId('runtime-status')).toHaveTextContent('RF pending read reload passed');
      const closes = probe.close.mock.calls.length;
      view.unmount();
      await act(async () => {});
      expect(probe.close.mock.calls.length).toBe(closes + 1);
    } finally { view.unmount(); jest.useRealTimers(); }
  });

  test('errors observed before generation-1 cancellation cannot be hidden by cleanup', async () => {
    const probe = pendingProbe();
    const runId = nextRunId();
    const ledger: string[] = [];
    let cancelled = false;
    await runWithFakeTime(async () => {
      await runRfPendingReadReload(probe, fixtureService, notification(runId), 1, (line) => {
        ledger.push(line);
        if (line.includes(':awaiting-arm:')) arm(runId);
        if (line.includes(':read-dispatched:')) {
          probe.inject({ kind: 'characteristicRead', commandId: '12008', error: { category: 'gatt', message: 'before cleanup' } });
          cancelled = true;
        }
      }, () => cancelled, runId, fixtureName);
    });
    expect(ledger.at(-1)).toContain(':failed:characteristicRead.error:12008:gatt:before cleanup');
    expect(probe.close).not.toHaveBeenCalled();
  });

  test('a separate JS module boot emits a different runtime nonce', async () => {
    const nonces: string[] = [];
    const runners = [runRfPendingReadReload];
    jest.isolateModules(() => { runners.push(require('./App').runRfPendingReadReload); });
    for (const runner of runners) {
      const runId = nextRunId();
      const probe = pendingProbe();
      probe.awaitPendingReloadReady.mockRejectedValue(new Error('synthetic barrier stop'));
      await runner(probe, fixtureService, notification(runId), 2, (line) => {
        if (line.includes(':runtime-start:')) nonces.push(line.split(':nonce=')[1]);
      }, () => false, runId, fixtureName);
    }
    expect(nonces).toHaveLength(2);
    expect(nonces[0]).not.toBe(nonces[1]);
  });

  test('same generation cannot rerun in this runtime', async () => {
    const runId = nextRunId();
    const probe = pendingProbe();
    const report = (line: string): void => { if (line.includes(':awaiting-arm:')) arm(runId); };
    await runWithFakeTime(async () => { await runRfPendingReadReload(probe, fixtureService, notification(runId), 2, report, () => false, runId, fixtureName); });
    const ledger: string[] = [];
    const second = pendingProbe();
    await runRfPendingReadReload(second, fixtureService, notification(runId), 2, (line) => ledger.push(line), () => false, runId, fixtureName);
    expect(ledger.at(-1)).toContain('generation cannot rerun in the same JS runtime');
    expect(second.initialize).not.toHaveBeenCalled();
  });
});

describe('iOS native-owned pending-read reload (deterministic, no RF)', () => {
  let attempt = 0;
  const nextRunId = (): string => `abcdefab-1234-4234-8234-${String(++attempt).padStart(12, '0')}`;
  const globals = globalThis as any;
  let bind: jest.Mock;
  beforeEach(() => {
    jest.replaceProperty(Platform, 'OS', 'ios');
    bind = jest.fn();
    globals.__blehubPendingReloadBindProbe = bind;
  });
  afterEach(() => { delete globals.__blehubPendingReloadBindProbe; });

  function probeFor(generation: number, name: string | undefined = undefined): any {
    const probe = mockFakeProbe(false, false, name) as any;
    let listener: (event: any) => void = () => {};
    const set = probe.setRfScanListener;
    probe.setRfScanListener = jest.fn((callback) => { listener = callback; set(callback); });
    probe.inject = (event: object): void => listener(event);
    // Exercise zero-valued Apple peripheral identity and an omitted local name.
    probe.startRfScan = jest.fn((commandId, scanSessionId) => {
      listener({ kind: 'scanStarted', commandId, scanSessionId });
      listener({ kind: 'commandCompleted', commandId });
      listener({ kind: 'advertisementObserved', scanSessionId, peripheralId: '0', localName: name });
    });
    probe.readRfCharacteristic = jest.fn((commandId, connectionId, gattSessionId, characteristicId) => {
      if (generation === 2) {
        listener({ kind: 'characteristicRead', commandId, connectionId, gattSessionId, characteristicId, value: new Uint8Array([0]).buffer });
        listener({ kind: 'commandCompleted', commandId });
      }
      return null;
    });
    jest.spyOn(probe, 'initialize');
    jest.spyOn(probe, 'close');
    return probe;
  }

  function launch(probe: any, runId: string, generation: number | undefined): React.JSX.Element {
    (createBleHubProbe as jest.Mock).mockReturnValueOnce(probe);
    return <App runtimeMode="rf-pending-read-reload" rfServiceUuid={SERVICE_UUID} rfExpectedLocalName="Frozen fixture"
      rfReloadNotificationName={`dev.blehub.rn.reload.${runId}`} rfPendingReadRunId={runId} rfPendingReadGeneration={generation} />;
  }

  test.each([1, 2])('generation %i binds actual probe, awaits native readiness and requires one ARM', async (generation) => {
    const runId = nextRunId();
    const probe = probeFor(generation);
    const settings = jest.spyOn(Settings, 'get').mockReturnValue(99);
    const ledger: string[] = [];
    jest.spyOn(console, 'log').mockImplementation((line: string) => {
      ledger.push(line);
      if (line.includes(':awaiting-arm:')) signal(`blehub-rn-spike://arm/${runId}`);
    });
    jest.useFakeTimers();
    const view = render(launch(probe, runId, generation));
    try {
      await act(async () => {
        if (generation === 1) await jest.advanceTimersByTimeAsync(3_000);
        else await jest.runAllTimersAsync();
      });
      expect(bind).toHaveBeenCalledWith(probe);
      expect(settings).not.toHaveBeenCalled();
      expect(probe.awaitPendingReloadReady).toHaveBeenCalledWith(runId, generation);
      expect(ledger.some((line) => line.includes(':runtime-start:runId=') && line.includes(':nonce='))).toBe(true);
      expect(probe.readRfCharacteristic).toHaveBeenCalledWith(generation === 1 ? '12011' : '13011', generation === 1 ? '12004' : '13004', generation === 1 ? '12008' : '13008', '20');
      expect(ledger.some((line) => line.includes(':failed:'))).toBe(false);
      expect(ledger.some((line) => line.includes(':scan:passed:') && line.includes(':peripheralId=0:'))).toBe(true);
      expect(probe.close).toHaveBeenCalledTimes(generation === 1 ? 0 : 1);
      view.unmount();
      await act(async () => { await jest.advanceTimersByTimeAsync(100); });
      if (generation === 1) expect(probe.close).not.toHaveBeenCalled();
    } finally { view.unmount(); jest.useRealTimers(); }
  });

  test('cannot initialize before old native completion; unmount cannot supply that completion', async () => {
    const probe = probeFor(1);
    const runId = nextRunId();
    let release = (): void => {};
    probe.awaitPendingReloadReady.mockImplementation(() => new Promise<void>((resolve) => { release = resolve; }));
    jest.spyOn(console, 'log').mockImplementation(() => {});
    const view = render(launch(probe, runId, 1));
    expect(bind).toHaveBeenCalledTimes(1);
    expect(probe.initialize).not.toHaveBeenCalled();
    view.unmount();
    await act(async () => { release(); });
    expect(probe.initialize).not.toHaveBeenCalled();
    expect(probe.close).not.toHaveBeenCalled();
  });

  test('rejects an explicit wrong local name even though name omission is allowed', async () => {
    const probe = probeFor(2, 'Wrong fixture');
    const runId = nextRunId();
    const ledger: string[] = [];
    await runWithFakeTime(async () => {
      await runRfPendingReadReload(probe, SERVICE_UUID, `dev.blehub.rn.reload.${runId}`, 2,
        (line) => ledger.push(line), () => false, runId, 'Frozen fixture');
    });
    expect(ledger.at(-1)).toContain('failed:unexpected peripheral local name');
    expect(probe.readRfCharacteristic).not.toHaveBeenCalled();
  });

  test('missing native generation is not repaired by Settings', async () => {
    const probe = probeFor(1);
    const settings = jest.spyOn(Settings, 'get').mockReturnValue(1);
    jest.spyOn(console, 'log').mockImplementation(() => {});
    const view = render(launch(probe, nextRunId(), undefined));
    await act(async () => {});
    expect(probe.awaitPendingReloadReady).not.toHaveBeenCalled();
    expect(probe.initialize).not.toHaveBeenCalled();
    expect(settings).not.toHaveBeenCalled();
    view.unmount();
  });
});

describe('RF Lab page', () => {
  const modes = [
    'rf',
    'rf-connect',
    'rf-gatt',
    'rf-io',
    'rf-rssi',
    'rf-notification',
    'rf-indication',
    'rf-passive-disconnect',
    'rf-cancellation',
  ] as const;

  test.each([
    ['with_response', '9011', '9012', 'write-response', 'read-response'],
    ['without_response', '9013', '9014', 'write-without-response', 'read-without-response'],
  ] as const)('real rf-io requires routed Written plus terminal before readback (%s, software only)', async (writeType, commandId, readbackId, writeStep, readStep) => {
    for (const invalidWritten of [false, true]) {
      const probe = mockFakeProbe(false) as any;
      const events: RfScanEvent[] = [];
      const subscribe = probe.setRfScanListener;
      probe.setRfScanListener = (listener: (event: RfScanEvent) => void): void => subscribe((event: RfScanEvent) => {
        if (invalidWritten && event.kind === 'characteristicWritten' && event.commandId === commandId) {
          // No matching Written: each substitute has exactly one wrong route ID.
          for (const field of ['commandId', 'connectionId', 'gattSessionId', 'characteristicId']) {
            const foreign = { ...event, [field]: '99999' };
            events.push(foreign); listener(foreign);
          }
          return;
        }
        events.push(event); listener(event);
      });
      const write = jest.spyOn(probe, 'writeRfCharacteristic');
      const read = jest.spyOn(probe, 'readRfCharacteristic');
      const ledger: string[] = [];
      await runWithFakeTime(() => runRfLabFlow('rf-io', probe, SERVICE_UUID, (line) => ledger.push(line), () => false));
      expect(write).toHaveBeenCalledWith(commandId, '9007', '9009', '20', expect.any(ArrayBuffer), writeType);
      if (invalidWritten) {
        expect(events).toContainEqual({ kind: 'commandCompleted', commandId, error: undefined });
        expect(read.mock.calls.some((call) => call[0] === readbackId)).toBe(false);
        expect(ledger.some((line) => line.startsWith(`rf:step:${writeStep}:passed:`))).toBe(false);
        expect(ledger.at(-1)).toMatch(/^rf-io:failed:/);
      } else {
        expect(events.filter((event) => event.commandId === commandId)).toMatchObject([
          { kind: 'characteristicWritten', commandId, connectionId: '9007', gattSessionId: '9009', characteristicId: '20' },
          { kind: 'commandCompleted', commandId },
        ]);
        expect(read).toHaveBeenCalledWith(readbackId, '9007', '9009', '20');
        const written = ledger.findIndex((line) => line.startsWith(`rf:step:${writeStep}:passed:`));
        const readback = ledger.findIndex((line) => line.startsWith(`rf:step:${readStep}:passed:`));
        expect(written).toBeGreaterThanOrEqual(0);
        expect(readback).toBeGreaterThan(written);
        if (writeType === 'without_response') expect(ledger[written]).toContain(':terminal=local-submission');
        expect(ledger.at(-1)).toMatch(/^rf-io:passed:/);
      }
    }
  });

  test.each(['ios', 'android'] as const)('runs all ten flows through one runtime (%s)', async (platform) => {
    jest.replaceProperty(Platform, 'OS', platform);
    const ledger: string[] = [];
    const times: number[] = [];
    const publish = controlledReport(ledger);
    await runWithFakeTime(() => runRfLabSuite(
      SERVICE_UUID,
      (line) => { times.push(Date.now()); publish(line); },
      () => false,
      (mode) => mockFakeProbe(mode === 'rf-passive-disconnect', mode === 'rf-cancellation') as any,
      undefined, undefined, 'BleHubNrf52840', BUNDLE_JSON,
    ));
    expect(ledger.filter((line) => line.startsWith('rf-lab-suite:flow-passed:'))).toHaveLength(modes.length + 1);
    if (platform === 'android') {
      const started = ledger.indexOf('rf-lab-suite:flow-started:index=9:mode=rf-watchdog');
      const armed = ledger.findIndex((line) => line.startsWith('rf-control:ready:') && line.includes(':flow=9:') && line.endsWith(':phase=arm'));
      expect(times[armed] - times[started]).toBeGreaterThanOrEqual(35_000);
      expect(ledger.filter((line) => line.startsWith('rf-lab-suite:android-scan-throttle-wait:'))).toHaveLength(2);
    }
    expect(ledger.at(-1)).toBe('rf-lab-suite:passed:flows=10:passedCount=10:failureCount=0');
    const runtime = ledger.filter((line) => line.startsWith('rf-lab-suite:runtime:'));
    expect(runtime).toHaveLength(2);
    expect(runtime[0].replace(':phase=start', '')).toBe(runtime[1].replace(':phase=close', ''));
    expect(ledger).not.toContain('rf-lab-suite:passed:flows=9:passedCount=9:failureCount=0');
    expect(ledger.some((line) => line.includes(':failed') || line.includes('close:rejected:'))).toBe(false);
  }, 60_000);

  test.each(['ios', 'android'] as const)('composes both real executors in one JS boot with deterministic mock BLE (%s, not RF evidence)', async (platform) => {
    jest.replaceProperty(Platform, 'OS', platform);
    const runId = platform === 'ios' ? 'abcdefab-1234-4234-8234-123456789abc' : 'abcdefab-1234-4234-8234-123456789abd';
    const cancellation = { ...CONTROL, session: runId };
    const watchdog = { ...WATCHDOG_CONTROL, session: runId };
    const raw = JSON.stringify({ controls: [cancellation, watchdog] });
    const ledger: string[] = [];
    const publish = controlledReport(ledger, cancellation, watchdog);
    const probes: any[] = [];
    const factory = (mode?: string): any => {
      const probe = mockFakeProbe(mode === 'rf-passive-disconnect', mode === 'rf-cancellation', 'BleHubNrf52840', SERVICE_UUID, mode === undefined);
      probes.push(probe); return probe;
    };
    let passed = false;
    await runWithFakeTime(async () => {
      passed = await runRfLabFull(runId, raw,
        (guard, output) => runRfLabSuite(SERVICE_UUID, output, () => false, factory, undefined, undefined, 'BleHubNrf52840', raw, guard),
        (guard, output) => runRfRecoveryTen(() => factory(),
          (probe, reject, connection) => createRfEventStream(probe, true, true, new Set([connection]), 1024, reject, true),
          { rfServiceUuid: SERVICE_UUID, rfExpectedLocalName: 'BleHubNrf52840', rfRecoveryRunId: runId }, output, () => false, guard),
        (line) => {
          publish(line);
          if (line === `rf-lab-full:recovery-awaiting:runId=${runId}`) signal(`blehub-rn-spike://recover/${runId}/begin`);
          const cycle = /^rf-recovery-ten:cycle:(\d):recovery-awaiting:/.exec(line);
          if (cycle) signal(`blehub-rn-spike://recover/${runId}/${cycle[1]}`);
        }, () => false);
    });
    expect(passed).toBe(true);
    expect(probes).toHaveLength(20);
    expect(probes.every((probe) => probe.close.mock.calls.length > 0)).toBe(true);
    expect(ledger.filter((line) => line.startsWith('rf-lab-suite:flow-passed:'))).toHaveLength(10);
    expect(ledger.filter((line) => /^rf-recovery-ten:cycle:\d:passed$/.test(line))).toHaveLength(10);
    expect(ledger.filter((line) => /:runtime:.*nonce=/.test(line)).every((line) => line.includes(`:nonce=${RF_BOOT_NONCE}:`))).toBe(true);
    expect(ledger.at(-1)).toBe(`rf-lab-full:passed:flows=10:cycles=10:runId=${runId}`);
    expect(signalListeners.size).toBe(0);
  }, 60_000);

  test('outer native close rejection remains sticky even after the flow emitted passed', async () => {
    const probe = mockFakeProbe(false) as any;
    probe.close.mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error('second native close rejected'));
    const ledger: string[] = []; let passed = true;
    await runWithFakeTime(async () => { passed = await runRfLabSuite(SERVICE_UUID, (line) => ledger.push(line), () => false, () => probe, 'rf'); });
    expect(ledger.some((line) => line.startsWith('rf:passed:'))).toBe(true);
    expect(passed).toBe(false);
    expect(ledger).toContain('rf-lab-suite:failed:flows=1:passedCount=0:failureCount=1');
    expect(ledger.some((line) => line.includes('second native close rejected'))).toBe(true);
  });

  test('rejects watchdog signals before its listener, including between ordinary flows', async () => {
    const ledger: string[] = [];
    let failure: unknown;
    const probe = mockFakeProbe(false) as any;
    const cancel = jest.spyOn(probe, 'cancelOperation');
    await runWithFakeTime(() => runRfLabSuite(SERVICE_UUID, (line) => {
      ledger.push(line);
      if (line === 'rf-lab-suite:flow-started:index=0:mode=rf') signal(rfSignalUri(WATCHDOG_CONTROL, 'armed'));
    }, () => false, () => probe, undefined, undefined, 'BleHubNrf52840', BUNDLE_JSON).catch((error) => { failure = error; }));
    expect(String(failure)).toContain('early');
    expect(ledger.some((line) => line.startsWith('rf-lab-suite:passed:'))).toBe(false);
    expect(cancel).not.toHaveBeenCalled();
    expect(signalListeners.size).toBe(0);
  });

  test('runs ten flows using supplied fixture UUID and local name', async () => {
    const service = '12345678-1234-4000-8000-123456789abc';
    const name = 'Custom RF: fixture';
    const ledger: string[] = [];
    await runWithFakeTime(() => runRfLabSuite(
      service, controlledReport(ledger), () => false,
      (mode) => mockFakeProbe(mode === 'rf-passive-disconnect', mode === 'rf-cancellation', name, service) as any,
      undefined, undefined, name, BUNDLE_JSON,
    ));
    expect(ledger.at(-1)).toBe('rf-lab-suite:passed:flows=10:passedCount=10:failureCount=0');
    const runtime = ledger.filter((line) => line.startsWith('rf-lab-suite:runtime:'));
    expect(runtime).toHaveLength(2);
    expect(runtime[0].replace(':phase=start', '')).toBe(runtime[1].replace(':phase=close', ''));
    expect(ledger).not.toContain('rf-lab-suite:passed:flows=9:passedCount=9:failureCount=0');
    expect(ledger.filter((line) => line.startsWith(`rf:start:serviceUuid=${service}:expectedLocalName=${name}:`))).toHaveLength(9);
  });

  test('consumes duplicate scan advertisements before the connection event sequence', async () => {
    const ledger: string[] = [];
    const probe = mockFakeProbe(false, false, undefined, SERVICE_UUID, false, true) as any;
    await runWithFakeTime(() => runRfLabFlow(
      'rf-connect', probe, SERVICE_UUID, controlledReport(ledger), () => false,
    ));
    expect(ledger.at(-1)).toMatch(/^rf-connect:passed:/);
    expect(ledger.some((line) => line.includes(':step:connecting:passed:'))).toBe(true);
  });

  test('forwards the suite page local-name prop to its flow runner', async () => {
    const name = 'Custom RF fixture';
    (createBleHubProbe as jest.Mock).mockImplementationOnce(() => mockFakeProbe(false, false, name));
    jest.useFakeTimers();
    const createdBefore = (createBleHubProbe as jest.Mock).mock.calls.length;
    const view = render(<React.StrictMode><App runtimeMode="rf-lab-suite" rfLabSuiteFlow="rf" rfServiceUuid={SERVICE_UUID} rfExpectedLocalName={name} /></React.StrictMode>);
    try {
      await act(async () => { await jest.runAllTimersAsync(); });
      expect(screen.getByTestId('rf-lab-suite-runtime-status')).toHaveTextContent('RF Lab suite passed');
      expect(screen.getByTestId('rf-lab-suite-coverage')).toHaveTextContent('1/1');
      expect((createBleHubProbe as jest.Mock).mock.calls.length - createdBefore).toBe(1);
    } finally {
      view.unmount();
      jest.useRealTimers();
    }
  });

  test('rejects a foreign advertised local name even with matching service', async () => {
    const ledger: string[] = [];
    await runWithFakeTime(() => runRfLabSuite(
      SERVICE_UUID, (line) => ledger.push(line), () => false,
      () => mockFakeProbe(false, false, 'Foreign fixture') as any,
      'rf', undefined, 'Expected fixture',
    ));
    expect(ledger.at(-1)).toBe('rf-lab-suite:failed:flows=1:passedCount=0:failureCount=1');
    expect(ledger.some((line) => line.startsWith('rf:step:advertisement:passed:'))).toBe(false);
  });

  test('continues later flows after one flow fails', async () => {
    const ledger: string[] = [];
    await runWithFakeTime(() => runRfLabSuite(
      SERVICE_UUID,
      controlledReport(ledger),
      () => false,
      (mode) => {
        const probe = mockFakeProbe(mode === 'rf-passive-disconnect', mode === 'rf-cancellation');
        if (mode === 'rf-gatt') {
          probe.initialize = (): object => ({ category: 'backend', detail: 'synthetic flow failure' });
        }
        return probe as any;
      },
      undefined, undefined, 'BleHubNrf52840', BUNDLE_JSON,
    ));
    expect(ledger.some((line) => line.startsWith('rf-lab-suite:flow-failed:index=2:mode=rf-gatt:'))).toBe(true);
    expect(ledger).toContain('rf-lab-suite:flow-passed:index=3:mode=rf-io');
    expect(ledger.at(-1)).toBe('rf-lab-suite:failed:flows=10:passedCount=9:failureCount=1');
  }, 60_000);

  test('cancels a native command through the RF Lab page', async () => {
    mockActiveMode = 'rf';
    const view = render(<App runtimeMode="rf-lab" rfServiceUuid={SERVICE_UUID} />);
    await fireEvent.press(screen.getByTestId('rf-lab-run-button'));
    await waitFor(() => expect(screen.getByTestId('rf-lab-cancel-button').props.disabled).not.toBe(true), {
      timeout: 2_000,
      interval: 10,
    });
    await fireEvent.changeText(screen.getByTestId('rf-lab-cancel-command-id'), '9002');
    await fireEvent.press(screen.getByTestId('rf-lab-cancel-button'));
    expect(screen.getByTestId('rf-lab-cancel-status')).toHaveTextContent('rf:cancel:accepted:targetCommandId=9002');
    await view.unmount();
  }, 10_000);

  test('cancels a native command through the continuous RF Lab suite page', async () => {
    mockActiveMode = 'rf';
    const view = render(<App runtimeMode="rf-lab-suite" rfLabSuiteFlow="rf" rfServiceUuid={SERVICE_UUID} />);
    await waitFor(() => expect(screen.getByTestId('rf-lab-suite-cancel-button').props.disabled).not.toBe(true), {
      timeout: 2_000,
      interval: 10,
    });
    await fireEvent.changeText(screen.getByTestId('rf-lab-suite-cancel-command-id'), '9002');
    await fireEvent.press(screen.getByTestId('rf-lab-suite-cancel-button'));
    expect(screen.getByTestId('rf-lab-suite-cancel-status')).toHaveTextContent(
      'rf-lab-suite:cancel:accepted:targetCommandId=9002',
    );
    expect(screen.getAllByText('rf-lab-suite:cancel:accepted:targetCommandId=9002')).toHaveLength(2);
    await view.unmount();
  }, 10_000);

  test.each(modes.filter((mode) => mode !== 'rf-cancellation'))('selects and completes %s through RN button flow', async (mode) => {
    mockActiveMode = mode;
    const view = render(<App runtimeMode="rf-lab" rfServiceUuid={SERVICE_UUID} />);
    await fireEvent.press(screen.getByTestId(`rf-lab-mode-${mode}`));
    await fireEvent.press(screen.getByTestId('rf-lab-run-button'));
    await waitFor(() => expect(screen.getByTestId('rf-lab-runtime-status')).toHaveTextContent(`${mode} passed`), {
      timeout: 10_000,
      interval: 10,
    });
    expect(screen.getByTestId('rf-lab-service-uuid')).toHaveTextContent(`serviceUuid: ${SERVICE_UUID}`);
    expect(screen.getByTestId('rf-lab-coverage')).not.toHaveTextContent('0/');
    await view.unmount();
  }, 15_000);
});
