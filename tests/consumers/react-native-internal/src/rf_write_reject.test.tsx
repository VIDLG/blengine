/// <reference types="node" />
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import React from 'react';
import { Linking, Platform } from 'react-native';
import { act, fireEvent, render, screen } from '@testing-library/react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import App, { RF_LAB_MODES } from './App';
import { runRfWriteReject } from './rf_write_reject';
import { rfSignalUri, type RfControl, type RfSignalPhase } from './rf_signal';
import { RF_RESOURCE_FIELDS } from './rf_recovery_ten';
import { RF_BOOT_NONCE } from './rf_runtime';
import * as rfContract from './rf_contract';

jest.mock('@blehub/react-native-internal', () => ({ createBleHubProbe: jest.fn() }));
jest.mock('./multi_rf', () => ({ runRfMulti: jest.fn(), runRfMultiSoak: jest.fn() }));
jest.mock('./multi_fault', () => ({ runRfMultiFault: jest.fn() }));
type Probe = ReturnType<typeof createBleHubProbe>;
type Fault = 'wrong-code' | 'wrong-name' | 'wrong-platform' | 'timeout' | 'aborted' | 'written-on-failure' |
  'duplicate-terminal' | 'wrong-route' | 'wrong-session' | 'wrong-characteristic' | 'unknown' | 'bad-read' |
  'missing-domain' | 'bad-gatt' | 'close' | 'snapshot' | 'nonzero' | 'late' | 'close-duplicate' |
  'dispatch-rejected' | 'early-terminal' | 'oversized-error' | 'delayed-reject' | 'missing-held' | 'remove' | 'write-success' | 'queued-route';
const SERVICE = 'fd600000-0000-4000-8000-000000000000';
let sequence = 0;
let urlListener: (event: { url: string }) => void;

