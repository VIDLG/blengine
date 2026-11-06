/// <reference types="node" />
import React, { Profiler } from 'react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, fireEvent, render, screen } from '@testing-library/react-native';
import { Linking, Platform, StatusBar, StyleSheet } from 'react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { DispatchError, RfResourceSnapshot, RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import App, { runRfLabFlow } from './App';
import { RfAdapterTransition, runRfAdapterTransition, type AdapterTransitionFlow } from './rf_adapter_transition';
import { RF_BOOT_NONCE } from './rf_runtime';
import { RunSessionProvider } from './rf_lab_session';

jest.mock('@blehub/react-native-internal', () => ({ createBleHubProbe: jest.fn() }));
const mockPresent = jest.fn();
jest.mock('react-native/Libraries/Settings/Settings.ios', () => ({ __esModule: true, default: { get: jest.fn() } }));
jest.mock('./multi_rf', () => ({ runRfMulti: jest.fn(), runRfMultiSoak: jest.fn() }));
jest.mock('./multi_fault', () => ({ runRfMultiFault: jest.fn() }));

// All fakes below are SOFTWARE-ONLY. No radio is toggled and no assertion is RF/native execution evidence.
type Probe = ReturnType<typeof createBleHubProbe>;
const P = 'rf-adapter-transition';
const SERVICE = 'fd100000-0000-4000-8000-000000000000';
const NAME = 'BleHubNrf52840';
const zero = (): RfResourceSnapshot => ({
  activeScans: 0, connections: 0, gattAttributes: 0, subscriptions: 0, pendingIo: 0, watchdogs: 0,
  callbackRoutes: 0, ingressRoots: 0, adapterDelegates: 0, peerCacheEntries: 0, backendLeases: 0,
  hubRoots: 0, handoffRoots: 0, handoffPending: 0, listeners: 0, workerThreads: 0, lifecycleOwners: 0,
});
const domain: RfScanEvent = { kind: 'adapterSnapshotChanged', commandId: '31000', adapterState: 'PoweredOff', scanAuthorization: 'Authorized' };
const terminal = (commandId: string): RfScanEvent => ({ kind: 'commandCompleted', commandId });
const unavailable: RfScanEvent = { ...terminal('31002'), error: {
  category: 'adapter_unavailable', message: 'Bluetooth adapter is off',
  platformDetails: { platform: 'platform_android', name: 'AndroidBleBackend', code: '10' },
} };
const signals = new Set<(event: { url: string }) => void>();
const signal = (url: string): void => { for (const listener of signals) listener({ url }); };
const url = (run: string): string => `blehub-rn-spike://adapter/${run}/on`;
let sequence = 0;
function profile() {
  sequence += 1;
  return { rfAdapterRunId: `12345678-1234-4234-8234-${String(sequence).padStart(12, '0')}`, rfServiceUuid: SERVICE, rfExpectedLocalName: NAME };
}
function offProbe(init: RfScanEvent[] = [domain, terminal('31000')], scan: RfScanEvent[] = [unavailable]) {
  let listener: (event: RfScanEvent) => void = () => {};
  const emit = (event: RfScanEvent): void => listener(event);
  const probe = {
    emit,
    setRfScanListener: jest.fn((callback: typeof listener) => { listener = callback; }),
    // Deliberately retain the callback even after close to attack the retired-listener guard.
    initialize: jest.fn((_id: string): DispatchError | undefined => { init.forEach(emit); return undefined; }),
    startRfScan: jest.fn((_id: string, _scan: string, _service: string): DispatchError | undefined => { scan.forEach(emit); return undefined; }),
    close: jest.fn(async () => {}),
    snapshotRfResources: jest.fn(async () => zero()),
  };
  return probe;
}
const asProbe = (probe: object): Probe => probe as Probe;
const passingFlow: AdapterTransitionFlow = async (_mode, _probe, _service, report) => { report('rf-io:passed:software-only'); };

