/// <reference types="node" />
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';
import React from 'react';
import { Platform } from 'react-native';
import { act, fireEvent, render, screen } from '@testing-library/react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import type { RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import App, { RF_LAB_MODES } from './App';
import { CCC_DISABLE_REJECT_STEPS, runRfCccDisableReject } from './rf_ccc_disable_reject';
import { fixture, execute, setupCccRejectTests, type Fault } from './rf_ccc_reject.test_support';
import { RF_RESOURCE_FIELDS } from './rf_recovery_ten';
import { RF_BOOT_NONCE } from './rf_runtime';
import { findScenario } from './rf_lab_catalog';
import { projectRunLine } from './rf_lab_session';

jest.mock('@blehub/react-native-internal', () => ({ createBleHubProbe: jest.fn() }));
jest.mock('./multi_rf', () => ({ runRfMulti: jest.fn(), runRfMultiSoak: jest.fn() }));
jest.mock('./multi_fault', () => ({ runRfMultiFault: jest.fn() }));
const PREFIX = 'rf-ccc-disable-reject';
const SERVICE = 'fd600000-0000-4000-8000-000000000000';
const pairs = [['baseline-enabled', 'baseline-value-sent'], ['arm', 'armed'], ['disable-dispatched', 'held'],
  ['reject', 'rejected'], ['reject-observed', 'continued-value-sent'], ['continued-notified', 'retry-allowed'],
  ['disabled', 'silence-started'], ['silence-done', 'close-allowed']];
const events = (lines: string[]): RfScanEvent[] => lines.filter((line) => line.startsWith(`${PREFIX}:event:`))
  .map((line) => JSON.parse(line.slice(`${PREFIX}:event:`.length)));
setupCccRejectTests();

test.each([[true, false], [false, false], [true, true], [false, true]])('original subscription survives rejected disable, Android=%s/value-after-ACK=%s', async (android, valueAfterAck) => {
  jest.replaceProperty(Platform, 'OS', android ? 'android' : 'ios');
  const f = fixture(android, undefined, valueAfterAck, true);
  expect(await execute(f)).toBeUndefined();
  expect(f.probe.setRfNotifications.mock.calls).toEqual([
    ['34010', '34004', '34007', '34009', '31', true, 'platform_default'],
    ['34011', '34004', '34007', '34009', '31', false, 'platform_default'],
    ['34013', '34004', '34007', '34009', '31', false, 'platform_default'],
  ]);
  expect(f.probe.connectRfPeripheral).toHaveBeenCalledWith('34005', '34004', '0');
  expect(f.probe.readRfCharacteristic.mock.calls).toEqual(['34016', '34012', '34014', '34015'].map((id) => [id, '34004', '34007', '30']));
  expect(f.lines.filter((line) => line.startsWith(`${PREFIX}:step:`)).map((line) => line.split(':')[2])).toEqual(CCC_DISABLE_REJECT_STEPS);
  expect(f.lines.find((line) => line.startsWith(`${PREFIX}:step:gatt:`))).toContain('characteristicId=31:readCharacteristicId=30');
  expect(f.lines.filter((line) => line.startsWith('rf-control:ready:')).map((line) => line.split(':phase=')[1])).toEqual(pairs.map(([request]) => request));
  expect(f.lines.filter((line) => line.startsWith('rf-control:observed:')).map((line) => line.split(':phase=')[1])).toEqual(pairs.map(([, phase]) => phase));
  const all = events(f.lines);
  expect(all.filter((event) => event.kind === 'notificationReceived')).toMatchObject([
    { subscriptionId: '34009', value: [0xef] }, { subscriptionId: '34009', value: [0xe1] },
  ]);
  expect(all.filter((event) => event.commandId === '34011')).toMatchObject([{ kind: 'commandCompleted', error: {
    category: 'gatt', platformDetails: { platform: android ? 'platform_android' : 'platform_apple', name: android ? 'BluetoothGatt' : 'CBATTErrorDomain', code: '14' },
  } }]);
  expect(all.findIndex((event) => event.commandId === '34011')).toBeLessThan(all.findIndex((event) => event.commandId === '34012'));
  for (const [index, request] of ['baseline-enabled', 'reject-observed'].entries()) {
    const valueIndex = f.lines.findIndex((line) => line.includes(`"value":[${index === 0 ? 239 : 225}]`));
    const ready = f.lines.findIndex((line) => line.startsWith('rf-control:ready:') && line.endsWith(`phase=${request}`));
    const ack = f.lines.findIndex((line) => line.startsWith('rf-control:observed:') && line.endsWith(`phase=${pairs[index === 0 ? 0 : 4][1]}`));
    expect(ready).toBeLessThan(valueIndex);
    expect(valueAfterAck ? ack < valueIndex : valueIndex < ack).toBe(true);
  }
  const warm = JSON.parse(f.lines.find((line) => line.includes(':resources:phase=failed-disable:'))!.split(':snapshot=')[1]);
  expect(warm).toMatchObject({ connections: 1, subscriptions: 1, pendingIo: 0, watchdogs: 0, callbackRoutes: 5 });
  expect(Number(f.lines.find((line) => line.includes(':step:silence:'))!.split('elapsedMs=')[1])).toBeGreaterThanOrEqual(1000);
  expect(Object.values(JSON.parse(f.lines.find((line) => line.includes(':resources:phase=closed:'))!.split(':snapshot=')[1]))).toEqual(Array(17).fill(0));
  expect(f.lines.slice(-3)).toEqual([`${PREFIX}:step:close:passed:session=${f.control.session}`, `${PREFIX}:runtime:session=${f.control.session}:nonce=${RF_BOOT_NONCE}:phase=close`, `${PREFIX}:passed`]);
});