function fixture(android = false, fault?: Fault, disconnecting = 0) {
  const control: RfControl = { session: `12345678-1234-4234-8234-${String(++sequence).padStart(12, '0')}`,
    token: '11111111-1111-4111-8111-111111111111', flow: 10, mode: 'rf-write-reject' };
  const profile = { rfServiceUuid: SERVICE, rfExpectedLocalName: 'Multifunction', rfWriteRejectControl: JSON.stringify(control) };
  const lines: string[] = [];
  let listener: ((event: RfScanEvent) => void) | undefined;
  const emit = (event: RfScanEvent): void => listener?.(event);
  const terminal = (commandId: string, error?: RfScanEvent['error']): void => emit({ kind: 'commandCompleted', commandId, error });
  const route = { connectionId: '30004', gattSessionId: '30007', characteristicId: '30' };
  const readEvent = (id: string, value: number): void => {
    emit({ kind: 'characteristicRead', commandId: id, ...route,
          ...(fault === 'queued-route' && id === '30011' ? { gattSessionId: '999' } : {}), value: new Uint8Array([value]).buffer });
    terminal(id);
  };
  const rejectionError: NonNullable<RfScanEvent['error']> = {
    category: fault === 'timeout' ? 'timeout' : fault === 'aborted' ? 'operation_aborted' : 'gatt',
    message: fault === 'oversized-error' ? 'x'.repeat(2049) : 'native write rejected',
    platformDetails: { platform: android ? 'platform_android' : 'platform_apple',
      name: android ? 'BluetoothGatt' : 'CBATTErrorDomain', code: '14', message: 'full native detail' },
  };
  const rejected = (): void => {
    const error = rejectionError;
    if (fault === 'wrong-code') error.platformDetails!.code = '133';
    if (fault === 'wrong-name') error.platformDetails!.name = 'OtherDomain';
    if (fault === 'wrong-platform') error.platformDetails!.platform = android ? 'platform_apple' : 'platform_android';
    if (fault === 'written-on-failure') emit({ kind: 'characteristicWritten', commandId: '30010', ...route });
    terminal('30010', fault === 'write-success' ? undefined : error);
    if (fault === 'duplicate-terminal') terminal('30010', error);
    if (fault !== 'early-terminal') readEvent('30011', fault === 'bad-read' ? 0x11 : 0);
  };
  const characteristics = ['fff10000', 'fff20000', 'fff50000', 'fff30000', 'fff40000'].map((uuid, i) => ({
    id: String(30 + i), uuid: `${uuid}-0000-4000-8000-000000000000`, readable: i !== 4,
    writableWithResponse: i === 0 || i === 4, writableWithoutResponse: i === 0 || i === 4,
    notifiable: i === 1, indicatable: i === 1 || i === 2,
    descriptors: i === 1 || i === 2 ? [{ id: String(40 + i), uuid: '00002902-0000-1000-8000-00805f9b34fb' }] : [],
  }));
  if (fault === 'bad-gatt') characteristics[0].writableWithResponse = false;
  const gattServices = [{ id: '29', primary: true, uuid: SERVICE, characteristics }];
  const probe = {
    setRfScanListener: jest.fn((value: (event: RfScanEvent) => void) => { listener = value; }),
    removeRfScanListener: jest.fn(() => { listener = undefined; if (fault === 'remove') throw new Error('remove failed'); }),
    initialize: jest.fn((id: string) => {
      emit({ kind: 'adapterSnapshotChanged', commandId: id, adapterState: 'PoweredOn', scanAuthorization: 'Authorized' });
      terminal(id);
    }),
    startRfScan: jest.fn((id: string, scanSessionId: string) => {
      emit({ kind: 'scanStarted', commandId: id, scanSessionId }); terminal(id);
      emit({ kind: 'advertisementObserved', scanSessionId, peripheralId: '876', localName: 'Multifunction', advertisedServices: [SERVICE] });
    }),
    stopRfScan: jest.fn((id: string, scanSessionId: string) => { emit({ kind: 'scanStopped', commandId: id, scanSessionId }); terminal(id); }),
    connectRfPeripheral: jest.fn((id: string, connectionId: string) => {
      for (const connectionState of ['Connecting', 'Connected']) emit({ kind: 'connectionStateChanged', commandId: id, connectionId, connectionState });
      terminal(id);
    }),
    discoverRfGatt: jest.fn((id: string, connectionId: string, gattSessionId: string) => {
      emit({ kind: 'gattDiscovered', commandId: id, connectionId, gattSessionId,
        gattServices }); terminal(id);
    }),
    readRfCharacteristic: jest.fn((id: string) => {
      if (id !== '30011') readEvent(id, id === '30013' ? 0x22 : 0);
    }),
    writeRfCharacteristic: jest.fn((id: string) => {
      if (id === '30010') {
        if (fault === 'dispatch-rejected') return { category: 'invalid_state' as const };
        if (fault === 'early-terminal') rejected();
        return;
      }
      if (fault !== 'missing-domain') emit({ kind: 'characteristicWritten', commandId: id, ...route,
        ...(fault === 'wrong-route' ? { connectionId: '999' } : fault === 'wrong-session' ? { gattSessionId: '999' }
          : fault === 'wrong-characteristic' ? { characteristicId: '999' } : {}) });
      if (fault === 'unknown') terminal('999');
      terminal(id);
    }),
    disconnectRfPeripheral: jest.fn((id: string, connectionId: string) => {
      for (let i = 0; i < disconnecting; i += 1) emit({ kind: 'connectionStateChanged', commandId: id, connectionId, connectionState: 'Disconnecting' });
      emit({ kind: 'connectionStateChanged', commandId: id, connectionId, connectionState: 'Disconnected' }); terminal(id);
      if (fault === 'late') setTimeout(() => readEvent('30013', 0x22), 10);
    }),
    close: jest.fn(async () => {
      if (fault === 'close-duplicate') terminal('30013');
      if (fault === 'close') throw new Error('dispose rejected');
    }),
    snapshotRfResources: jest.fn(async () => {
      if (fault === 'snapshot') throw new Error('snapshot rejected');
      return Object.fromEntries(RF_RESOURCE_FIELDS.map((key) => [key, key === 'hubRoots' && fault === 'nonzero' ? 1 : 0]));
    }),
  };
  const signal = (phase: RfSignalPhase): void => urlListener({ url: rfSignalUri(control, phase) });
  const report = (line: string): void => {
    lines.push(line);
    if (!line.startsWith('rf-control:ready:')) return;
    const request = line.split(':phase=')[1];
    if (request === 'arm') queueMicrotask(() => signal('armed'));
    if (request === 'write-dispatched' && fault !== 'missing-held') queueMicrotask(() => signal('held'));
    if (request === 'reject') {
      // Native callbacks are deliberately delivered before the Linking ACK.
      rejected();
      if (fault === 'delayed-reject') setTimeout(() => signal('rejected'), 7000);
      else queueMicrotask(() => signal('rejected'));
    }
    if (request === 'recovery-done') queueMicrotask(() => signal('close-allowed'));
  };
  return { probe, native: probe as unknown as Probe, lines, profile, report, emit, terminal, signal, control, gattServices, rejectionError };
}

