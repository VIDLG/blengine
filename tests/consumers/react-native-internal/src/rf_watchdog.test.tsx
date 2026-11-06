import React from 'react';
import { Linking } from 'react-native';
import { rfSignalUri, type RfControl, type RfSignalPhase } from './rf_signal';
import { act, fireEvent, render, screen } from '@testing-library/react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import App, { RF_LAB_MODES } from './App';
import { runRfWatchdog, WatchdogEventStream } from './rf_watchdog';
import { RF_WATCHDOG_IDS as ID, RF_WATCHDOG_SERVICE_UUID as SERVICE } from './rf_contract';

jest.mock('./multi_rf', () => ({ runRfMulti: jest.fn(), runRfMultiSoak: jest.fn() }));
jest.mock('./multi_fault', () => ({ runRfMultiFault: jest.fn() }));
jest.mock('@blehub/react-native-internal', () => ({ createBleHubProbe: jest.fn() }));

const MULTIFUNCTION_SERVICE = 'fd600000-0000-4000-8000-000000000000';
type Probe = ReturnType<typeof createBleHubProbe>;
type Fault = 'duplicate' | 'queued-domain' | 'late-domain' | 'wrong-id' | 'js-timeout' | 'bad-gatt' |
  'missing-terminal' | 'missing-loss' | 'missing-readvertisement' | 'close' | 'duplicate-at-close' | 'bad-value' |
  'wrong-advertisement-service' | 'wrong-recovery-service' | 'wrong-gatt-service' |
    'missing-local-name' | 'wrong-local-name' | 'wrong-recovery-local-name';
