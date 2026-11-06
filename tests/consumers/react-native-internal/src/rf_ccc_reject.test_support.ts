import { Linking, Platform } from 'react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import { runRfCccEnableReject } from './rf_ccc_enable_reject';
import { runRfCccDisableReject } from './rf_ccc_disable_reject';
import type { CccRejectProfile } from './rf_ccc_reject';
import { rfSignalUri, type RfControl, type RfSignalPhase } from './rf_signal';
import { RF_RESOURCE_FIELDS } from './rf_recovery_ten';
const SERVICE = 'fd600000-0000-4000-8000-000000000000';
type Probe = ReturnType<typeof createBleHubProbe>;
export type Fault = 'code' | 'platform' | 'name' | 'timeout' | 'success' | 'duplicate-terminal' | 'failed-state' | 'failed-value' |
  'early-terminal' | 'early-read' | 'queued-before-terminal' | 'queued-route' | 'read-value' | 'enable-route' | 'enable-mode' |
  'enable-state' | 'duplicate-state' | 'missing-state' | 'value-before-ready' | 'value-route' | 'value-sub' | 'value-byte' |
  'duplicate-value' | 'disable-state' | 'silence-value' | 'late-value' | 'unknown' | 'warm-connections' | 'warm-subscriptions' |
  'warm-pendingIo' | 'warm-watchdogs' | 'close' | 'closed-nonzero' | 'close-event' | 'snapshot' | 'remove' |
  'missing-held' | 'late-reject' | 'dispatch' | 'topology' | 'descriptor' | 'overflow' | 'duplicate-disconnecting' | 'baseline-duplicate' | 'continued-before-ready' | 'continued-ef' | 'duplicate-read' | 'duplicate-read-terminal' | 'duplicate-control' | 'foreign-control' | 'early-control';