beforeEach(() => {
  jest.useFakeTimers();
  jest.replaceProperty(Platform, 'OS', 'android');
  jest.spyOn(Linking, 'addEventListener').mockImplementation((_type, callback) => { urlListener = callback; return { remove: jest.fn() } as unknown as ReturnType<typeof Linking.addEventListener>; });
});
afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); jest.clearAllMocks(); });
async function execute(f: ReturnType<typeof fixture>): Promise<unknown> {
  const result = runRfWriteReject(f.native, f.profile, f.report, () => false).then(() => undefined, (error: unknown) => error);
  await jest.runAllTimersAsync();
  return result;
}

test.each([[true, 0], [true, 1], [false, 0], [false, 1]] as const)('same Probe/route recovers; exact native ATT error survives (Android=%s, Disconnecting=%s)', async (android, disconnecting) => {
  jest.replaceProperty(Platform, 'OS', android ? 'android' : 'ios');
  const f = fixture(android, undefined, disconnecting);
  expect(await execute(f)).toBeUndefined();
  expect(f.probe.writeRfCharacteristic.mock.calls.map(([id]) => id)).toEqual(['30010', '30012']);
  expect(f.probe.readRfCharacteristic.mock.calls.map(([id]) => id)).toEqual(['30009', '30011', '30013']);
  for (const id of ['30009', '30011', '30013']) expect(f.probe.readRfCharacteristic).toHaveBeenCalledWith(id, '30004', '30007', '30');
  expect(f.probe.writeRfCharacteristic).toHaveBeenCalledWith('30012', '30004', '30007', '30', new Uint8Array([0x22]).buffer, 'with_response');
  expect(f.probe.setRfScanListener.mock.invocationCallOrder[0]).toBeLessThan(f.probe.initialize.mock.invocationCallOrder[0]);
  expect(f.probe.close).toHaveBeenCalledTimes(1);
  expect(f.probe.disconnectRfPeripheral).toHaveBeenCalledWith('30006', '30004');
  const events = f.lines.filter((line) => line.startsWith('rf-write-reject:event:')).map((line) => JSON.parse(line.slice('rf-write-reject:event:'.length)));
  expect(events.every((event) => event.runId === f.control.session && event.nonce === RF_BOOT_NONCE)).toBe(true);
  const error = events.find((event) => event.commandId === '30010');
  expect(error.error.platformDetails).toEqual({ platform: android ? 'platform_android' : 'platform_apple',
    name: android ? 'BluetoothGatt' : 'CBATTErrorDomain', code: '14', message: 'full native detail' });
  const ack = f.lines.findIndex((line) => line.startsWith('rf-control:observed:') && line.endsWith('phase=rejected'));
  expect(f.lines.findIndex((line) => line.includes('"commandId":"30010"'))).toBeLessThan(ack);
  const steps = f.lines.filter((line) => line.startsWith('rf-write-reject:step:')).map((line) => line.split(':')[2]);
  expect(steps).toEqual(['initialize', 'scan', 'connected', 'gatt', 'baseline-read', 'write-dispatched', 'queued-read-dispatched',
    'write-rejected', 'unchanged-read', 'recovery-write', 'recovery-read', 'disconnected', 'close']);
  expect(f.lines.at(-1)).toBe('rf-write-reject:passed');
  const runtimeClose = `rf-write-reject:runtime:session=${f.control.session}:nonce=${RF_BOOT_NONCE}:phase=close`;
  expect(f.lines.filter((line) => line === runtimeClose)).toHaveLength(1);
  const disconnected = f.lines.findIndex((line) => line.startsWith('rf-write-reject:step:disconnected:passed:'));
  const resources = f.lines.findIndex((line) => line.startsWith('rf-write-reject:resources:'));
  const closeStep = f.lines.indexOf(`rf-write-reject:step:close:passed:session=${f.control.session}`);
  expect(disconnected).toBeLessThan(resources);
  expect(resources).toBeLessThan(closeStep);
  expect(f.lines.slice(closeStep)).toEqual([
    `rf-write-reject:step:close:passed:session=${f.control.session}`, runtimeClose, 'rf-write-reject:passed',
  ]);
  expect(f.probe.disconnectRfPeripheral.mock.invocationCallOrder[0]).toBeLessThan(f.probe.close.mock.invocationCallOrder[0]);
  expect(f.probe.close.mock.invocationCallOrder[0]).toBeLessThan(f.probe.snapshotRfResources.mock.invocationCallOrder[0]);
  const closed = f.lines[resources];
  expect(Object.values(JSON.parse(closed.split(':snapshot=')[1]))).toEqual(Array(17).fill(0));
  expect(RF_LAB_MODES).toHaveLength(10);
  expect(RF_LAB_MODES).not.toContain('rf-write-reject');
});