function fixture(android = false, fault?: Fault, extendedGatt = false, serviceUuid = SERVICE): {
  probe: Probe; emit: (event: RfScanEvent) => void; release: () => void;
  calls: string[]; closed: jest.Mock; removed: jest.Mock;
  gattServices: NonNullable<RfScanEvent['gattServices']>;
} {
  let listener: ((event: RfScanEvent) => void) | undefined;
  const calls: string[] = [];
  const emit = (event: RfScanEvent): void => listener?.(event);
  const timeout = android
    ? { category: 'timeout' as const, message: 'read characteristic exceeded the native callback watchdog', platformDetails: { platform: 'platform_android' as const, name: 'AndroidBleBackend' } }
    : { category: 'timeout' as const, message: 'Apple GATT operation watchdog expired: read characteristic' };
  const complete = (commandId: string, error?: RfScanEvent['error']): void => emit({ kind: 'commandCompleted', commandId, error });
  const loss = (): void => emit({ kind: 'connectionStateChanged', connectionId: ID.connection,
    ...(android ? { error: { category: 'connection' as const, message: 'Android GATT operation watchdog retired the connection', platformDetails: { platform: 'platform_android' as const, name: 'BluetoothGatt' } } } : { connectionState: 'Disconnected' }),
  });
  const characteristics = ['fff10000', 'fff20000', 'fff50000', 'fff30000', 'fff40000'].map((uuid, i) => ({
    id: String(30 + i), uuid: `${uuid}-0000-4000-8000-000000000000`,
    readable: i !== 4, writableWithResponse: i === 0 || i === 4, writableWithoutResponse: i === 0 || i === 4,
    notifiable: i === 1, indicatable: i === 1 || i === 2,
    descriptors: i === 1 || i === 2 ? [{ id: String(40 + i), uuid: '00002902-0000-1000-8000-00805f9b34fb' }] : [],
  }));
  if (fault === 'bad-gatt') characteristics[0].readable = false;
  const gattServices: NonNullable<RfScanEvent['gattServices']> = [{ id: '29', uuid: fault === 'wrong-gatt-service' ? 'fd000000-0000-4000-8000-000000000000' : serviceUuid, primary: true, characteristics }];
  if (extendedGatt) {
    const readable = (id: string, uuid: string): typeof characteristics[number] => ({
      id, uuid: `0000${uuid}-0000-1000-8000-00805f9b34fb`,
      readable: true, writableWithResponse: false, writableWithoutResponse: false,
      notifiable: false, indicatable: false, descriptors: [],
    });
    // Realistic Android discovery also includes GAP, GATT and Device Information.
    gattServices.push(
      { id: '60', uuid: '00001800-0000-1000-8000-00805f9b34fb', primary: true,
        characteristics: [readable('100', '2a00'), readable('101', '2a01'), readable('106', '2a04')] },
      { id: '61', uuid: '00001801-0000-1000-8000-00805f9b34fb', primary: true,
        characteristics: [{ ...readable('102', '2a05'), readable: false, indicatable: true,
          descriptors: [{ id: '200', uuid: '00002902-0000-1000-8000-00805f9b34fb' }] },
                    readable('103', '2b29'), readable('104', '2b2a'), readable('105', '2b3a')] },
      { id: '62', uuid: '0000180a-0000-1000-8000-00805f9b34fb', primary: true,
        characteristics: ['2a29', '2a24', '2a25', '2a27', '2a26', '2a28', '2a23', '2a50', '2a2a']
          .map((uuid, index) => readable(String(110 + index), uuid)) },
    );
  }
  const removed = jest.fn(() => { listener = undefined; });
  const closed = jest.fn(async () => {
    if (fault === 'duplicate-at-close') complete(ID.recoveryRead);
    if (fault === 'close') throw new Error('dispose rejected');
  });
  const probe = {
    setRfScanListener(callback: (event: RfScanEvent) => void): void { listener = callback; },
    removeRfScanListener: removed,
    initialize(commandId: string): undefined {
      emit({ kind: 'adapterSnapshotChanged', commandId, adapterState: 'PoweredOn', scanAuthorization: 'Authorized' });
      complete(commandId);
    },
    startRfScan(commandId: string, scanSessionId: string, service: string): undefined {
      expect(service).toBe(serviceUuid);
      emit({ kind: 'scanStarted', commandId, scanSessionId });
      complete(commandId);
      // Service matches the host-selected profile, not installed-image attestation.
      if (scanSessionId !== ID.recoveryScan || fault !== 'missing-readvertisement') {
        const wrongService = fault === 'wrong-advertisement-service' ||
          (scanSessionId === ID.recoveryScan && fault === 'wrong-recovery-service');
        emit({ kind: 'advertisementObserved', scanSessionId, peripheralId: '876',
          localName: fault === 'missing-local-name' ? undefined : fault === 'wrong-local-name' ||
                      (scanSessionId === ID.recoveryScan && fault === 'wrong-recovery-local-name') ? 'ForeignFixture' : 'Multifunction',
                    advertisedServices: [wrongService ? 'fd000000-0000-4000-8000-000000000000' : serviceUuid] });
      }
    },
    stopRfScan(commandId: string, scanSessionId: string): undefined {
      complete(commandId);
      emit({ kind: 'scanStopped', commandId, scanSessionId });
    },
    connectRfPeripheral(commandId: string, connectionId: string, peripheralId: string): undefined {
      expect(peripheralId).toBe('876');
      calls.push(commandId);
      complete(commandId);
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Connecting' });
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Connected' });
    },
    discoverRfGatt(commandId: string, connectionId: string, gattSessionId: string): undefined {
      emit({ kind: 'gattDiscovered', commandId, connectionId, gattSessionId, gattServices });
      complete(commandId);
    },
    readRfCharacteristic(commandId: string, connectionId: string, gattSessionId: string, characteristicId: string): undefined {
      calls.push(commandId);
      expect(characteristicId).toBe(commandId === ID.recoveryRead ? '31' : '30');
      const read = { kind: 'characteristicRead', commandId, connectionId, gattSessionId, characteristicId, value: new Uint8Array([fault === 'bad-value' ? 1 : 0]).buffer };
      if (commandId === ID.active) return;
      if (commandId === ID.queued) {
        setTimeout(() => {
          if (fault === 'queued-domain') emit(read);
          if (android && fault !== 'missing-loss') loss();
          complete(commandId, { category: 'operation_aborted', message: 'native recovery retired pending operation' });
          if (fault !== 'missing-terminal') complete(fault === 'wrong-id' ? '999' : ID.active,
            fault === 'js-timeout' ? { category: 'timeout', message: 'JS timeout after 10000ms' } : timeout);
          if (fault === 'duplicate') complete(commandId, { category: 'operation_aborted', message: 'duplicate' });
        }, 10_000);
        return;
      }
      complete(commandId);
      emit(read);
    },
    // Before handler entry a cancellation may validly be accepted. Never use it as readiness.
    cancelOperation: jest.fn(() => undefined),
    disconnectRfPeripheral(commandId: string, connectionId: string): undefined {
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Disconnecting' });
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Disconnected' });
      complete(commandId);
    },
    close: closed,
  } as unknown as Probe;
  return { probe, emit, calls, closed, removed, gattServices, release: () => {
    if (!android && fault !== 'missing-loss') loss();
    if (fault === 'late-domain') emit({ kind: 'characteristicRead', commandId: ID.active, connectionId: ID.connection, gattSessionId: ID.gatt, characteristicId: '30', value: new Uint8Array([0]).buffer });
  } };
}