function start(options: { off?: ReturnType<typeof offProbe>; on?: ReturnType<typeof offProbe>; flow?: AdapterTransitionFlow; autoSignal?: boolean; cancelled?: () => boolean; onReport?: (line: string) => void;
  props?: ReturnType<typeof profile> & { rfAdapterNativeIdentity?: string; rfAdapterMode?: 'startup-off' | 'active-loss' };
  bindOnRequest?: (request: () => void) => void;
} = {}) {
  const props = options.props ?? profile();
  const off = options.off ?? offProbe();
  const on = options.on ?? offProbe();
  const probes = [asProbe(off), asProbe(on)];
  const factory = jest.fn(() => probes.shift()!);
  const lines: string[] = [];
  const report = (line: string): void => {
    lines.push(line);
    options.onReport?.(line);
    if (options.autoSignal !== false && line === `${P}:awaiting-on:runId=${props.rfAdapterRunId}`) signal(url(props.rfAdapterRunId));
  };
  const result = runRfAdapterTransition(factory, options.flow ?? passingFlow, props, report, options.cancelled ?? (() => false), options.bindOnRequest);
  return { result, off, on, factory, lines, props };
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

async function finish(run: ReturnType<typeof start>): Promise<boolean> {
  await jest.runAllTimersAsync();
  return run.result;
}
function expectFailed(run: ReturnType<typeof start>): void {
  expect(run.lines.some((line) => line.startsWith(`${P}:failed:`))).toBe(true);
  expect(run.lines.some((line) => line.startsWith(`${P}:passed:`))).toBe(false);
}

describe('Android adapter transition software ledger', () => {
  test('exact marker protocol, original events/errors, two fresh Probes and both zero snapshots', async () => {
    const flow: AdapterTransitionFlow = jest.fn(async (mode, _probe, service, report, cancelled, name) => {
      expect([mode, service, name, cancelled()]).toEqual(['rf-io', SERVICE, NAME, false]);
      report('rf:step:read-initial:passed:value=00');
      report('rf-io:passed:software-only');
    });
    const run = start({ flow });
    expect(await finish(run)).toBe(true);
    const id = run.props.rfAdapterRunId;
    expect(run.lines).toEqual([
      `${P}:runtime:runId=${id}:nonce=${RF_BOOT_NONCE}:phase=start`,
      ...[domain, terminal('31000'), unavailable].map((event) => `${P}:off:event:${JSON.stringify(event)}`),
      `${P}:off:resources:${JSON.stringify(zero())}`, `${P}:off:passed`,
      `${P}:awaiting-on:runId=${id}`, `${P}:on-signal:runId=${id}:source=linking`, `${P}:on:started`,
      `${P}:on:flow:rf:step:read-initial:passed:value=00`, `${P}:on:flow:rf-io:passed:software-only`,
      `${P}:on:resources:${JSON.stringify(zero())}`, `${P}:on:passed`,
      `${P}:runtime:runId=${id}:nonce=${RF_BOOT_NONCE}:phase=close`, `${P}:passed:runId=${id}`,
    ]);
    expect(run.factory).toHaveBeenCalledTimes(2);
    expect(run.off.initialize).toHaveBeenCalledWith('31000');
    expect(run.off.setRfScanListener.mock.invocationCallOrder[0]).toBeLessThan(run.off.initialize.mock.invocationCallOrder[0]);
    expect(Linking.addEventListener).toHaveBeenCalledTimes(1);
    expect(jest.mocked(Linking.addEventListener).mock.invocationCallOrder[0]).toBeLessThan(run.factory.mock.invocationCallOrder[0]);
    expect(run.off.startRfScan).toHaveBeenCalledWith('31002', '31001', SERVICE);
    for (const probe of [run.off, run.on]) {
      expect(probe.close).toHaveBeenCalledTimes(1);
      expect(probe.close.mock.invocationCallOrder[0]).toBeLessThan(probe.snapshotRfResources.mock.invocationCallOrder[0]);
    }
    expect(signals.size).toBe(0);
  });

  test.each([
    ['terminal-before-domain', [terminal('31000'), domain]],
    ['PoweredOn', [{ ...domain, adapterState: 'PoweredOn' }, terminal('31000')]],
    ['denied', [{ ...domain, scanAuthorization: 'Denied' }, terminal('31000')]],
    ['initialize-error', [domain, { ...terminal('31000'), error: unavailable.error }]],
    ['duplicate-initialize-terminal', [domain, terminal('31000'), terminal('31000')]],
  ] as const)('off rejects %s before scan', async (_label, events) => {
    const run = start({ off: offProbe([...events]) });
    expect(await finish(run)).toBe(false);
    expectFailed(run);
    expect(run.off.startRfScan).not.toHaveBeenCalled();
    expect(run.off.close).toHaveBeenCalledTimes(1);
    expect(run.factory).toHaveBeenCalledTimes(1);
  });

  test.each([
    ['successful scan terminal', [terminal('31002')]],
    ['wrong error', [{ ...unavailable, error: { category: 'permission', message: 'denied' } }]],
    ['duplicate scan terminal', [unavailable, unavailable]],
    ['scanStarted', [{ kind: 'scanStarted', commandId: '31002', scanSessionId: '31001' }, unavailable]],
    ['advertisement', [unavailable, { kind: 'advertisementObserved', scanSessionId: '31001' }]],
  ] as [string, RfScanEvent[]][])('off rejects %s', async (_label, events) => {
    const run = start({ off: offProbe(undefined, events) });
    expect(await finish(run)).toBe(false);
    expectFailed(run);
    expect(run.factory).toHaveBeenCalledTimes(1);
  });

  test.each(['initialize', 'startRfScan'] as const)('off %s must be dispatch-accepted', async (method) => {
    const off = offProbe();
    off[method].mockReturnValue({ category: 'invalid_state' });
    const run = start({ off });
    expect(await finish(run)).toBe(false);
    expect(run.lines.join('\n')).toContain('dispatch rejected: invalid_state');
    expect(run.factory).toHaveBeenCalledTimes(1);
  });

  test('early canonical URL is fatal before initialize and never becomes stored readiness', async () => {
    const props = profile();
    const off = offProbe();
    const factory = jest.fn(() => asProbe(off));
    const lines: string[] = [];
    const result = runRfAdapterTransition(factory, passingFlow, props, (line) => {
      lines.push(line);
      if (line.endsWith('phase=start')) signal(url(props.rfAdapterRunId));
    }, () => false);
    expect(await result).toBe(false);
    expect(factory).not.toHaveBeenCalled();
    expect(lines.join('\n')).toContain('foreign, early or duplicate on URL');
  });

  test.each(['foreign', 'duplicate', 'query', 'encoded', 'wrong-case'])('%s readiness URL fails closed', async (kind) => {
    let injected = false;
    const run = start({ autoSignal: false, onReport: (line) => {
      const id = line.startsWith(`${P}:awaiting-on:runId=`) ? line.split('runId=')[1] : undefined;
      if (!id || injected) return;
      injected = true;
      const canonical = url(id);
      if (kind === 'duplicate') { signal(canonical); signal(canonical); }
      else signal(kind === 'foreign' ? url('00000000-0000-0000-0000-000000000000')
        : kind === 'query' ? `${canonical}?ready=1` : kind === 'encoded' ? canonical.replace('/on', '/%6fn') : canonical.replace('adapter', 'Adapter'));
    } });
    expect(await finish(run)).toBe(false);
    expectFailed(run);
    expect(run.factory).toHaveBeenCalledTimes(1);
  });

  test('on wait is bounded to 30s and cancellation cleans up without a second Probe', async () => {
    const timeout = start({ autoSignal: false });
    expect(await finish(timeout)).toBe(false);
    expect(timeout.lines.join('\n')).toContain('on signal timed out after 30000ms');
    let cancel = false;
    const cancelled = start({ autoSignal: false, cancelled: () => cancel, onReport: (line) => {
      if (line.startsWith(`${P}:awaiting-on:`)) cancel = true;
    } });
    expect(await finish(cancelled)).toBe(false);
    expect(cancelled.lines.join('\n')).toContain('adapter transition cancelled');
    expect(cancelled.factory).toHaveBeenCalledTimes(1);
  });

  test.each(['off', 'on'] as const)('%s close rejection is fatal and never retried', async (phase) => {
    const off = offProbe(); const on = offProbe();
    const failed = phase === 'off' ? off : on;
    failed.close.mockRejectedValue(new Error('native disposal failed'));
    const run = start({ off, on });
    expect(await finish(run)).toBe(false);
    expectFailed(run);
    expect(run.lines.join('\n')).toContain(`${phase} close failed: native disposal failed`);
    expect(failed.close).toHaveBeenCalledTimes(1);
    expect(failed.snapshotRfResources).not.toHaveBeenCalled();
    expect(run.factory).toHaveBeenCalledTimes(phase === 'off' ? 1 : 2);
  });

  test.each(['off', 'on'] as const)('%s snapshot must have all and only 17 zero counters', async (phase) => {
    for (const snapshot of [{ ...zero(), callbackRoutes: 1 }, { ...zero(), extra: 0 }, {}, { ...zero(), workerThreads: -0 }]) {
      const off = offProbe(); const on = offProbe();
      (phase === 'off' ? off : on).snapshotRfResources.mockResolvedValue(snapshot as RfResourceSnapshot);
      const run = start({ off, on });
      expect(await finish(run)).toBe(false);
      expectFailed(run);
      expect(run.lines).not.toContain(`${P}:${phase}:passed`);
    }
  });

  test('old off callback during on flow still invalidates the result', async () => {
    const off = offProbe();
    const run = start({ off, flow: async (_mode, _probe, _service, report) => {
      off.emit(unavailable);
      report('rf-io:passed:software-only');
    } });
    expect(await finish(run)).toBe(false);
    expect(run.lines.join('\n')).toContain('late off callback after close began');
    expect(run.on.close).toHaveBeenCalledTimes(1);
    expectFailed(run);
  });

  test.each([
    ['rf:passed:wrong-mode'], ['not-rf-io:passed:wrong-prefix'], ['rf-io:passedness'], [],
    ['rf-io:passed:one', 'rf-io:passed:two'], ['rf:close:rejected:native', 'rf-io:passed:one'],
    ['rf:step:read-initial:failed:value', 'rf-io:passed:one'],
  ])('on reports %j cannot produce a summary pass', async (...lines: string[]) => {
    const run = start({ flow: async (_mode, _probe, _service, report) => { lines.forEach(report); } });
    expect(await finish(run)).toBe(false);
    expectFailed(run);
    expect(run.on.close).toHaveBeenCalledTimes(1);
  });

  test('Android rejects the iOS ready URL without emitting iOS markers or opening a URL', async () => {
    const open = jest.spyOn(Linking, 'openURL').mockResolvedValue(undefined);
    const run = start({ autoSignal: false });
    await jest.advanceTimersByTimeAsync(0);
    signal(url(run.props.rfAdapterRunId).replace('/on', '/ready'));
    expect(await finish(run)).toBe(false);
    expect(run.factory).toHaveBeenCalledTimes(1);
    expect(open).not.toHaveBeenCalled();
    expect(run.lines.join('\n')).not.toMatch(/host-window-ready|on-request/);
  });
});

// Exercise the REAL existing rf-io executor with simulated events, not a fabricated flow verdict.
function iosProfile() {
  const props = profile();
  return { ...props, rfAdapterNativeIdentity: JSON.stringify({
    run_id: props.rfAdapterRunId, adapter_mode: 'startup-off', pid: 123,
    boot_id: '11111111-1111-4111-8111-111111111abc',
    bundle_id: 'org.reactjs.native.example.BleHubNitroHost',
    executable: 'file:///private/var/containers/BleHubNitroHost.app/BleHubNitroHost', source: 'launch',
  }) };
}
const readyUrl = (id: string): string => `blehub-rn-spike://adapter/${id}/ready`;

function startIos(options: Parameters<typeof start>[0] = {}) {
  let request = (): void => { throw new Error('ON request was not bound'); };
  const iosUnavailable: RfScanEvent = { ...unavailable, error: { category: 'adapter_unavailable', message: 'CoreBluetooth adapter is not powered on' } };
  const run = start({ props: iosProfile(), off: offProbe(undefined, [iosUnavailable]), ...options,
    autoSignal: false, bindOnRequest: (callback) => { request = callback; } });
  return { ...run, request: () => request() };
}

describe('iOS startup-off candidate foundation (software only)', () => {
  beforeEach(() => {
    jest.replaceProperty(Platform, 'OS', 'ios');
    jest.spyOn(Linking, 'openURL').mockResolvedValue(undefined);
  });

  test.each([
    ['extra key', { extra: true }], ['wrong run', { run_id: '00000000-0000-0000-0000-000000000000' }],
    ['noncanonical run', { run_id: '12345678-1234-4234-8234-000000000ABC' }],
    ['wrong mode', { adapter_mode: 'active-loss' }], ['wrong source', { source: 'payload-url' }],
    ['zero pid', { pid: 0 }], ['negative pid', { pid: -1 }], ['fractional pid', { pid: 1.5 }],
    ['string pid', { pid: '123' }], ['unsafe pid', { pid: Number.MAX_SAFE_INTEGER + 1 }],
    ['noncanonical boot', { boot_id: '11111111-1111-4111-8111-111111111ABC' }],
    ['invalid boot', { boot_id: 'boot' }], ['wrong bundle', { bundle_id: 'other.app' }],
    ['https executable', { executable: 'https://example.com/App' }], ['relative executable', { executable: '/App' }],
    ['missing executable path', { executable: 'file:///' }], ['directory executable', { executable: 'file:///App/' }],
    ['executable query', { executable: 'file:///App?x=1' }], ['executable fragment', { executable: 'file:///App#x' }],
    ['malformed escape', { executable: 'file:///App%ZZ' }], ['encoded control', { executable: 'file:///App%00' }],
    ['executable whitespace', { executable: 'file:///My App' }], ['remote file host', { executable: 'file://other/App' }],
  ])('rejects %s identity before constructing a Probe', async (_label, patch) => {
    const props = iosProfile();
    props.rfAdapterNativeIdentity = JSON.stringify({ ...JSON.parse(props.rfAdapterNativeIdentity), ...patch });
    const run = startIos({ props });
    expect(await finish(run)).toBe(false);
    expect(run.factory).not.toHaveBeenCalled();
    expect(run.lines).toEqual([`${P}:failed:invalid iOS adapter native identity`]);
    expect(signals.size).toBe(0);
  });

  test.each(['run_id', 'adapter_mode', 'pid', 'boot_id', 'bundle_id', 'executable', 'source'])(
    'rejects missing identity key %s', async (key) => {
      const props = iosProfile(); const native = JSON.parse(props.rfAdapterNativeIdentity);
      delete native[key]; props.rfAdapterNativeIdentity = JSON.stringify(native);
      const run = startIos({ props });
      expect(await finish(run)).toBe(false);
      expect(run.factory).not.toHaveBeenCalled();
    });

  test.each([undefined, '', '{', 'null', '[]', '123', '"identity"'])('rejects malformed identity %j', async (raw) => {
    const run = startIos({ props: { ...iosProfile(), rfAdapterNativeIdentity: raw } });
    expect(await finish(run)).toBe(false);
    expect(run.factory).not.toHaveBeenCalled();
  });

  test('active-loss remains explicitly unsupported, even with a valid launch identity', async () => {
    const run = startIos({ props: { ...iosProfile(), rfAdapterMode: 'active-loss' } });
    expect(await finish(run)).toBe(false);
    expect(run.factory).not.toHaveBeenCalled();
    expect(run.lines).toEqual([`${P}:failed:iOS supports startup-off only`]);
  });

  test('ready is accepted only after off close, request only opens the URL, and native callback alone advances', async () => {
    const run = startIos(); const id = run.props.rfAdapterRunId;
    await jest.advanceTimersByTimeAsync(0);
    expect(run.off.close).toHaveBeenCalledTimes(1);
    expect(run.off.snapshotRfResources).toHaveBeenCalledTimes(1);
    signal(readyUrl(id));
    expect(run.lines.at(-1)).toBe(`${P}:host-window-ready:runId=${id}:source=linking`);
    run.request();
    expect(run.lines.at(-1)).toBe(`${P}:on-request:runId=${id}:source=ui`);
    expect(Linking.openURL).toHaveBeenCalledWith(url(id));
    await jest.advanceTimersByTimeAsync(25);
    expect(run.factory).toHaveBeenCalledTimes(1);
    signal(url(id));
    expect(await finish(run)).toBe(true);
    expect(run.factory).toHaveBeenCalledTimes(2);
    expect(run.lines).toContain(`${P}:on-signal:runId=${id}:source=linking`);
    expect(run.lines).toContain(`${P}:runtime:runId=${id}:nonce=${RF_BOOT_NONCE}:phase=close`);
    expect(jest.mocked(Linking.addEventListener).mock.invocationCallOrder.at(-1)!).toBeLessThan(run.factory.mock.invocationCallOrder[0]);
  });

  test('ready before startup is sticky and creates no Probe', async () => {
    const props = iosProfile();
    const run = startIos({ props, onReport: (line) => {
      if (line.endsWith('phase=start')) signal(readyUrl(props.rfAdapterRunId));
    } });
    expect(await finish(run)).toBe(false);
    expect(run.factory).not.toHaveBeenCalled();
    expect(run.lines.join('\n')).toContain('early or duplicate ready URL');
  });

  test('ready while off close is pending cannot authorize ON', async () => {
    let release = (): void => {};
    const off = offProbe(); off.close.mockImplementation(() => new Promise<void>((resolveClose) => { release = resolveClose; }));
    const run = startIos({ off });
    await jest.advanceTimersByTimeAsync(0);
    expect(off.close).toHaveBeenCalledTimes(1);
    signal(readyUrl(run.props.rfAdapterRunId));
    release();
    expect(await finish(run)).toBe(false);
    expect(run.factory).toHaveBeenCalledTimes(1);
    expect(run.lines.join('\n')).not.toContain('host-window-ready');
  });

  test.each(['duplicate', 'foreign-run', 'query', 'encoded', 'wrong-case'])(
    '%s ready URL fails closed', async (kind) => {
      const run = startIos(); const canonical = readyUrl(run.props.rfAdapterRunId);
      await jest.advanceTimersByTimeAsync(0);
      if (kind === 'duplicate') { signal(canonical); signal(canonical); }
      else signal(kind === 'foreign-run' ? readyUrl('00000000-0000-0000-0000-000000000000')
        : kind === 'query' ? `${canonical}?ready=1` : kind === 'encoded' ? canonical.replace('/ready', '/%72eady')
          : canonical.replace('adapter', 'Adapter'));
      run.request(); signal(url(run.props.rfAdapterRunId));
      expect(await finish(run)).toBe(false);
      expect(run.factory).toHaveBeenCalledTimes(1);
      expect(Linking.openURL).not.toHaveBeenCalled();
    });

  test.each(['before-ready', 'before-request', 'duplicate-on'])('%s ON callback fails closed', async (kind) => {
    const run = startIos(); const id = run.props.rfAdapterRunId;
    await jest.advanceTimersByTimeAsync(0);
    if (kind !== 'before-ready') signal(readyUrl(id));
    if (kind === 'duplicate-on') { run.request(); signal(url(id)); }
    signal(url(id));
    expect(await finish(run)).toBe(false);
    expect(run.factory).toHaveBeenCalledTimes(1);
    expectFailed(run);
  });

  test.each(['early', 'duplicate', 'open-rejected'])('%s UI request fails without native recovery', async (kind) => {
    const run = startIos();
    await jest.advanceTimersByTimeAsync(0);
    if (kind !== 'early') signal(readyUrl(run.props.rfAdapterRunId));
    if (kind === 'open-rejected') jest.mocked(Linking.openURL).mockRejectedValueOnce(new Error('native rejected ON'));
    run.request();
    if (kind === 'duplicate') run.request();
    await Promise.resolve();
    signal(url(run.props.rfAdapterRunId));
    expect(await finish(run)).toBe(false);
    expect(run.factory).toHaveBeenCalledTimes(1);
    expect(Linking.openURL).toHaveBeenCalledTimes(kind === 'early' ? 0 : 1);
    expectFailed(run);
  });

  test.each(['PoweredOn', 'Unknown', 'Denied', 'scan-success'])('iOS still rejects OFF phase %s', async (kind) => {
    const off = kind === 'scan-success' ? offProbe(undefined, [terminal('31002')])
      : offProbe([{ ...domain, ...(kind === 'Denied' ? { scanAuthorization: 'Denied' } : { adapterState: kind }) }, terminal('31000')]);
    const run = startIos({ off });
    expect(await finish(run)).toBe(false);
    expect(run.factory).toHaveBeenCalledTimes(1);
    expectFailed(run);
  });

  test.each(['off-close', 'off-nonzero', 'on-close', 'on-nonzero', 'old-callback'])(
    '%s cannot produce a passing iOS result', async (kind) => {
      const off = offProbe(); const on = offProbe(); const failing = kind.startsWith('off') ? off : on;
      if (kind.endsWith('close')) failing.close.mockRejectedValueOnce(new Error('native dispose failed'));
      if (kind.endsWith('nonzero')) failing.snapshotRfResources.mockResolvedValue({ ...zero(), backendLeases: 1 });
      const run = startIos({ off, on, flow: async (...args) => {
        if (kind === 'old-callback') off.emit(unavailable);
        await passingFlow(...args);
      } });
      await jest.advanceTimersByTimeAsync(0);
      if (!kind.startsWith('off')) { signal(readyUrl(run.props.rfAdapterRunId)); run.request(); signal(url(run.props.rfAdapterRunId)); }
      expect(await finish(run)).toBe(false);
      expectFailed(run);
      expect(run.factory).toHaveBeenCalledTimes(kind.startsWith('off') ? 1 : 2);
      if (kind.endsWith('close')) expect(failing.close).toHaveBeenCalledTimes(1);
    });

  test('ready does not bypass the bounded ON wait or create an unsolicited second Probe', async () => {
    const run = startIos();
    await jest.advanceTimersByTimeAsync(0);
    signal(readyUrl(run.props.rfAdapterRunId));
    expect(await finish(run)).toBe(false);
    expect(run.lines.join('\n')).toContain('on signal timed out after 30000ms');
    expect(run.factory).toHaveBeenCalledTimes(1);
    expect(Linking.openURL).not.toHaveBeenCalled();
  });
});

function softwareIoProbe() {
  let listener: (event: RfScanEvent) => void = () => {};
  let value = new Uint8Array([0]).buffer;
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
    setRfScanListener(callback: typeof listener) { listener = callback; },
    removeRfScanListener() { listener = () => {}; },
    initialize: jest.fn((commandId: string) => {
      emit({ kind: 'adapterSnapshotChanged', commandId, adapterState: 'PoweredOn', scanAuthorization: 'Authorized' });
      complete(commandId);
    }),
    startRfScan: jest.fn((commandId: string, scanSessionId: string) => {
      emit({ kind: 'scanStarted', commandId, scanSessionId }); complete(commandId);
      emit({ kind: 'advertisementObserved', scanSessionId, peripheralId: '42', localName: NAME, advertisedServices: [SERVICE] });
    }),
    stopRfScan(commandId: string, scanSessionId: string) { emit({ kind: 'scanStopped', commandId, scanSessionId }); complete(commandId); },
    connectRfPeripheral(commandId: string, connectionId: string) {
      for (const connectionState of ['Connecting', 'Connected']) emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState });
      complete(commandId);
    },
    discoverRfGatt(commandId: string, connectionId: string, gattSessionId: string) {
      emit({ kind: 'gattDiscovered', commandId, connectionId, gattSessionId, gattServices }); complete(commandId);
    },
    readRfCharacteristic(commandId: string, connectionId: string, gattSessionId: string, characteristicId: string) {
      emit({ kind: 'characteristicRead', commandId, connectionId, gattSessionId, characteristicId, value }); complete(commandId);
    },
    writeRfCharacteristic(commandId: string, connectionId: string, gattSessionId: string, characteristicId: string, bytes: ArrayBuffer, writeType: string) {
      value = bytes;
      // No-response success is local submission, not a simulated remote ACK.
      emit({ kind: 'characteristicWritten', commandId, connectionId, gattSessionId, characteristicId });
      complete(commandId);
    },
    disconnectRfPeripheral(commandId: string, connectionId: string) {
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Disconnected' }); complete(commandId);
    },
    close: jest.fn(async () => {}), snapshotRfResources: jest.fn(async () => zero()),
  };
}