let sequence = 0;
let urlListener: (event: { url: string }) => void;
export function fixture(android = true, fault?: Fault, valueAfterAck = false, disable = false) {
  const PREFIX = disable ? 'rf-ccc-disable-reject' : 'rf-ccc-enable-reject';
  const ids = disable
    ? { connection: '34004', gatt: '34007', rejected: '34011', queued: '34012', failedSub: '34009', retrySub: '34009' }
    : { connection: '33004', gatt: '33007', rejected: '33010', queued: '33011', failedSub: '33009', retrySub: '33012' };
  let currentValue = 0xef;
  const control: RfControl = { session: `12345678-1234-4234-8234-${String(++sequence).padStart(12, '0')}`,
    token: '11111111-1111-4111-8111-111111111111', flow: disable ? 12 : 11, mode: PREFIX };
  const profile: CccRejectProfile = { rfServiceUuid: SERVICE, rfExpectedLocalName: 'Multifunction',
    ...(disable ? { rfCccDisableRejectControl: JSON.stringify(control) } : { rfCccEnableRejectControl: JSON.stringify(control) }) };
  const lines: string[] = [];
  let listener: ((event: RfScanEvent) => void) | undefined;
  let closed = false;
  const route = { connectionId: ids.connection, gattSessionId: ids.gatt, characteristicId: '31' };
  const emit = (event: RfScanEvent): void => listener?.(event);
  const terminal = (commandId: string, error?: RfScanEvent['error']): void => emit({ kind: 'commandCompleted', commandId, error });
  const read = (commandId: string): void => {
    emit({ kind: 'characteristicRead', commandId, ...route, characteristicId: '30',
      ...(fault === 'queued-route' && commandId === ids.queued ? { gattSessionId: '999' } : {}),
      value: new Uint8Array([fault === 'read-value' ? 1 : 0]).buffer });
    terminal(commandId);
    if (fault === 'duplicate-read') emit({ kind: 'characteristicRead', commandId, ...route, characteristicId: '30', value: new Uint8Array([0]).buffer });
    if (fault === 'duplicate-read-terminal') terminal(commandId);
  };
  const value = (subscriptionId = ids.retrySub): void => emit({ kind: 'notificationReceived', ...route, subscriptionId,
    ...(fault === 'value-route' ? { characteristicId: '30' } : {}),
    ...(fault === 'value-sub' ? { subscriptionId: disable ? '999' : ids.failedSub } : {}), value: new Uint8Array([fault === 'value-byte' ? 0xf0 : currentValue]).buffer });
  const reject = (): void => {
    const error: NonNullable<RfScanEvent['error']> = { category: fault === 'timeout' ? 'timeout' : 'gatt', message: fault === 'overflow' ? '界'.repeat(1500) : 'CCC rejected',
      platformDetails: { platform: (android !== (fault === 'platform')) ? 'platform_android' : 'platform_apple',
        name: fault === 'name' ? 'wrong' : android ? 'BluetoothGatt' : 'CBATTErrorDomain', code: fault === 'code' ? '133' : '14', message: 'full native error' } };
    if (fault === 'queued-before-terminal') read(ids.queued);
    if (fault === 'failed-state') emit({ kind: 'notificationStateChanged', commandId: ids.rejected, ...route, subscriptionId: ids.failedSub, notificationState: disable ? 'Disabled' : 'Enabled', actualNotificationMode: disable ? undefined : 'Notification' });
    if (fault === 'failed-value') value(ids.failedSub);
    terminal(ids.rejected, fault === 'success' ? undefined : error);
    if (fault === 'duplicate-terminal') terminal(ids.rejected, error);
    if (fault !== 'early-read') read(ids.queued);
  };
  const characteristics = ['fff10000', 'fff20000', 'fff50000', 'fff30000', 'fff40000'].map((uuid, i) => ({
    id: String(30 + i), uuid: `${uuid}-0000-4000-8000-000000000000`, readable: i !== 4,
    writableWithResponse: i === 0 || i === 4, writableWithoutResponse: i === 0 || i === 4,
    notifiable: i === 1, indicatable: i === 1 || i === 2,
    descriptors: i === 1 || i === 2 ? [{ id: String(40 + i), uuid: '00002902-0000-1000-8000-00805f9b34fb' }] : [],
  }));
  if (fault === 'topology') characteristics[1].indicatable = false;
  if (fault === 'descriptor') characteristics[1].descriptors = [];
  const gattServices = [{ id: '29', primary: true, uuid: SERVICE, characteristics }];
  const probe = {
    setRfScanListener: jest.fn((callback: (event: RfScanEvent) => void) => { listener = callback; }),
    removeRfScanListener: jest.fn(() => { listener = undefined; if (fault === 'remove') throw new Error('remove failed'); }),
    initialize: jest.fn((id: string) => { emit({ kind: 'adapterSnapshotChanged', commandId: id, adapterState: 'PoweredOn', scanAuthorization: 'Authorized' }); terminal(id); }),
    startRfScan: jest.fn((id: string, scanSessionId: string) => {
      emit({ kind: 'scanStarted', commandId: id, scanSessionId }); terminal(id);
      emit({ kind: 'advertisementObserved', scanSessionId, peripheralId: '0', advertisedServices: [SERVICE] });
    }),
    stopRfScan: jest.fn((id: string, scanSessionId: string) => { emit({ kind: 'scanStopped', commandId: id, scanSessionId }); terminal(id); }),
    connectRfPeripheral: jest.fn((id: string, connectionId: string) => {
      for (const connectionState of ['Connecting', 'Connected']) emit({ kind: 'connectionStateChanged', commandId: id, connectionId, connectionState });
      terminal(id);
    }),
    discoverRfGatt: jest.fn((id: string, connectionId: string, gattSessionId: string) => { emit({ kind: 'gattDiscovered', commandId: id, connectionId, gattSessionId, gattServices }); terminal(id); }),
    readRfCharacteristic: jest.fn((id: string) => { if (id !== ids.queued || fault === 'early-read') read(id); }),
    setRfNotifications: jest.fn((id: string, _connection: string, _gatt: string, subscriptionId: string, _characteristic: string, enabled: boolean) => {
      if (id === ids.rejected) {
        if (fault === 'dispatch') return { category: 'invalid_state' as const };
        if (fault === 'early-terminal') reject();
        return;
      }
      const event = { kind: 'notificationStateChanged', commandId: id, ...route, subscriptionId,
        notificationState: enabled ? 'Enabled' : 'Disabled', actualNotificationMode: enabled ? 'Notification' : undefined };
      if (fault === 'enable-route' && enabled) event.gattSessionId = '999';
      if (fault === 'enable-mode' && enabled) event.actualNotificationMode = 'Indication';
      if (fault === 'enable-state' && enabled || fault === 'disable-state' && !enabled) event.notificationState = 'Other';
      if (fault !== 'missing-state' || !enabled) emit(event);
      if (fault === 'duplicate-state' && enabled) emit(event);
      if (fault === 'unknown') terminal('999');
      terminal(id);
      if (fault === 'value-before-ready' && enabled) value();
    }),
    disconnectRfPeripheral: jest.fn((id: string, connectionId: string) => {
      for (let i = 0; i < (fault === 'duplicate-disconnecting' ? 2 : 1); i++) emit({ kind: 'connectionStateChanged', commandId: id, connectionId, connectionState: 'Disconnecting' });
      emit({ kind: 'connectionStateChanged', commandId: id, connectionId, connectionState: 'Disconnected' }); terminal(id);
      if (fault === 'late-value') setTimeout(() => value(), 10);
    }),
    close: jest.fn(async () => { closed = true; if (fault === 'close-event') value(); if (fault === 'close') throw new Error('close rejected'); }),
    snapshotRfResources: jest.fn(async () => {
      if (fault === 'snapshot') throw new Error('snapshot failed');
      const snapshot = Object.fromEntries(RF_RESOURCE_FIELDS.map((key) => [key, closed ? 0 : key === 'connections' || disable && key === 'subscriptions' ? 1 : key === 'callbackRoutes' ? 5 : key === 'gattAttributes' ? 8 : 0]));
      if (!closed && fault?.startsWith('warm-')) snapshot[fault.slice(5)] = fault === 'warm-connections' || disable && fault === 'warm-subscriptions' ? 0 : 1;
      if (closed && fault === 'closed-nonzero') snapshot.callbackRoutes = 1;
      return snapshot;
    }),
  };
  const signal = (phase: RfSignalPhase): void => urlListener({ url: rfSignalUri(control, phase) });
  const report = (line: string): void => {
    lines.push(line);
    if (!line.startsWith('rf-control:ready:')) return;
    const request = line.split(':phase=')[1];
    if (fault === 'foreign-control') { queueMicrotask(() => urlListener({ url: rfSignalUri({ ...control, flow: 11 }, 'baseline-value-sent') })); return; }
    if (fault === 'early-control') { queueMicrotask(() => signal('retry-allowed')); return; }
    if (fault === 'duplicate-control') queueMicrotask(() => { signal('baseline-value-sent'); signal('baseline-value-sent'); });
    if (request === 'arm') queueMicrotask(() => { if (fault === 'baseline-duplicate') value(); signal('armed'); });
    if ((request === 'enable-dispatched' || request === 'disable-dispatched') && fault !== 'missing-held') queueMicrotask(() => signal('held'));
    if (request === 'reject') {
      reject();
      if (fault === 'continued-before-ready') { currentValue = 0xe1; value(); }
      if (fault === 'late-reject') setTimeout(() => signal('rejected'), 7000);
      else queueMicrotask(() => signal('rejected'));
    }
    if (request === 'recovery-enabled' || request === 'baseline-enabled' || request === 'reject-observed') {
      currentValue = request === 'reject-observed' && fault !== 'continued-ef' ? 0xe1 : 0xef;
      // The host mutates the native value before its ACK, or just after it.
      if (!valueAfterAck) value();
      queueMicrotask(() => signal(request === 'baseline-enabled' ? 'baseline-value-sent' : request === 'reject-observed' ? 'continued-value-sent' : 'value-sent'));
      if (valueAfterAck) setTimeout(() => value(), 50);
    }
    if ((request === 'recovery-notified' || request === 'continued-notified')) queueMicrotask(() => { if (fault === 'duplicate-value') value(); signal(disable ? 'retry-allowed' : 'disable-allowed'); });
    if (request === 'disabled') { if (disable) currentValue = 0xf0; queueMicrotask(() => signal('silence-started')); if (fault === 'silence-value') setTimeout(() => value(), 500); }
    if (request === 'silence-done') queueMicrotask(() => signal('close-allowed'));
  };
  return { probe, native: probe as unknown as Probe, profile, report, lines, emit, terminal, signal, control, gattServices, value, route };
}
export function setupCccRejectTests(): void {
  beforeEach(() => {
    jest.useFakeTimers(); jest.replaceProperty(Platform, 'OS', 'android');
    jest.spyOn(Linking, 'addEventListener').mockImplementation((_type, callback) => { urlListener = callback; return { remove: jest.fn() } as unknown as ReturnType<typeof Linking.addEventListener>; });
  });
  afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); jest.clearAllMocks(); });
}
export async function execute(f: ReturnType<typeof fixture>): Promise<unknown> {
  const result = (f.control.mode === 'rf-ccc-disable-reject' ? runRfCccDisableReject : runRfCccEnableReject)(f.native, f.profile, f.report, () => false).then(() => undefined, (error: unknown) => error);
  await jest.runAllTimersAsync(); return result;
}