test.each([
  [true, undefined, SERVICE, true], [false, undefined, SERVICE, true],
  [true, 'Multifunction', SERVICE, true], [false, 'Multifunction', SERVICE, true],
  [true, 'Other fixture', SERVICE, false], [false, 'Other fixture', SERVICE, false],
  [true, undefined, 'fd700000-0000-4000-8000-000000000000', false],
  [false, undefined, 'fd700000-0000-4000-8000-000000000000', false],
] as const)('scan identity permits absent name, never wrong present name or service (Android=%s, name=%s, service=%s)', async (android, localName, service, accepted) => {
  jest.replaceProperty(Platform, 'OS', android ? 'android' : 'ios');
  const f = fixture(android);
  f.probe.startRfScan.mockImplementation((id: string, scanSessionId: string) => {
    f.emit({ kind: 'scanStarted', commandId: id, scanSessionId }); f.terminal(id);
    f.emit({ kind: 'advertisementObserved', scanSessionId, peripheralId: '0', localName, advertisedServices: [service] });
  });
  const error = await execute(f);
  if (accepted) {
    expect(error).toBeUndefined();
    expect(f.probe.connectRfPeripheral).toHaveBeenCalledWith('30005', '30004', '0');
    expect(f.lines.at(-1)).toBe('rf-write-reject:passed');
  } else {
    expect(error).toBeInstanceOf(Error);
    expect(f.probe.connectRfPeripheral).not.toHaveBeenCalled();
    expect(f.probe.writeRfCharacteristic).not.toHaveBeenCalled();
    expect(f.lines).not.toContain('rf-write-reject:passed');
  }
  expect(f.probe.close).toHaveBeenCalledTimes(1);
});

test.each<Fault>(['wrong-code', 'wrong-name', 'wrong-platform', 'timeout', 'aborted', 'written-on-failure', 'duplicate-terminal',
  'wrong-route', 'wrong-session', 'wrong-characteristic', 'unknown', 'bad-read', 'missing-domain', 'bad-gatt', 'close', 'snapshot',
  'nonzero', 'late', 'close-duplicate', 'dispatch-rejected', 'early-terminal', 'oversized-error', 'delayed-reject', 'missing-held', 'remove', 'write-success', 'queued-route'])('%s fails closed and cannot emit Passed', async (fault) => {
  const f = fixture(true, fault);
  expect(await execute(f)).toBeInstanceOf(Error);
  expect(f.lines.some((line) => line.startsWith('rf-write-reject:failed:'))).toBe(true);
  expect(f.lines).not.toContain('rf-write-reject:passed');
  expect(f.lines.some((line) => line.startsWith('rf-write-reject:runtime:') && line.endsWith(':phase=close'))).toBe(false);
  expect(f.lines.some((line) => line.startsWith('rf-write-reject:step:close:passed:'))).toBe(false);
  expect(f.probe.close).toHaveBeenCalledTimes(1);
  expect(f.probe.removeRfScanListener).toHaveBeenCalledTimes(1);
});