test.each([[true, false], [false, false], [true, true], [false, true]])('actual disable executor crosses actual provider complete=True (Android=%s, value-after-ACK=%s)', async (android, valueAfterAck) => {
  jest.replaceProperty(Platform, 'OS', android ? 'android' : 'ios');
  const f = fixture(android, undefined, valueAfterAck, true);

  expect(await execute(f)).toBeUndefined();
  const text = f.lines.map((line) => `BLEHUB_NITRO_RUNTIME ${line}`).join('\n');
  const value = f.lines.find((line) => line.includes('"kind":"notificationReceived"') && line.includes('"value":[225]'))!;
  const queuedRead = f.lines.find((line) => line.includes('"kind":"characteristicRead"') && line.includes('"commandId":"34012"'))!;
  const cases = { actual: text,
    'old-duration': text.replace('elapsedMs=', 'durationMs='),
    'missing-scan-command': text.replace('step:scan:passed:commandId=34003:', 'step:scan:passed:'),
    'foreign-read-route': text.replace(/(step:queued-read:passed:[^\n]*characteristicId=)30/, (_match, route: string) => `${route}999`),
    'foreign-subscription': text.replace(value, value.replace('"subscriptionId":"34009"', '"subscriptionId":"34017"')),
    'continued-ef': text.replace(value, value.replace('"value":[225]', '"value":[239]')),
    'duplicate-value': text.replace(value, `${value}\nBLEHUB_NITRO_RUNTIME ${value}`),
    'missing-read-domain': text.replace(`BLEHUB_NITRO_RUNTIME ${queuedRead}\n`, ''),
    'wrong-enable-mode': text.replace('"actualNotificationMode":"Notification"', '"actualNotificationMode":"Indication"'),
    'wrong-flow': text.replace(':flow=12:', ':flow=11:'),
    'missing-warm': text.split('\n').filter((line) => !line.includes(':resources:phase=failed-disable:')).join('\n'),
    'missing-closed': text.split('\n').filter((line) => !line.includes(':resources:phase=closed:')).join('\n'),
  };
  const result = spawnSync('pixi', ['run', 'python', '-c', `
import json, sys
from tests.peripherals.nrf52840.ccc_disable_reject import runtime_facts
value = json.load(sys.stdin)
results = {}
for name, text in value["cases"].items():
    try:
        facts = runtime_facts(text, run_id=value["run_id"], platform=value["platform"], complete=True)
    except ValueError as error:
        results[name] = {"accepted": False, "error": str(error)}
    else:
        results[name] = {"accepted": True, "facts": facts}
print(json.dumps(results))
`], { cwd: resolve(__dirname, '../../../..'), input: JSON.stringify({ cases, run_id: f.control.session, platform: android ? 'android' : 'ios' }), encoding: 'utf8', timeout: 60_000, maxBuffer: 2 * 1024 * 1024 });
  expect({ error: result.error?.message, status: result.status, stderr: result.stderr }).toEqual({ error: undefined, status: 0, stderr: '' });
  const parsed = JSON.parse(result.stdout);
  expect(parsed.actual.error).toBeUndefined();
  expect(parsed.actual).toMatchObject({ accepted: true, facts: { passed: true, closed: true, run_id: f.control.session, nonce: RF_BOOT_NONCE } });
  expect(Object.keys(parsed.actual.facts.steps)).toEqual(CCC_DISABLE_REJECT_STEPS);
  for (const name of Object.keys(cases).filter((name) => name !== 'actual')) expect({ name, accepted: parsed[name].accepted }).toEqual({ name, accepted: false });
});