test('App integrates adapter cards into the RF Lab suite shell with readable insets and scenario-only results (software only)', async () => {
  jest.replaceProperty(StatusBar, 'currentHeight', 32);
  const off = offProbe(); const on = softwareIoProbe(); const props = profile();
  jest.mocked(createBleHubProbe).mockReturnValueOnce(asProbe(off)).mockReturnValueOnce(asProbe(on));
  render(<App runtimeMode="rf-adapter-transition" {...props} />);
  const page = screen.getByTestId('rf-adapter-transition-page');
  expect(StyleSheet.flatten(screen.getByTestId('rf-workbench-root').props.style)).toMatchObject({ flex: 1, backgroundColor: '#f5f7fa', paddingTop: 32 });
  const scroll = screen.getByTestId('rf-lab-full-scroll');
  expect(StyleSheet.flatten(scroll.props.contentContainerStyle)).toMatchObject({ padding: 20, paddingBottom: 32 });
  expect(scroll.props.contentInsetAdjustmentBehavior).toBe('automatic');
  expect(StyleSheet.flatten(screen.getByText('RF Lab').props.style)).toMatchObject({ color: '#122033' });
  expect(screen.getByText('Android adapter transition')).toBeTruthy();
  expect(screen.queryByTestId('rf-adapter-transition-confirm-on')).toBeNull();
  expect(screen.queryByTestId('rf-adapter-transition-identity')).toBeNull();
  expect(screen.getByText(/Separate from the full suite/)).toBeTruthy();
  expect(screen.queryByTestId('rf-lab-full-flow-0')).toBeNull();
  expect(screen.queryByTestId('rf-adapter-transition-scroll')).toBeNull();
  expect(StyleSheet.flatten(screen.getByTestId('rf-adapter-transition-off').props.style)).toMatchObject({ color: '#243447' });
  await act(async () => { await jest.advanceTimersByTimeAsync(0); });
  expect(screen.getByTestId('rf-adapter-transition-signal').props.children).toEqual(['Signal: ', 'Awaiting on URL (30s)']);
  expect(createBleHubProbe).toHaveBeenCalledTimes(1);
  fireEvent.press(screen.getByTestId('rf-workbench-home'));
  expect(screen.getByTestId('rf-workbench-active', { includeHiddenElements: true }).props.importantForAccessibility).toBe('no-hide-descendants');
  expect(off.close).toHaveBeenCalledTimes(1);
  fireEvent.press(screen.getByTestId('rf-workbench-category-lifecycle'));
  fireEvent.press(screen.getByTestId('rf-workbench-scenario-rf-adapter-transition'));
  expect(screen.getByTestId('rf-workbench-view-scenario').props.accessibilityState.disabled).toBe(false);
  expect(screen.getByTestId('rf-workbench-open-interactive').props.accessibilityState.disabled).toBe(true);
  await act(async () => { signal(url(props.rfAdapterRunId)); await jest.runAllTimersAsync(); });
  expect(createBleHubProbe).toHaveBeenCalledTimes(2);
  fireEvent.press(screen.getByTestId('rf-workbench-view-scenario'));
  expect(on.initialize).toHaveBeenCalledWith('9001');
  expect(on.startRfScan).toHaveBeenCalledWith('9002', '9004', SERVICE);
  expect(on.close).toHaveBeenCalledTimes(2); // Existing flow close + explicit idempotent close.
  const lines = jest.mocked(console.log).mock.calls.map(([line]) => String(line));
  expect(lines.filter((line) => /:failed:|:rejected:/.test(line))).toEqual([]);
  expect(lines).toContainEqual(expect.stringContaining(`BLEHUB_NITRO_RUNTIME ${P}:on:flow:rf-io:passed:serviceUuid=${SERVICE}:`));
  expect(lines.at(-1)).toBe(`BLEHUB_NITRO_RUNTIME ${P}:passed:runId=${props.rfAdapterRunId}`);
  expect(screen.getByTestId('rf-adapter-transition-page')).toBe(page);
  expect(screen.getByTestId('rf-adapter-transition-result').props.children).toBe('Passed');
  expect(StyleSheet.flatten(screen.getByTestId('rf-adapter-transition-result').props.style)).toMatchObject({ color: '#218739' });
  expect(screen.getByTestId('rf-adapter-transition-cancel').props.accessibilityState).toEqual({ disabled: true });
  fireEvent.press(screen.getByTestId('rf-workbench-home'));
  fireEvent.press(screen.getByTestId('rf-workbench-current'));
  expect(screen.getByTestId('rf-adapter-transition-page')).toBe(page);
  expect(screen.getByTestId('rf-adapter-transition-result').props.children).toBe('Passed');
  expect(createBleHubProbe).toHaveBeenCalledTimes(2);
});