test.each([true, false])('duplicate optional Disconnecting fails closed (Android=%s)', async (android) => {
  jest.replaceProperty(Platform, 'OS', android ? 'android' : 'ios');
  const f = fixture(android, undefined, 2);
  expect(detail(await execute(f))).toContain('duplicate domain: 30006:connectionStateChanged:Disconnecting');
  expect(f.lines.some((line) => line.startsWith('rf-write-reject:runtime:') && line.endsWith(':phase=close'))).toBe(false);
  expect(f.lines).not.toContain('rf-write-reject:passed');
  expect(f.probe.close).toHaveBeenCalledTimes(1);
});

test('runtime close waits for native close and resource-zero, then follows the close step', async () => {
  const f = fixture(true);
  let releaseClose!: () => void;
  let releaseSnapshot!: (snapshot: Record<string, number>) => void;
  f.probe.close.mockImplementationOnce(() => new Promise<void>((resolveClose) => { releaseClose = resolveClose; }));
  f.probe.snapshotRfResources.mockImplementationOnce(() => new Promise<Record<string, number>>((resolveSnapshot) => { releaseSnapshot = resolveSnapshot; }));
  const result = runRfWriteReject(f.native, f.profile, f.report, () => false);
  const runtimeClose = `rf-write-reject:runtime:session=${f.control.session}:nonce=${RF_BOOT_NONCE}:phase=close`;
  const closeStep = `rf-write-reject:step:close:passed:session=${f.control.session}`;
  const assertStillOpen = (): void => {
    expect(f.lines).not.toContain(runtimeClose);
    expect(f.lines).not.toContain(closeStep);
    expect(f.lines).not.toContain('rf-write-reject:passed');
  };
  await jest.runAllTimersAsync();
  expect(f.lines.some((line) => line.startsWith('rf-write-reject:step:disconnected:passed:'))).toBe(true);
  expect(f.probe.close).toHaveBeenCalledTimes(1);
  expect(f.probe.snapshotRfResources).not.toHaveBeenCalled();
  assertStillOpen();
  releaseClose();
  await jest.runAllTimersAsync();
  expect(f.probe.snapshotRfResources).toHaveBeenCalledTimes(1);
  expect(f.lines.some((line) => line.startsWith('rf-write-reject:resources:'))).toBe(false);
  assertStillOpen();
  releaseSnapshot(Object.fromEntries(RF_RESOURCE_FIELDS.map((key) => [key, 0])));
  await jest.runAllTimersAsync();
  await result;
  expect(f.lines.slice(-3)).toEqual([closeStep, runtimeClose, 'rf-write-reject:passed']);
});

test('iOS routing source publishes validated native ACK before Linking, counting only successful delivery', () => {
  // Source contract only: this does not execute UIKit/RN or prove live capture order.
  const apple = readFileSync(resolve(__dirname, '../ios/BleHubNitroHost/AppDelegate.swift'), 'utf8');
  const begin = apple.indexOf('fileprivate func deliverRfControl(');
  const end = apple.indexOf('private func installRfControl(', begin);
  expect(begin).toBeGreaterThanOrEqual(0);
  expect(end).toBeGreaterThan(begin);
  const handler = apple.slice(begin, end);
  const accepted = handler.indexOf('writeRejectControl!.accept(uri.absoluteString)');
  const target = handler.indexOf('guard let (uri, identity, phase) = target');
  const rejected = handler.indexOf('failRfControl("foreign-early-or-duplicate-signal")');
  const queued = handler.indexOf('DispatchQueue.main.async');
  const healthy = handler.indexOf('guard let self, !self.rfControlLock.withLock({ self.rfControlFailed })');
  const ack = handler.indexOf('self.writeRfHostRecord("signal", phase: phase, identity: identity)');
  const linking = handler.indexOf('guard RCTLinkingManager.application(UIApplication.shared, open: uri, options: [:]) else {');
  const failed = handler.indexOf('self.failRfControl("linking-rejected")');
  const delivered = handler.indexOf('self.rfControlLock.withLock { self.rfControlDelivered += 1 }');
  const order = [accepted, target, rejected, queued, healthy, ack, linking, failed, delivered];
  expect(order.every((index) => index >= 0)).toBe(true);
  expect(order).toEqual([...order].sort((a, b) => a - b));
  expect(handler.slice(failed, delivered)).toMatch(/return\s*\}/);
  expect(handler.match(/writeRfHostRecord\("signal"/g)).toHaveLength(1);
});