test.each<Fault>(['code', 'platform', 'name', 'timeout', 'success', 'duplicate-terminal', 'failed-state', 'failed-value', 'early-terminal', 'early-read',
  'queued-before-terminal', 'queued-route', 'read-value', 'enable-route', 'enable-mode', 'enable-state', 'duplicate-state', 'missing-state', 'value-before-ready',
  'value-route', 'value-sub', 'value-byte', 'duplicate-value', 'disable-state', 'silence-value', 'late-value', 'unknown', 'warm-connections', 'warm-subscriptions',
  'warm-pendingIo', 'warm-watchdogs', 'close', 'closed-nonzero', 'close-event', 'snapshot', 'remove', 'missing-held', 'late-reject', 'dispatch', 'topology', 'descriptor',
  'overflow', 'duplicate-disconnecting', 'baseline-duplicate', 'continued-before-ready', 'continued-ef', 'duplicate-read', 'duplicate-read-terminal',
  'duplicate-control', 'foreign-control', 'early-control'])('%s cannot become a disable App pass', async (fault) => {
  const f = fixture(true, fault, false, true);
  expect(await execute(f)).toBeInstanceOf(Error);
  expect(f.lines.some((line) => line.startsWith(`${PREFIX}:failed:`))).toBe(true);
  expect(f.lines).not.toContain(`${PREFIX}:passed`);
  expect(f.lines.some((line) => line.includes(':step:close:') || line.endsWith(':phase=close'))).toBe(false);
  expect(f.probe.close).toHaveBeenCalledTimes(1);
  expect(f.probe.removeRfScanListener).toHaveBeenCalledTimes(1);
});

test.each([undefined, 'Multifunction', 'wrong-name', 'wrong-service'])('service required; advertisement name optional but never wrong: %s', async (name) => {
  const f = fixture(true, undefined, false, true);
  f.probe.startRfScan.mockImplementation((id, scanSessionId) => {
    f.emit({ kind: 'scanStarted', commandId: id, scanSessionId }); f.terminal(id);
    f.emit({ kind: 'advertisementObserved', scanSessionId, peripheralId: '0', localName: name === 'wrong-service' ? undefined : name,
      advertisedServices: [name === 'wrong-service' ? 'fd700000-0000-4000-8000-000000000000' : SERVICE] });
  });
  expect(await execute(f)).toEqual(name?.startsWith('wrong') ? expect.any(Error) : undefined);
  expect(f.probe.connectRfPeripheral).toHaveBeenCalledTimes(name?.startsWith('wrong') ? 0 : 1);
});

test('target GATT retains the complete target only under the full UTF-8 ceiling; missing launch name is allowed', async () => {
  const f = fixture(true, undefined, false, true);
  delete f.profile.rfExpectedLocalName;
  for (let i = 0; i < 80; i++) f.gattServices.push({ id: String(100 + i), uuid: '00001800-0000-1000-8000-00805f9b34fb', primary: true, characteristics: [] });
  expect(await execute(f)).toBeUndefined();
  const line = f.lines.find((entry) => entry.includes('"kind":"gattDiscovered"'))!;
  expect(JSON.parse(line.slice(`${PREFIX}:event:`.length)).gattServices).toEqual([f.gattServices[0]]);
  expect(Buffer.byteLength(`BLEHUB_NITRO_RUNTIME ${line}`, 'utf8')).toBeLessThan(4000);
});

test('opaque notification characteristic ID zero is valid, not a missing route', async () => {
  const f = fixture(true, undefined, false, true);
  f.route.characteristicId = '0';
  f.gattServices[0].characteristics[1].id = '0';
  expect(await execute(f)).toBeUndefined();
  expect(f.probe.setRfNotifications.mock.calls.every((call) => call[4] === '0')).toBe(true);
  expect(events(f.lines).filter((event) => event.kind === 'notificationReceived')).toMatchObject([
    { characteristicId: '0', subscriptionId: '34009' }, { characteristicId: '0', subscriptionId: '34009' },
  ]);
});

test('close resolves and all 17 resources are zero before final markers', async () => {
  const f = fixture(true, undefined, false, true);
  let release!: () => void;
  f.probe.close.mockImplementationOnce(() => new Promise<void>((resolveClose) => { release = resolveClose; }));
  const result = runRfCccDisableReject(f.native, f.profile, f.report, () => false).catch((error: unknown) => error);
  await jest.runAllTimersAsync();
  expect(f.lines.some((line) => line.startsWith(`${PREFIX}:step:disconnected:`))).toBe(true);
  expect(f.lines.some((line) => line.includes(':step:close:') || line.endsWith(':phase=close'))).toBe(false);
  f.probe.snapshotRfResources.mockImplementation(async () => Object.fromEntries(RF_RESOURCE_FIELDS.map((key) => [key, 0])));
  release(); await jest.runAllTimersAsync(); expect(await result).toBeUndefined();
});