const STANDALONE_CONTROL: RfControl = { session: "12345678-1234-4234-8234-123456789abc", flow: 9, mode: "rf-watchdog", token: "11111111-1111-4111-8111-111111111111" };
function standaloneRelay(): (line: string) => void {
  let receive: (event: { url: string }) => void;
  jest.spyOn(Linking, "addEventListener").mockImplementation((_type, listener) => { receive = listener; return { remove: jest.fn() } as any; });
  return (raw) => {
    const line = raw.replace(/^BLEHUB_NITRO_RUNTIME /, "");
    if (!line.startsWith("rf-control:ready:")) return;
    const request = line.split(":phase=")[1];
    const phase: RfSignalPhase = request === "arm" ? "armed" : request === "read-dispatched" ? "held" : request === "release" ? "released" : "recovery";
    setTimeout(() => receive({ url: rfSignalUri(STANDALONE_CONTROL, phase) }), 0);
  };
}
function standalone(probe: Probe, service: string | undefined, report: (line: string) => void, cancelled: () => boolean): Promise<void> {
  const relay = standaloneRelay();
  return runRfWatchdog(probe, service, (line) => { report(line); relay(line); }, cancelled, STANDALONE_CONTROL, "Multifunction");
}

async function execute(android = false, fault?: Fault, extendedGatt = false, serviceUuid = SERVICE): Promise<{ lines: string[]; error: unknown; fake: ReturnType<typeof fixture> }> {
  const fake = fixture(android, fault, extendedGatt, serviceUuid);
  const lines: string[] = [];
  let error: unknown;
  const done = standalone(fake.probe, serviceUuid, (line) => {
    lines.push(line);
    if (line === 'rf-watchdog:awaiting-release') {
      expect(fake.calls).not.toContain(ID.recoveryConnect);
      fake.release();
    }
  }, () => false).catch((reason: unknown) => { error = reason; });
  await jest.advanceTimersByTimeAsync(60_000);
  await done;
  expect(fake.probe.cancelOperation).not.toHaveBeenCalled();
  return { lines, error, fake };
}