test('iOS App keeps one native/JS identity across ready, UI request, native-forwarded ON and real I/O executor (software only)', async () => {
  jest.replaceProperty(Platform, 'OS', 'ios');
  const open = jest.spyOn(Linking, 'openURL').mockResolvedValue(undefined);
  const props = iosProfile();
  const native = JSON.parse(props.rfAdapterNativeIdentity);
  native.executable = 'file:///Application%20Support/BleHubNitroHost.app/BleHubNitroHost';
  props.rfAdapterNativeIdentity = JSON.stringify(native);
  const off = offProbe(); const on = softwareIoProbe();
  jest.spyOn(on, 'readRfCharacteristic');
  jest.spyOn(on, 'writeRfCharacteristic');
  jest.mocked(createBleHubProbe).mockReturnValueOnce(asProbe(off)).mockReturnValueOnce(asProbe(on));
  const view = render(<App runtimeMode="rf-adapter-transition" {...props} />);
  const page = screen.getByTestId('rf-adapter-transition-page');
  const identity = screen.getByTestId('rf-adapter-transition-identity');
  const serialized = identity.props.accessibilityLabel;
  expect(JSON.parse(serialized)).toEqual({ native, run_id: props.rfAdapterRunId, nonce: RF_BOOT_NONCE });
  expect(screen.getByText(/OFF before backend initialization/)).toBeTruthy();
  expect(screen.getByTestId('rf-adapter-transition-confirm-on').props.accessibilityState.disabled).toBe(true);
  await act(async () => { await jest.advanceTimersByTimeAsync(0); });
  expect(off.close).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId('rf-adapter-transition-confirm-on').props.accessibilityState.disabled).toBe(true);
  await act(async () => { signal(readyUrl(props.rfAdapterRunId)); });
  expect(screen.getByTestId('rf-adapter-transition-confirm-on').props.accessibilityState.disabled).toBe(false);
  view.rerender(<App runtimeMode="rf-adapter-transition" {...props}
    rfAdapterNativeIdentity={JSON.stringify({ ...native, pid: 456 })} />);
  expect(screen.getByTestId('rf-adapter-transition-identity').props.accessibilityLabel).toBe(serialized);
  expect(createBleHubProbe).toHaveBeenCalledTimes(1);
  fireEvent.press(screen.getByTestId('rf-adapter-transition-confirm-on'));
  expect(open).toHaveBeenCalledTimes(1);
  expect(open).toHaveBeenCalledWith(url(props.rfAdapterRunId));
  await act(async () => { await jest.advanceTimersByTimeAsync(25); });
  expect(createBleHubProbe).toHaveBeenCalledTimes(1);
  fireEvent.press(screen.getByTestId('rf-workbench-home'));
  fireEvent.press(screen.getByTestId('rf-workbench-evidence'));
  fireEvent.press(screen.getByTestId('rf-workbench-current'));
  expect(screen.getByTestId('rf-adapter-transition-page')).toBe(page);
  expect(screen.getByTestId('rf-adapter-transition-identity')).toBe(identity);
  expect(identity.props.accessibilityLabel).toBe(serialized);
  await act(async () => { signal(url(props.rfAdapterRunId)); await jest.runAllTimersAsync(); });
  expect(createBleHubProbe).toHaveBeenCalledTimes(2);
  expect(on.initialize).toHaveBeenCalledWith('9001');
  expect(on.readRfCharacteristic).toHaveBeenCalled();
  expect(on.writeRfCharacteristic).toHaveBeenCalled();
  expect(on.close).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId('rf-adapter-transition-result')).toHaveTextContent('Passed');
  expect(screen.getByTestId('rf-adapter-transition-identity').props.accessibilityLabel).toBe(serialized);
  expect(screen.getByTestId('rf-workbench-run-state')).toHaveTextContent('App checks · Passed');
  expect(screen.getByTestId('rf-adapter-transition-confirm-on').props.accessibilityState.disabled).toBe(true);
  const lines = jest.mocked(console.log).mock.calls.map(([line]) => String(line));
  expect(lines.filter((line) => /:failed:|:rejected:/.test(line))).toEqual([]);
  expect(lines.filter((line) => line.includes(`${P}:host-window-ready:`))).toEqual([
    `BLEHUB_NITRO_RUNTIME ${P}:host-window-ready:runId=${props.rfAdapterRunId}:source=linking`,
  ]);
  expect(lines.filter((line) => line.includes(`${P}:on-request:`))).toEqual([
    `BLEHUB_NITRO_RUNTIME ${P}:on-request:runId=${props.rfAdapterRunId}:source=ui`,
  ]);
});