test('same App keeps flow12 mounted while browsing; not a full-suite flow or invented recipe', async () => {
  const f = fixture(true, undefined, false, true);
  jest.mocked(createBleHubProbe).mockReturnValue(f.native);
  jest.spyOn(console, 'log').mockImplementation((message: unknown) => { if (typeof message === 'string') f.report(message.replace(/^BLEHUB_NITRO_RUNTIME /, '')); });
  render(<App runtimeMode={PREFIX} {...f.profile} />);
  await act(async () => { await jest.advanceTimersByTimeAsync(100); });
  for (const view of ['home', 'evidence', 'current']) fireEvent.press(screen.getByTestId(`rf-workbench-${view}`));
  await act(async () => { await jest.runAllTimersAsync(); });
  expect(createBleHubProbe).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId(`${PREFIX}-status`).props.children).toBe('passed');
  expect(findScenario(PREFIX, { fullSuite: true })).toMatchObject({ id: PREFIX, recipes: {} });
  expect(RF_LAB_MODES).not.toContain(PREFIX);
  expect(projectRunLine(PREFIX, false, `${PREFIX}:event:{"error":{"category":"gatt"}}`)).toBeUndefined();
  expect(projectRunLine(PREFIX, false, `${PREFIX}:step:disable-rejected:passed:commandId=34011`)).toMatchObject({ state: 'running', completed: 10, total: 18 });
  expect(projectRunLine('rf-lab-suite', true, `${PREFIX}:passed`)).toBeUndefined();
});

test('invalid launch makes no Probe and a consumed session/token is not replayable', async () => {
  const f = fixture(true, undefined, false, true);
  jest.spyOn(console, 'log').mockImplementation(() => {});
  render(<App runtimeMode={PREFIX} {...f.profile} rfCccDisableRejectControl={JSON.stringify({ ...f.control, flow: 11 })} />);
  await act(async () => { await jest.runAllTimersAsync(); });
  expect(createBleHubProbe).not.toHaveBeenCalled();
  expect(screen.getByTestId(`${PREFIX}-status`).props.children).toBe('failed');
  expect(await execute(f)).toBeUndefined();
  const repeated = fixture(true, undefined, false, true);
  repeated.profile.rfCccDisableRejectControl = f.profile.rfCccDisableRejectControl;
  expect(await execute(repeated)).toBeInstanceOf(Error);
  expect(repeated.probe.initialize).not.toHaveBeenCalled();
});

test('native flow12 ACK precedes Linking and the shared narrow gate belongs to App and native tests', () => {
  const apple = readFileSync(resolve(__dirname, '../ios/BleHubNitroHost/AppDelegate.swift'), 'utf8');
  const handler = apple.slice(apple.indexOf('fileprivate func deliverRfControl('), apple.indexOf('private func installRfControl('));
  expect(handler.indexOf('cccDisableRejectControl!.accept(uri.absoluteString)')).toBeGreaterThan(-1);
  expect(handler.indexOf('cccDisableRejectControl!.accept(uri.absoluteString)')).toBeLessThan(handler.indexOf('DispatchQueue.main.async'));
  expect(handler.indexOf('self.writeRfHostRecord("signal", phase: phase, identity: identity)')).toBeLessThan(handler.indexOf('guard RCTLinkingManager.application'));
  for (const suffix of ['service', 'control']) expect(apple).toContain(`--blehub-nitro-rf-ccc-disable-reject-${suffix}`);
  const project = readFileSync(resolve(__dirname, '../ios/BleHubNitroHost.xcodeproj/project.pbxproj'), 'utf8');
  expect(project.match(/NarrowRejectControlGate.swift in Sources.*isa = PBXBuildFile/g)).toHaveLength(2);
  const android = readFileSync(resolve(__dirname, '../android/app/src/main/java/com/blehubnitrohost/MainActivity.kt'), 'utf8');
  const intent = android.slice(android.indexOf('override fun onNewIntent'), android.indexOf('val permission = permissionRunId'));
  expect(intent).toContain('(cccDisableReject ?: cccEnableReject ?: writeReject)');
  expect(intent.indexOf('rf-host:signal:')).toBeLessThan(intent.indexOf('super.onNewIntent(intent)'));
  expect(android).toContain('blehub.rfCccDisableRejectControl');
  expect(android).toContain('props.putString("rfCccDisableRejectControl", cccDisableRejectRaw)');
});