describe('integrated multifunction watchdog barrier', () => {
  const control: RfControl = { session: '12345678-1234-4234-8234-123456789abc', flow: 9, mode: 'rf-watchdog', token: '11111111-1111-4111-8111-111111111111' };
  let receive: (event: { url: string }) => void;
  const remove = jest.fn();
  beforeEach(() => {
    jest.useFakeTimers(); remove.mockClear();
    jest.spyOn(Linking, 'addEventListener').mockImplementation((_type, listener) => { receive = listener; return { remove } as any; });
  });
  afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); });

  test.each([false, true].flatMap((android) => [undefined, 'missing-local-name' as const].map((fault) => ({ android, fault }))))('dispatches across host barriers with optional advertised name (android=$android, fault=$fault)', async ({ android, fault }) => {
    const fake = fixture(android, fault, false, MULTIFUNCTION_SERVICE);
    const lines: string[] = [];
    let error: unknown;
    const done = runRfWatchdog(fake.probe, MULTIFUNCTION_SERVICE, (line) => {
      lines.push(line);
      if (!line.startsWith('rf-control:ready:')) return;
      const request = line.split(':phase=')[1];
      if (request === 'arm') {
        expect(fake.calls).not.toContain(ID.active);
        expect(lines.some((entry) => entry.startsWith('rf-watchdog:discovery-ready:'))).toBe(true);
      }
      if (request === 'read-dispatched') { expect(fake.calls).toContain(ID.active); expect(fake.calls).not.toContain(ID.queued); }
      if (request === 'release') { expect(lines).toContain('rf-watchdog:queued-aborted:commandId=21011:error=operation_aborted'); fake.release(); }
      if (request === 'recover') { expect(lines).toContain('rf-watchdog:late-silence:observationMs=1000'); expect(fake.calls).not.toContain(ID.recoveryConnect); }
      const phase: RfSignalPhase = request === 'arm' ? 'armed' : request === 'read-dispatched' ? 'held' : request === 'release' ? 'released' : 'recovery';
      setTimeout(() => receive({ url: rfSignalUri(control, phase) }), 0);
    }, () => false, control, 'Multifunction').catch((reason) => { error = reason; });
    await jest.advanceTimersByTimeAsync(20_000); await done;
    expect(error).toBeUndefined();
    expect(lines.at(-1)).toBe('rf-watchdog:passed');
    expect(fake.calls.filter((id) => id === ID.active || id === ID.queued)).toEqual([ID.active, ID.queued]);
    expect(fake.probe.cancelOperation).not.toHaveBeenCalled();
    expect(remove).toHaveBeenCalledTimes(1);
  });

  test.each(['wrong-local-name', 'wrong-recovery-local-name'] as const)('rejects an explicitly foreign name (%s)', async (fault) => {
    const fake = fixture(false, fault, false, MULTIFUNCTION_SERVICE);
    const lines: string[] = [];
    let error: unknown;
    const done = runRfWatchdog(fake.probe, MULTIFUNCTION_SERVICE, (line) => {
      lines.push(line);
      if (!line.startsWith('rf-control:ready:')) return;
      const request = line.split(':phase=')[1];
      if (request === 'release') fake.release();
      const phase: RfSignalPhase = request === 'arm' ? 'armed' : request === 'read-dispatched' ? 'held' : request === 'release' ? 'released' : 'recovery';
      setTimeout(() => receive({ url: rfSignalUri(control, phase) }), 0);
    }, () => false, control, 'Multifunction').catch((reason: unknown) => { error = reason; });
    await jest.advanceTimersByTimeAsync(60_000); await done;
    expect(error).toBeDefined();
    expect(lines.at(-1)).toContain('JS outer wait expired');
    expect(lines).not.toContain('rf-watchdog:passed');
    expect(fake.calls).not.toContain(ID.recoveryConnect);
    if (fault === 'wrong-local-name') expect(fake.calls).not.toContain(ID.connect);
    expect(fake.closed).toHaveBeenCalledTimes(1);
    expect(fake.removed).toHaveBeenCalledTimes(1);
  });

  test.each(['no-arm', 'no-held', 'wrong-token', 'early', 'duplicate'] as const)('fails closed on %s without probing cancellation', async (fault) => {
    const fake = fixture(false, undefined, false, MULTIFUNCTION_SERVICE);
    const lines: string[] = [];
    const done = runRfWatchdog(fake.probe, MULTIFUNCTION_SERVICE, (line) => {
      lines.push(line);
      if (fault === 'early' && line.includes(':phase=registered')) receive({ url: rfSignalUri(control, 'armed') });
      if (!line.startsWith('rf-control:ready:')) return;
      const request = line.split(':phase=')[1];
      if ((fault === 'no-arm' && request === 'arm') || (fault === 'no-held' && request === 'read-dispatched')) return;
      setTimeout(() => {
        receive({ url: rfSignalUri(fault === 'wrong-token' ? { ...control, token: control.session } : control, request === 'arm' ? 'armed' : 'held') });
        if (fault === 'duplicate') receive({ url: rfSignalUri(control, 'armed') });
      }, 0);
    }, () => false, control, 'Multifunction').catch(() => {});
    await jest.advanceTimersByTimeAsync(20_000); await done;
    expect(lines).not.toContain('rf-watchdog:passed');
    expect(fake.calls).not.toContain(ID.queued);
    if (fault !== 'no-held') expect(fake.calls).not.toContain(ID.active);
    expect(fake.probe.cancelOperation).not.toHaveBeenCalled();
    expect(fake.closed).toHaveBeenCalledTimes(1);
  });
});