test('extended Android 4/21/3 GATT logs only the complete target service while App validates the original database', async () => {
  const f = fixture(true);
  const target = f.gattServices[0];
  const uuid = (short: string): string => `0000${short}-0000-1000-8000-00805f9b34fb`;
  let id = 100;
  for (const [service, characteristics] of [
    ['1800', ['2a00', '2a01']],
    ['1801', ['2a05', '2b29']],
    ['180a', ['2a23', '2a24', '2a25', '2a26', '2a27', '2a28', '2a29', '2a2a', '2a50', '2bff', '2c00', '2c01']],
  ] as const) {
    f.gattServices.push({ id: String(id++), uuid: uuid(service), primary: true,
      characteristics: characteristics.map((characteristic) => ({ ...target.characteristics[0],
        id: String(id++), uuid: uuid(characteristic), writableWithResponse: false, writableWithoutResponse: false,
        readable: characteristic !== '2a05', indicatable: characteristic === '2a05',
        descriptors: characteristic === '2a05' ? [{ id: String(id++), uuid: uuid('2902') }] : [],
      })) });
  }
  const event = { kind: 'gattDiscovered', commandId: '30008', connectionId: '30004', gattSessionId: '30007', gattServices: f.gattServices };
  const original = JSON.stringify(event);
  expect(Buffer.byteLength(original, 'utf8')).toBeGreaterThan(4096);
  expect(f.gattServices.reduce((count, service) => count + service.characteristics.length, 0)).toBe(21);
  expect(f.gattServices.flatMap((service) => service.characteristics).reduce((count, characteristic) => count + characteristic.descriptors.length, 0)).toBe(3);
  const validate = jest.spyOn(rfContract, 'validateRfGattDatabase');
  expect(await execute(f)).toBeUndefined();
  expect(validate).toHaveBeenCalledTimes(1);
  expect(validate.mock.calls[0][0].gattServices).toBe(f.gattServices);
  expect(JSON.stringify(event)).toBe(original);
  const records = f.lines.filter((line) => line.startsWith('rf-write-reject:event:') && line.includes('"kind":"gattDiscovered"'));
  expect(records).toHaveLength(1);
  expect(JSON.parse(records[0].slice('rf-write-reject:event:'.length))).toEqual({
    ...event, gattServices: [target], runId: f.control.session, nonce: RF_BOOT_NONCE,
  });
  expect(Buffer.byteLength(`BLEHUB_NITRO_RUNTIME ${records[0]}`, 'utf8')).toBeLessThan(4000);
  expect(f.lines).toContain('rf-write-reject:passed');
});

test.each(['services', 'characteristics', 'descriptors'] as const)('full GATT %s bound is enforced before foreign services are filtered', async (limit) => {
  const f = fixture(true);
  const characteristic = f.gattServices[0].characteristics[0];
  const foreign = { id: '100', uuid: '0000180a-0000-1000-8000-00805f9b34fb', primary: true, characteristics: [] as typeof f.gattServices[0]['characteristics'] };
  if (limit === 'services') f.gattServices.push(...Array.from({ length: 128 }, () => ({ ...foreign })));
  else for (let i = 0; i < 2; i += 1) f.gattServices.push({ ...foreign,
    characteristics: limit === 'characteristics' ? Array.from({ length: 257 }, () => ({ ...characteristic }))
      : [{ ...characteristic, descriptors: Array.from({ length: 257 }, () => ({ id: '101', uuid: '00002902-0000-1000-8000-00805f9b34fb' })) }],
  });
  expect(detail(await execute(f))).toContain(`event GATT exceeds ${limit === 'services' ? 128 : 512} ${limit}`);
  expect(f.lines.some((line) => line.includes('"kind":"gattDiscovered"'))).toBe(false);
  expect(f.lines.some((line) => line.startsWith('rf-control:ready:') && line.endsWith(':phase=arm'))).toBe(false);
  expect(f.lines).not.toContain('rf-write-reject:passed');
  expect(f.probe.close).toHaveBeenCalledTimes(1);
});