test('iOS duplicate confirmation is sticky failure and opens only once', async () => {
  jest.replaceProperty(Platform, 'OS', 'ios');
  const open = jest.spyOn(Linking, 'openURL').mockResolvedValue(undefined);
  const props = iosProfile(); const off = offProbe();
  jest.mocked(createBleHubProbe).mockReturnValue(asProbe(off));
  render(<RfAdapterTransition {...props} runFlow={passingFlow} />);
  await act(async () => { await jest.advanceTimersByTimeAsync(0); signal(readyUrl(props.rfAdapterRunId)); });
  const button = screen.getByTestId('rf-adapter-transition-confirm-on');
  fireEvent.press(button); fireEvent.press(button);
  await act(async () => { signal(url(props.rfAdapterRunId)); await jest.runAllTimersAsync(); });
  expect(open).toHaveBeenCalledTimes(1);
  expect(createBleHubProbe).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId('rf-adapter-transition-result')).toHaveTextContent('Failed');
  expect(screen.getByTestId('rf-adapter-transition-failure')).toHaveTextContent(/duplicate ON request/);
});

test('fixed UI ignores high-rate diagnostics; cancellation is supported', async () => {
  let commits = 0;
  let send: (line: string) => void = () => {};
  let finishFlow: () => void = () => {};
  const flow: AdapterTransitionFlow = async (_mode, _probe, _service, report) => {
    send = report;
    await new Promise<void>((resolve) => { finishFlow = resolve; });
  };
  const off = offProbe(); const on = offProbe(); const props = profile();
  jest.mocked(createBleHubProbe).mockReturnValueOnce(asProbe(off)).mockReturnValueOnce(asProbe(on));
  render(<Profiler id="adapter" onRender={() => { commits += 1; }}><RfAdapterTransition {...props} runFlow={flow} /></Profiler>);
  await act(async () => { await jest.advanceTimersByTimeAsync(0); signal(url(props.rfAdapterRunId)); await jest.advanceTimersByTimeAsync(25); });
  const hosts = () => screen.root.findAll((node: { type: unknown }) => typeof node.type === 'string');
  const previous = hosts(); const baseline = commits;
  for (let index = 0; index < 100; index += 1) await act(async () => { send(`rf:event:software-diagnostic-${index}`); });
  expect(commits).toBe(baseline);
  expect(hosts()).toEqual(previous);
  await act(async () => { fireEvent.press(screen.getByTestId('rf-adapter-transition-cancel')); finishFlow(); await jest.runAllTimersAsync(); });
  expect(screen.getByTestId('rf-adapter-transition-result').props.children).toBe('Failed');
  expect(screen.getByTestId('rf-adapter-transition-failure').props.children).toContain('cancelled');
  expect(on.close).toHaveBeenCalledTimes(1);
});