describe('standalone production-native RF watchdog ledger', () => {
  beforeEach(() => { jest.useFakeTimers(); jest.mocked(createBleHubProbe).mockClear(); });
  afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); });

  test.each([
    [false, SERVICE], [true, SERVICE], [false, MULTIFUNCTION_SERVICE], [true, MULTIFUNCTION_SERVICE],
  ] as const)('uses exact markers and retains out-of-order terminals (android=%s, service=%s)', async (android, service) => {
    const { lines, error, fake } = await execute(android, undefined, false, service);
    expect(error).toBeUndefined();
    expect(lines.filter((line) => !line.startsWith('rf-watchdog:event:') && !line.startsWith('rf-control:'))).toEqual([
      'rf-watchdog:discovery-ready:connectionId=21007:gattSessionId=21009:characteristicId=30',
      'rf-watchdog:active-dispatched:commandId=21010:connectionId=21007:gattSessionId=21009:characteristicId=30',
      'rf-watchdog:read-dispatched:activeCommandId=21010:queuedCommandId=21011:connectionId=21007:gattSessionId=21009:characteristicId=30',
      'rf-watchdog:active-timeout:commandId=21010:error=timeout',
      'rf-watchdog:queued-aborted:commandId=21011:error=operation_aborted',
      'rf-watchdog:awaiting-release',
      'rf-watchdog:connection-retired:connectionId=21007',
      'rf-watchdog:late-silence:observationMs=1000',
      'rf-watchdog:recovery-read:passed:commandId=21024:connectionId=21021:gattSessionId=21023:characteristicId=31:value=00',
      'rf-watchdog:close:passed',
      'rf-watchdog:passed',
    ]);
    expect(fake.closed).toHaveBeenCalledTimes(1);
    expect(fake.removed).toHaveBeenCalledTimes(1);
    const events = lines.filter((line) => line.startsWith('rf-watchdog:event:')).map((line) => JSON.parse(line.slice('rf-watchdog:event:'.length)));
    expect(events.filter((event) => event.kind === 'commandCompleted')).toHaveLength(13);
    expect(events.filter((event) => event.kind === 'advertisementObserved').map((event) => event.advertisedServices)).toEqual([[service], [service]]);
    expect(events.find((event) => event.commandId === ID.active).error.message).toContain(android ? 'native callback watchdog' : 'Apple GATT operation watchdog');
    expect(events.find((event) => event.kind === 'characteristicRead').value).toEqual([0]);
  });

  test.each((['duplicate', 'queued-domain', 'late-domain', 'wrong-id', 'js-timeout', 'bad-gatt', 'missing-terminal', 'missing-loss', 'missing-readvertisement', 'close', 'duplicate-at-close', 'bad-value', 'wrong-advertisement-service', 'wrong-recovery-service', 'wrong-gatt-service'] as const)
    .flatMap((fault) => [SERVICE, MULTIFUNCTION_SERVICE].map((service) => ({ fault, service }))))(
    'fails closed for $fault ($service) and always disposes', async ({ fault, service }) => {
      const { lines, error, fake } = await execute(false, fault, false, service);
      expect(error).toBeDefined();
      expect(lines).not.toContain('rf-watchdog:passed');
      expect(lines.at(-1)).toMatch(/^rf-watchdog:failed:/);
      expect(fake.closed).toHaveBeenCalledTimes(1);
      expect(fake.removed).toHaveBeenCalledTimes(1);
      if (fault === 'wrong-advertisement-service' || fault === 'wrong-recovery-service') {
        expect(lines.at(-1)).toContain('JS outer wait expired');
        expect(fake.calls).not.toContain(ID.recoveryConnect);
      }
      if (fault === 'wrong-gatt-service') expect(fake.calls).not.toContain(ID.active);
      if (fault === 'bad-gatt') expect(fake.calls).not.toContain(ID.active);
      if (fault === 'missing-terminal' || fault === 'missing-loss' || fault === 'missing-readvertisement') expect(lines.at(-1)).toContain('JS outer wait expired');
    },
  );

  test('does not infer installed image identity from a canonical service UUID', async () => {
    const service = 'fd000000-0000-4000-8000-000000000000';
    const { lines, error } = await execute(false, undefined, false, service);
    expect(error).toBeUndefined();
    expect(lines.at(-1)).toBe('rf-watchdog:passed');
    // This fake proves routing only. A real launch requires the host's live attestation.
  });

  test('waits for real loss and a full second of silence before recovery', async () => {
    const fake = fixture();
    const lines: string[] = [];
    const done = standalone(fake.probe, SERVICE, (line) => lines.push(line), () => false);
    await jest.advanceTimersByTimeAsync(10_100);
    expect(lines).toContain('rf-watchdog:awaiting-release');
    expect(fake.calls).not.toContain(ID.recoveryConnect);
    await jest.advanceTimersByTimeAsync(2_000);
    expect(fake.calls).not.toContain(ID.recoveryConnect);
    fake.release();
    await jest.advanceTimersByTimeAsync(950);
    expect(fake.calls).not.toContain(ID.recoveryConnect);
    await jest.advanceTimersByTimeAsync(2_000);
    await done;
    expect(lines.at(-1)).toBe('rf-watchdog:passed');
  });

  test('waits for host re-advertising rather than racing recovery connect', async () => {
    const fake = fixture(false, 'missing-readvertisement');
    const lines: string[] = [];
    const done = standalone(fake.probe, SERVICE, (line) => {
      lines.push(line);
      if (line === 'rf-watchdog:awaiting-release') fake.release();
    }, () => false);
    await jest.advanceTimersByTimeAsync(14_000);
    expect(lines).toContain('rf-watchdog:late-silence:observationMs=1000');
    expect(fake.calls).not.toContain(ID.recoveryConnect);
    fake.emit({ kind: 'advertisementObserved', scanSessionId: ID.recoveryScan, peripheralId: '876', advertisedServices: [SERVICE] });
    await jest.advanceTimersByTimeAsync(2_000);
    await done;
    expect(lines.at(-1)).toBe('rf-watchdog:passed');
  });

  test('dispatches active then queued without probing cancellation or claiming native entry', async () => {
    const fake = fixture();
    const lines: string[] = [];
    const done = standalone(fake.probe, SERVICE, (line) => {
      lines.push(line);
      if (line.startsWith('rf-watchdog:read-dispatched:')) {
        expect(fake.calls.slice(-2)).toEqual([ID.active, ID.queued]);
        expect(fake.probe.cancelOperation).not.toHaveBeenCalled();
        expect(lines.some((entry) => entry.includes(':active-timeout:'))).toBe(false);
      }
      if (line === 'rf-watchdog:awaiting-release') fake.release();
    }, () => false);
    await jest.advanceTimersByTimeAsync(100);
    expect(lines.some((line) => line.startsWith('rf-watchdog:read-dispatched:'))).toBe(true);
    expect(lines.some((line) => line.includes(':active-timeout:'))).toBe(false);
    await jest.advanceTimersByTimeAsync(15_000);
    await done;
    expect(fake.probe.cancelOperation).not.toHaveBeenCalled();
    expect(lines.at(-1)).toBe('rf-watchdog:passed');
  });

  test.each([SERVICE, MULTIFUNCTION_SERVICE])('projects extended Android GATT as compact JSON while validating full topology (%s)', async (service) => {
    const { lines, error, fake } = await execute(true, undefined, true, service);
    expect(error).toBeUndefined();
    expect(JSON.stringify(fake.gattServices).length).toBeGreaterThan(4096);
    const discoveryLines = lines.filter((line) => line.includes('"kind":"gattDiscovered"'));
    expect(discoveryLines).toHaveLength(2);
    for (const line of discoveryLines) {
      expect(`BLEHUB_NITRO_RUNTIME ${line}`.length).toBeLessThan(4096);
      const event = JSON.parse(line.slice('rf-watchdog:event:'.length));
      expect(event.gattServices).toBeUndefined();
      expect(event.gattSummary).toEqual({ serviceCount: 4, characteristicCount: 21, descriptorCount: 3 });
      expect(event.commandId).toBe(event.connectionId === ID.connection ? ID.discover : ID.recoveryDiscover);
      expect(event.gattSessionId).toBe(event.connectionId === ID.connection ? ID.gatt : ID.recoveryGatt);
    }
    expect(lines.at(-1)).toBe('rf-watchdog:passed');
    // Compact logging must not conceal a canonical characteristic property mismatch.
    const malformed = await execute(true, 'bad-gatt', true, service);
    expect(malformed.error).toBeDefined();
    expect(malformed.lines.at(-1)).toContain('property readable mismatch');
    expect(malformed.fake.calls).not.toContain(ID.active);
  });

  test.each([0, 1, 64, 65, 2048])('bounds logged values to 512 characters without altering %s native bytes', (size) => {
    const fake = fixture();
    const lines: string[] = [];
    const ledger = new WatchdogEventStream(fake.probe, (line) => lines.push(line));
    ledger.expect('1', { domains: ['characteristicRead'] });
    const value = new Uint8Array(size).fill(255).buffer;
    fake.emit({ kind: 'characteristicRead', commandId: '1', value });
    ledger.assertHealthy();
    expect(ledger.events[0].value).toBe(value);
    expect(ledger.events[0].value?.byteLength).toBe(size);
    const projected = JSON.parse(lines[0].slice('rf-watchdog:event:'.length));
    expect(JSON.stringify(projected.value).length).toBeLessThanOrEqual(512);
    expect(projected.value).toEqual(Array(Math.min(size, 64)).fill(255));
    expect(projected.valueByteLength).toBe(size);
    expect(projected.valueTruncated).toBe(size > 64);
    expect(`BLEHUB_NITRO_RUNTIME ${lines[0]}`.length).toBeLessThan(4096);
    ledger.remove();
  });

  test.each(['scanSessionId', 'connectionId', 'gattSessionId', 'characteristicId'] as const)('rejects mismatched %s and records the offending event', (field) => {
    const fake = fixture();
    const lines: string[] = [];
    const ledger = new WatchdogEventStream(fake.probe, (line) => lines.push(line));
    ledger.expect('1', { domains: ['characteristicRead'], connectionId: '2', gattSessionId: '3', characteristicId: '4' });
    fake.emit({ kind: 'characteristicRead', commandId: '1', connectionId: '2', gattSessionId: '3', characteristicId: '4', [field]: '999' });
    expect(() => ledger.assertHealthy()).toThrow();
    expect(lines).toHaveLength(1);
    ledger.remove();
  });

  test('retains watchdog details but cannot rerun a host-coordinated attempt with consumed controls', async () => {
    const fake = fixture(true);
    jest.mocked(createBleHubProbe).mockReturnValue(fake.probe);
    jest.spyOn(console, 'log').mockImplementation(standaloneRelay());
    const view = render(<React.StrictMode><App runtimeMode="rf-watchdog" rfServiceUuid={SERVICE} rfCancellationControl={JSON.stringify(STANDALONE_CONTROL)} rfExpectedLocalName="Multifunction" /></React.StrictMode>);
    expect(screen.getByTestId('rf-watchdog-root')).toBeTruthy();
    await act(async () => { await jest.advanceTimersByTimeAsync(15_000); });
    expect(screen.getByTestId('rf-watchdog-status')).toHaveTextContent('rf-watchdog passed');
    await act(async () => { await jest.advanceTimersByTimeAsync(60_000); });
    expect(screen.getByTestId('rf-watchdog-status')).toHaveTextContent('rf-watchdog passed');
    expect(createBleHubProbe).toHaveBeenCalledTimes(1);
    expect(RF_LAB_MODES).toHaveLength(10);
    expect(RF_LAB_MODES.at(-1)).toBe('rf-watchdog');
    expect(screen.getByTestId('rf-watchdog-rerun')).toBeDisabled();
    fireEvent.press(screen.getByTestId('rf-watchdog-rerun'));
    await act(async () => { await jest.advanceTimersByTimeAsync(15_000); });
    expect(createBleHubProbe).toHaveBeenCalledTimes(1);
    fireEvent.press(screen.getByTestId('rf-watchdog-close'));
    expect(screen.getByTestId('rf-watchdog-status')).toHaveTextContent('rf-watchdog closed');
    view.unmount();
  });

  test('explicit close stops an active diagnostic and disposes without a pass', async () => {
    const fake = fixture();
    jest.mocked(createBleHubProbe).mockReturnValue(fake.probe);
    jest.spyOn(console, 'log').mockImplementation(standaloneRelay());
    const view = render(<App runtimeMode="rf-watchdog" rfServiceUuid={SERVICE} rfCancellationControl={JSON.stringify(STANDALONE_CONTROL)} rfExpectedLocalName="Multifunction" />);
    await act(async () => { await jest.advanceTimersByTimeAsync(100); });
    expect(screen.getByTestId('rf-watchdog-status')).toHaveTextContent('rf-watchdog running');
    fireEvent.press(screen.getByTestId('rf-watchdog-close'));
    await act(async () => { await jest.advanceTimersByTimeAsync(100); });
    expect(screen.getByTestId('rf-watchdog-status')).toHaveTextContent('rf-watchdog closed');
    expect(fake.closed).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('rf-watchdog:passed')).toBeNull();
    view.unmount();
  });

  test("missing standalone control cannot construct a Probe or send BLE commands", async () => {
    jest.mocked(createBleHubProbe).mockClear();
    jest.spyOn(console, "log").mockImplementation(() => {});
    const view = render(<App runtimeMode="rf-watchdog" rfServiceUuid={SERVICE} rfExpectedLocalName="Multifunction" />);
    await act(async () => { await jest.advanceTimersByTimeAsync(100); });
    expect(createBleHubProbe).not.toHaveBeenCalled();
    expect(screen.getByTestId("rf-watchdog-status")).toHaveTextContent("rf-watchdog failed");
    view.unmount();
  });

  test.each([undefined, '', 'fd4', 'not-a-uuid', MULTIFUNCTION_SERVICE.toUpperCase(), ` ${MULTIFUNCTION_SERVICE}`, `${MULTIFUNCTION_SERVICE}\n`])('retains failure for missing or malformed launch service (%s)', async (service) => {
    jest.mocked(createBleHubProbe).mockClear();
    const fake = fixture();
    jest.mocked(createBleHubProbe).mockReturnValue(fake.probe);
    jest.spyOn(console, 'log').mockImplementation(standaloneRelay());
    const initialize = jest.spyOn(fake.probe, 'initialize');
    const listener = jest.spyOn(fake.probe, 'setRfScanListener');
    const view = render(<App runtimeMode="rf-watchdog" rfServiceUuid={service} rfCancellationControl={JSON.stringify(STANDALONE_CONTROL)} rfExpectedLocalName="Multifunction" />);
    await act(async () => { await jest.advanceTimersByTimeAsync(1_000); });
    expect(screen.getByTestId('rf-watchdog-status')).toHaveTextContent('rf-watchdog failed');
    await act(async () => { await jest.advanceTimersByTimeAsync(60_000); });
    expect(screen.getByTestId('rf-watchdog-status')).toHaveTextContent('rf-watchdog failed');
    expect(createBleHubProbe).toHaveBeenCalledTimes(1);
    expect(fake.closed).toHaveBeenCalledTimes(1);
    expect(initialize).not.toHaveBeenCalled();
    expect(listener).not.toHaveBeenCalled();
    view.unmount();
  });
});