test.each([3999, 4000, 4100])('complete event line enforces UTF-8 byte ceiling without truncating nonascii errors (%s bytes)', async (bytes) => {
  const f = fixture(true);
  const prefix = 'BLEHUB_NITRO_RUNTIME rf-write-reject:event:';
  const overhead = Buffer.byteLength(prefix + JSON.stringify({ kind: 'commandCompleted', commandId: '30010',
    error: { ...f.rejectionError, message: '' }, runId: f.control.session, nonce: RF_BOOT_NONCE }), 'utf8');
  const remaining = bytes - overhead;
  f.rejectionError.message = '界'.repeat(Math.floor(remaining / 3)) + 'x'.repeat(remaining % 3);
  expect(f.rejectionError.message.length).toBeLessThan(2048);
  const result = await execute(f);
  const events = f.lines.filter((line) => line.startsWith('rf-write-reject:event:'));
  for (const line of events) {
    expect(Buffer.byteLength(`BLEHUB_NITRO_RUNTIME ${line}`, 'utf8')).toBeLessThan(4000);
    expect(() => JSON.parse(line.slice('rf-write-reject:event:'.length))).not.toThrow();
  }
  const rejection = events.find((line) => line.includes('"commandId":"30010"'));
  if (bytes < 4000) {
    expect(result).toBeUndefined();
    expect(Buffer.byteLength(`BLEHUB_NITRO_RUNTIME ${rejection}`, 'utf8')).toBe(bytes);
    expect(JSON.parse(rejection!.slice('rf-write-reject:event:'.length)).error).toEqual(f.rejectionError);
    expect(f.lines).toContain('rf-write-reject:passed');
  } else {
    expect(detail(result)).toContain('4000-byte UTF-8 log ceiling');
    expect(rejection).toBeUndefined();
    expect(f.lines.some((line) => line.startsWith('rf-write-reject:failed:') && line.includes('no truncated event emitted'))).toBe(true);
    expect(f.lines).not.toContain('rf-write-reject:passed');
  }
});

test('event floods remain bounded even after an invalid native event', async () => {
  const f = fixture(true);
  const result = runRfWriteReject(f.native, f.profile, (line) => {
    f.lines.push(line);
    if (line.startsWith('rf-control:ready:') && line.endsWith(':phase=arm')) {
      for (let i = 0; i < 300; i += 1) f.terminal('unknown');
    }
  }, () => false).catch((error: unknown) => error);
  await jest.runAllTimersAsync();
  expect(await result).toBeInstanceOf(Error);
  expect(f.lines.filter((line) => line.startsWith('rf-write-reject:event:'))).toHaveLength(256);
  expect(f.lines).not.toContain('rf-write-reject:passed');
});

test('the combined prompt REJECT deadline is monotonic even when the wall clock stops', async () => {
  jest.spyOn(Date, 'now').mockReturnValue(0);
  const f = fixture(true, 'missing-held');
  expect(detail(await execute(f))).toContain('prompt REJECT budget');
  expect(f.probe.disconnectRfPeripheral).not.toHaveBeenCalled();
  expect(f.lines).not.toContain('rf-write-reject:passed');
});