test('real flow is passed as a dependency, not imported back into the scenario panel', () => {
  expect(typeof runRfLabFlow).toBe('function');
  const source = readFileSync(resolve(__dirname, 'rf_adapter_transition.tsx'), 'utf8');
  expect(source).not.toMatch(/from ['"]\.\/App['"]/);
});

test('Android native source contract: dedicated identity, one canonical ACTION_VIEW, record before Linking, no reload (not native execution)', () => {
  const source = readFileSync(resolve(__dirname, '../android/app/src/main/java/com/blehubnitrohost/MainActivity.kt'), 'utf8');
  const record = source.slice(source.indexOf('private fun adapterRecord('), source.indexOf('private fun failFull('));
  expect([...record.matchAll(/put\("([^"]+)"/g)].map((match) => match[1]).sort())
    .toEqual(['run_id', 'pid', 'boot_id', 'bundle_id', 'executable', 'source', 'url', 'adapter_mode'].sort());
  expect(record).toContain('put("boot_id", recoveryBootId)');
  expect(record).toContain('put("source", if (url == null) "launch" else "payload-url")');
  expect(record).toContain('if (url != null) put("url", url)');
  expect(record).toContain('rf-adapter-transition:native:$kind:$record');
  const handler = source.slice(source.indexOf('val adapter = adapterRunId'), source.indexOf('val full = fullRecovery'));
  expect(handler).toContain('adapterFailed || adapterSignalled || intent.action != Intent.ACTION_VIEW');
  expect(handler).toContain('url != onUrl');
  expect(handler).toContain('active-loss/arm');
  expect(handler).toContain('active-loss/on');
  expect(handler).toContain('adapterMode == "active-loss" && !adapterArmed');
  expect(handler).toContain('intent.getBooleanExtra(RELOAD_EXTRA, false)');
  expect(handler.indexOf('adapterSignalled = true')).toBeLessThan(handler.indexOf('adapterRecord(if (arm)'));
  expect(handler.indexOf('adapterRecord(if (arm)')).toBeLessThan(handler.indexOf('super.onNewIntent(intent)'));
  expect(handler).not.toMatch(/setIntent\(|reload\(/);
  expect(source).toContain('intent.getStringExtra("blehub.rfAdapterRunId")');
  expect(source).toContain('props.putString("rfAdapterRunId", runId)');
  expect(source).toContain('adapterRecord("bootstrap")');
  expect(source).toContain('rf-adapter-transition:failed:native:$reason');
  expect(source).toContain('props.putString("rfAdapterMode", adapterMode)');
});

function activeSoftwareRun(options: {
  policy?: 'connection' | 'gatt' | 'timeout' | 'ok' | 'missing' | 'duplicate' | 'wrong-route';
  dirtyResources?: boolean; rejectQueued?: boolean; late?: boolean; badArm?: boolean;
  stopCommandId?: string | null;
  extendedGatt?: boolean; badGatt?: boolean; duplicateCanonical?: boolean; wideIds?: boolean; errorText?: string;
} = {}) {
  const props = { ...profile(), rfAdapterMode: 'active-loss' as const };
  const old = softwareIoProbe();
  if (options.extendedGatt) {
    const readable = (id: string, uuid: string): typeof old.gattServices[number]['characteristics'][number] => ({
      id, uuid: `0000${uuid}-0000-1000-8000-00805f9b34fb`, readable: true,
      writableWithResponse: false, writableWithoutResponse: false, notifiable: false, indicatable: false, descriptors: [],
    });
    // The production Android boundary: canonical + GAP + GATT + Device Information.
    old.gattServices.push(
      { id: '60', uuid: '00001800-0000-1000-8000-00805f9b34fb', primary: true,
        characteristics: [readable('100', '2a00'), readable('101', '2a01'), readable('106', '2a04')] },
      { id: '61', uuid: '00001801-0000-1000-8000-00805f9b34fb', primary: true,
        characteristics: [{ ...readable('102', '2a05'), readable: false, indicatable: true,
          descriptors: [{ id: '200', uuid: '00002902-0000-1000-8000-00805f9b34fb' }] },
          readable('103', '2b29'), readable('104', '2b2a'), readable('105', '2b3a')] },
      { id: '62', uuid: '0000180a-0000-1000-8000-00805f9b34fb', primary: true,
        characteristics: ['2a29', '2a24', '2a25', '2a27', '2a26', '2a28', '2a23', '2a50', '2a2a'].map((uuid, i) => readable(String(110 + i), uuid)) },
    );
  }
  if (options.badGatt) old.gattServices[0].characteristics[0].readable = false;
  if (options.duplicateCanonical) old.gattServices.push({ ...old.gattServices[0], id: '999' });
  if (options.wideIds) {
    let next = BigInt('18446744073709551615');
    for (const service of old.gattServices) {
      service.id = String(next--);
      for (const characteristic of service.characteristics) {
        characteristic.id = String(next--);
        for (const descriptor of characteristic.descriptors) descriptor.id = String(next--);
      }
    }
  }
  let closed = false;
  old.readRfCharacteristic = jest.fn((_id: string) => undefined) as typeof old.readRfCharacteristic;
  if (options.rejectQueued) old.readRfCharacteristic = jest.fn((id: string) => id === '32009' ? { category: 'invalid_state' } : undefined) as unknown as typeof old.readRfCharacteristic;
  if (options.stopCommandId !== undefined) {
    old.stopRfScan = (commandId, scanSessionId) => {
      old.emit({ kind: 'scanStopped', scanSessionId, commandId: options.stopCommandId ?? undefined });
      old.emit(terminal(commandId));
    };
  }
  old.close = jest.fn(async () => { closed = true; });
  old.snapshotRfResources = jest.fn(async () => closed ? zero() : ({
    ...zero(), ingressRoots: 1, adapterDelegates: 1, backendLeases: 1, hubRoots: 1,
    handoffRoots: 1, listeners: 1, lifecycleOwners: 1, workerThreads: 2, peerCacheEntries: 1,
    pendingIo: options.dirtyResources ? 1 : 0,
  }));
  const on = offProbe();
  const probes = [asProbe(old), asProbe(on)];
  const factory = jest.fn(() => probes.shift()!);
  const lines: string[] = [];
  const fault = (): void => {
    if (options.policy === 'missing') return;
    const category = options.policy ?? 'connection';
    const error = (kind: string) => ({ category: kind, message: options.errorText ?? 'synthetic Android error', platformDetails: { platform: 'platform_android', name: 'BluetoothGatt', code: '133' } });
    old.emit({ ...terminal('32008'), error: category === 'ok' ? undefined : error(category) } as RfScanEvent);
    old.emit({ ...terminal('32009'), error: error('connection') } as RfScanEvent);
    old.emit({ kind: 'connectionStateChanged', connectionId: options.policy === 'wrong-route' ? '999' : '32005', error: error('connection') } as RfScanEvent);
    if (options.policy === 'duplicate') old.emit({ ...terminal('32008'), error: error('connection') } as RfScanEvent);
  };
  const report = (line: string): void => {
    lines.push(line);
    if (line.startsWith(`${P}:active:discovery-ready:`)) signal(`blehub-rn-spike://adapter/${props.rfAdapterRunId}/active-loss/${options.badArm ? 'arm?foreign' : 'arm'}`);
    if (line === `${P}:active:accepted:32009`) setTimeout(fault, 0);
    if (line === `${P}:awaiting-on:runId=${props.rfAdapterRunId}`) signal(`blehub-rn-spike://adapter/${props.rfAdapterRunId}/active-loss/on`);
  };
  const flow: AdapterTransitionFlow = async (_mode, _probe, _service, write) => {
    if (options.late) old.emit({ kind: 'characteristicRead', commandId: '32008', connectionId: '32005' });
    write('rf-io:passed:software-only');
  };
  const result = runRfAdapterTransition(factory, flow, props, report, () => false);
  return { result, lines, old, on, factory };
}

describe('explicit active-loss software branch (not RF)', () => {
  test.each([false, true])('production 4/21/3 topology fits one explicit compact audit record, wide IDs=%s', async (wideIds) => {
    const run = activeSoftwareRun({ extendedGatt: true, wideIds });
    const original = JSON.stringify(run.old.gattServices);
    expect(Buffer.byteLength(original, 'utf8')).toBeGreaterThan(4096);
    await jest.runAllTimersAsync();
    expect(await run.result).toBe(true);
    expect(JSON.stringify(run.old.gattServices)).toBe(original);
    const line = run.lines.find((entry) => entry.startsWith(`${P}:active:event:`) && entry.includes('"kind":"gattDiscovered"'))!;
    expect(Buffer.byteLength(`BLEHUB_NITRO_RUNTIME ${line}`, 'utf8')).toBeLessThanOrEqual(3500);
    const audit = JSON.parse(line.slice(`${P}:active:event:`.length));
    expect(audit.auditSchema).toBe('active-gatt-canonical-v1');
    expect(audit.gattSummary).toEqual({ serviceCount: 4, characteristicCount: 21, descriptorCount: 3 });
    expect(audit.gattServices).toEqual([run.old.gattServices[0]]);
    expect(audit).toMatchObject({ commandId: '32006', connectionId: '32005', gattSessionId: '32007' });
    const route = JSON.parse(run.lines.find((entry) => entry.startsWith(`${P}:active:discovery-ready:`))!.slice(`${P}:active:discovery-ready:`.length));
    expect(route.characteristicId).toBe(audit.gattServices[0].characteristics[0].id);
  });
  test.each([{ badGatt: true }, { duplicateCanonical: true }])('compact audit cannot hide original topology fault %j', async (fault) => {
    const run = activeSoftwareRun({ extendedGatt: true, ...fault });
    await jest.runAllTimersAsync();
    expect(await run.result).toBe(false);
    expect(run.old.readRfCharacteristic).not.toHaveBeenCalled();
    expect(run.lines.some((line) => line.startsWith(`${P}:active:discovery-ready:`))).toBe(false);
  });
  test('audit limit counts UTF-8 bytes and fails instead of slicing original error bytes', async () => {
    const errorText = '界'.repeat(1300);
    const run = activeSoftwareRun({ errorText });
    await jest.runAllTimersAsync();
    expect(await run.result).toBe(false);
    expect(run.lines.some((line) => line.includes(errorText))).toBe(false);
    expect(run.lines.some((line) => line.includes('3500-byte log budget; no truncated event emitted'))).toBe(true);
    for (const line of run.lines.filter((entry) => entry.startsWith(`${P}:active:event:`))) {
      expect(Buffer.byteLength(`BLEHUB_NITRO_RUNTIME ${line}`, 'utf8')).toBeLessThanOrEqual(3500);
      expect(() => JSON.parse(line.slice(`${P}:active:event:`.length))).not.toThrow();
    }
  });
  test.each(['connection', 'gatt'] as const)('%s-first settles before close and uses one fresh recovery Probe', async (policy) => {
    const run = activeSoftwareRun({ policy });
    await jest.runAllTimersAsync();
    expect(await run.result).toBe(true);
    expect(run.lines).toContain(`${P}:active:settled:${policy === 'connection' ? 'disconnect-first' : 'read-error-first'}`);
    expect(run.lines).toContain(`${P}:active:silence:passed`);
    expect(run.factory).toHaveBeenCalledTimes(2);
    expect(run.old.snapshotRfResources.mock.invocationCallOrder[0]).toBeLessThan(run.old.close.mock.invocationCallOrder[0]);
    expect(run.old.close).toHaveBeenCalledTimes(1);
    expect(run.on.close).toHaveBeenCalledTimes(1);
    expect(run.lines.filter((s) => /:(off|on):resources:/.test(s))).toHaveLength(2);
  });
  test.each([null, '999'])('Requested scanStopped requires commandId=32003, not %s', async (stopCommandId) => {
    const run = activeSoftwareRun({ stopCommandId });
    await jest.runAllTimersAsync();
    expect(await run.result).toBe(false);
    expect(run.factory).toHaveBeenCalledTimes(1);
    expect(run.old.readRfCharacteristic).not.toHaveBeenCalled();
  });
  test.each(['timeout', 'ok', 'missing', 'duplicate', 'wrong-route'] as const)('%s is failed and cannot become radio-loss recovery', async (policy) => {
    const run = activeSoftwareRun({ policy });
    await jest.runAllTimersAsync();
    expect(await run.result).toBe(false);
    expect(run.factory).toHaveBeenCalledTimes(1);
    expect(run.lines.some((s) => s.startsWith(`${P}:passed:`))).toBe(false);
  });
  test.each([{ dirtyResources: true }, { rejectQueued: true }, { badArm: true }])('refuses unsafe boundary %j', async (options) => {
    const run = activeSoftwareRun(options);
    await jest.runAllTimersAsync();
    expect(await run.result).toBe(false);
    expect(run.factory).toHaveBeenCalledTimes(1);
  });
  test('old active callback invalidates fresh recovery rather than disappearing', async () => {
    const run = activeSoftwareRun({ late: true });
    await jest.runAllTimersAsync();
    expect(await run.result).toBe(false);
    expect(run.lines.some((s) => s.includes('old active callback'))).toBe(true);
  });
  test('component forwards reports to coordinated session presenter after console output', async () => {
    const props = profile();
    jest.mocked(createBleHubProbe).mockReturnValue(asProbe(offProbe()));
    render(<RunSessionProvider value={{ report: mockPresent, begin: jest.fn(), update: jest.fn() }}><RfAdapterTransition {...props} runFlow={passingFlow} /></RunSessionProvider>);
    await act(async () => { await jest.advanceTimersByTimeAsync(10); });
    expect(mockPresent).toHaveBeenCalledWith(expect.stringContaining(`${P}:runtime:`));
    expect(jest.mocked(console.log).mock.invocationCallOrder[0]).toBeLessThan(mockPresent.mock.invocationCallOrder[0]);
  });
});