test('no accepted write before armed; no reject before HELD; queued read precedes reject and close waits for host', async () => {
  const f = fixture(true);
  const result = runRfWriteReject(f.native, f.profile, (line) => f.lines.push(line), () => false);
  const finished = result.catch((error: unknown) => error);
  await jest.advanceTimersByTimeAsync(20);
  expect(f.probe.writeRfCharacteristic).not.toHaveBeenCalled();
  f.signal('armed');
  await jest.advanceTimersByTimeAsync(20);
  expect(f.probe.writeRfCharacteristic).toHaveBeenCalledWith('30010', '30004', '30007', '30', new Uint8Array([0x11]).buffer, 'with_response');
  expect(f.probe.readRfCharacteristic).not.toHaveBeenCalledWith('30011', expect.anything(), expect.anything(), expect.anything());
  expect(f.lines.some((line) => line.endsWith(':phase=reject'))).toBe(false);
  f.signal('held');
  await jest.advanceTimersByTimeAsync(20);
  expect(f.probe.readRfCharacteristic).toHaveBeenCalledWith('30011', '30004', '30007', '30');
  expect(f.lines.at(-1)).toMatch(/:phase=reject$/);
  f.terminal('30010', { category: 'gatt', message: 'ATT error', platformDetails: { platform: 'platform_android', name: 'BluetoothGatt', code: '14' } });
  f.emit({ kind: 'characteristicRead', commandId: '30011', connectionId: '30004', gattSessionId: '30007', characteristicId: '30', value: new Uint8Array([0]).buffer });
  f.terminal('30011'); f.signal('rejected');
  await jest.advanceTimersByTimeAsync(20);
  expect(f.lines.at(-1)).toMatch(/:phase=recovery-done$/);
  expect(f.probe.disconnectRfPeripheral).not.toHaveBeenCalled();
  f.signal('close-allowed');
  await jest.runAllTimersAsync();
  expect(await finished).toBeUndefined();
});

test('primary error and close error both survive; invalid metadata constructs no native Probe', async () => {
  const f = fixture(true, 'close');
  f.profile.rfServiceUuid = 'bad';
  expect(detail(await execute(f))).toMatch(/canonical service.*cleanup: dispose rejected/);
  render(<App runtimeMode="rf-write-reject" rfWriteRejectControl="{}" />);
  await act(async () => { await jest.runAllTimersAsync(); });
  expect(createBleHubProbe).not.toHaveBeenCalled();
  expect(screen.getByTestId('rf-write-reject-status')).toHaveTextContent('failed');
});
const detail = (error: unknown): string => error instanceof Error ? error.message : String(error);

test('catalog browsing is inert; host scenario remains mounted across Tests / Evidence navigation', async () => {
  const browsing = render(<App />);
  fireEvent.press(screen.getByTestId('rf-workbench-category-diagnostics'));
  fireEvent.press(screen.getByTestId('rf-workbench-scenario-rf-write-reject'));
  expect(screen.getByText(/host-android-rf-write-reject/)).toBeTruthy();
  expect(createBleHubProbe).not.toHaveBeenCalled();
  browsing.unmount();
  const f = fixture(true);
  jest.mocked(createBleHubProbe).mockReturnValue(f.native);
  const log = jest.spyOn(console, 'log').mockImplementation((message: unknown) => {
    const line = String(message).replace('BLEHUB_NITRO_RUNTIME ', '');
    if (line.startsWith('rf-control:ready:') && line.endsWith(':phase=arm')) f.lines.push(line);
    else f.report(line);
  });
  const view = render(<App runtimeMode="rf-write-reject" {...f.profile} />);
  await act(async () => { await jest.advanceTimersByTimeAsync(20); });
  const runtime = screen.getByTestId('rf-write-reject-root');
  expect(screen.getByTestId('rf-write-reject-status')).toHaveTextContent('running');
  expect(f.probe.close).not.toHaveBeenCalled();
  fireEvent.press(screen.getByTestId('rf-workbench-home'));
  fireEvent.press(screen.getByTestId('rf-workbench-evidence'));
  expect(screen.getByText(`Session: ${f.control.session}`)).toBeTruthy();
  expect(screen.getByText('Mode: rf-write-reject')).toBeTruthy();
  fireEvent.press(screen.getByTestId('rf-workbench-current'));
  expect(screen.getByTestId('rf-write-reject-root')).toBe(runtime);
  expect(f.probe.close).not.toHaveBeenCalled();
  await act(async () => { f.signal('armed'); await jest.runAllTimersAsync(); });
  expect(screen.getByTestId('rf-write-reject-status')).toHaveTextContent('passed');
  expect(createBleHubProbe).toHaveBeenCalledTimes(1);
  expect(f.probe.close).toHaveBeenCalledTimes(1);
  view.unmount(); log.mockRestore();
});
