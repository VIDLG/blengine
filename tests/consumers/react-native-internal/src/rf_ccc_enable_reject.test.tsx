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
import { CCC_REJECT_STEPS, runRfCccEnableReject } from './rf_ccc_enable_reject';
import { fixture, execute, setupCccRejectTests, type Fault } from './rf_ccc_reject.test_support';
import { RF_RESOURCE_FIELDS } from './rf_recovery_ten';
import { RF_BOOT_NONCE } from './rf_runtime';
import { findScenario } from './rf_lab_catalog';
import { projectRunLine } from './rf_lab_session';

jest.mock('@blehub/react-native-internal', () => ({ createBleHubProbe: jest.fn() }));
jest.mock('./multi_rf', () => ({ runRfMulti: jest.fn(), runRfMultiSoak: jest.fn() }));
jest.mock('./multi_fault', () => ({ runRfMultiFault: jest.fn() }));
const PREFIX = 'rf-ccc-enable-reject';
const SERVICE = 'fd600000-0000-4000-8000-000000000000';
setupCccRejectTests();

test.each([[true, false], [false, false], [true, true], [false, true]])('same connection recovers with Notification, ACK/value race=%s/%s', async (android, valueAfterAck) => {
  jest.replaceProperty(Platform, 'OS', android ? 'android' : 'ios');
  const f = fixture(android, undefined, valueAfterAck);
  expect(await execute(f)).toBeUndefined();
  expect(f.probe.connectRfPeripheral).toHaveBeenCalledWith('33005', '33004', '0');
  expect(f.probe.setRfNotifications.mock.calls).toEqual([
    ['33010', '33004', '33007', '33009', '31', true, 'platform_default'],
    ['33013', '33004', '33007', '33012', '31', true, 'platform_default'],
    ['33014', '33004', '33007', '33012', '31', false, 'platform_default'],
  ]);
  expect(f.probe.readRfCharacteristic.mock.calls).toEqual(['33017', '33011', '33015', '33016'].map((id) => [id, '33004', '33007', '30']));
  expect(f.lines.filter((line) => line.startsWith(`${PREFIX}:step:`)).map((line) => line.split(':')[2])).toEqual(CCC_REJECT_STEPS);
  expect(f.lines.find((line) => line.startsWith(`${PREFIX}:step:gatt:`))).toContain('characteristicId=31:readCharacteristicId=30');
  const requests = f.lines.filter((line) => line.startsWith('rf-control:ready:')).map((line) => line.split(':phase=')[1]);
  expect(requests).toEqual(['arm', 'enable-dispatched', 'reject', 'recovery-enabled', 'recovery-notified', 'disabled', 'silence-done']);
  const events = f.lines.filter((line) => line.startsWith(`${PREFIX}:event:`)).map((line) => JSON.parse(line.slice(`${PREFIX}:event:`.length)));
  expect(events.every((event) => event.runId === f.control.session && event.nonce === RF_BOOT_NONCE)).toBe(true);
  expect(events.findIndex((event) => event.commandId === '33010')).toBeLessThan(events.findIndex((event) => event.commandId === '33011'));
  expect(events.find((event) => event.commandId === '33010').error.platformDetails).toEqual({ platform: android ? 'platform_android' : 'platform_apple', name: android ? 'BluetoothGatt' : 'CBATTErrorDomain', code: '14', message: 'full native error' });
  expect(events.filter((event) => event.kind === 'notificationReceived')).toMatchObject([{ subscriptionId: '33012', value: [0xef] }]);
  const valueIndex = f.lines.findIndex((line) => line.includes('"kind":"notificationReceived"'));
  expect(f.lines.findIndex((line) => line.startsWith('rf-control:ready:') && line.endsWith('phase=recovery-enabled'))).toBeLessThan(valueIndex);
  const ack = f.lines.findIndex((line) => line.startsWith('rf-control:observed:') && line.endsWith('phase=value-sent'));
  expect(valueAfterAck ? ack < valueIndex : valueIndex < ack).toBe(true);
  const silence = f.lines.find((line) => line.startsWith(`${PREFIX}:step:silence:`))!;
  expect(Number(silence.split('elapsedMs=')[1])).toBeGreaterThanOrEqual(1000);
    expect(silence).not.toContain('durationMs=');
    expect(f.lines.find((line) => line.startsWith(`${PREFIX}:step:scan:`))).toContain('commandId=33003:scanSessionId=33001');
  expect(f.lines.find((line) => line.includes(':resources:phase=failed-enable:'))).toContain('"callbackRoutes":5');
  const closed = f.lines.find((line) => line.includes(':resources:phase=closed:'))!;
  expect(Object.values(JSON.parse(closed.split(':snapshot=')[1]))).toEqual(Array(17).fill(0));
  expect(f.lines.slice(-3)).toEqual([`${PREFIX}:step:close:passed:session=${f.control.session}`, `${PREFIX}:runtime:session=${f.control.session}:nonce=${RF_BOOT_NONCE}:phase=close`, `${PREFIX}:passed`]);
  expect(f.probe.close).toHaveBeenCalledTimes(1);
});

test.each([[true, false], [false, false], [true, true], [false, true]])('actual executor transcript crosses the real provider complete=True parser (Android=%s, valueAfterAck=%s)', async (android, valueAfterAck) => {
  jest.replaceProperty(Platform, 'OS', android ? 'android' : 'ios');
  const f = fixture(android, undefined, valueAfterAck);
  expect(await execute(f)).toBeUndefined();
  const text = f.lines.map((line) => `BLEHUB_NITRO_RUNTIME ${line}`).join('\n');
  const notification = f.lines.find((line) => line.includes('"kind":"notificationReceived"'))!;
  const queuedRead = f.lines.find((line) => line.includes('"kind":"characteristicRead"') && line.includes('"commandId":"33011"'))!;
  // Mutate only output from the real executor: no second hand-authored happy-path transcript.
  const cases = {
    actual: text,
    'old-duration-field': text.replace('elapsedMs=', 'durationMs='),
        'missing-scan-command': text.replace('step:scan:passed:commandId=33003:', 'step:scan:passed:'),
    'wrong-read-step-route': text.replace(/(step:queued-read:passed:[^\n]*characteristicId=)30/, (_match, route: string) => `${route}31`),
    'failed-subscription-value': text.replace(notification, notification.replace('"subscriptionId":"33012"', '"subscriptionId":"33009"')),
    'duplicate-value-domain': text.replace(notification, `${notification}\nBLEHUB_NITRO_RUNTIME ${notification}`),
    'missing-queued-read-domain': text.replace(`BLEHUB_NITRO_RUNTIME ${queuedRead}\n`, ''),
    'wrong-enable-mode': text.replace('"actualNotificationMode":"Notification"', '"actualNotificationMode":"Indication"'),
    'wrong-control-flow': text.replace(':flow=11:', ':flow=10:'),
    'missing-closed-resources': text.split('\n').filter((line) => !line.includes(':resources:phase=closed:')).join('\n'),
  };
  // A test-only subprocess imports the actual live parser, never the reverse. Pixi supplies Python.
  const result = spawnSync('pixi', ['run', 'python', '-c', `
import json
import sys
from tests.peripherals.nrf52840.ccc_enable_reject import runtime_facts
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
`], { cwd: resolve(__dirname, '../../../..'), input: JSON.stringify({ cases, run_id: f.control.session, platform: android ? 'android' : 'ios' }),
    encoding: 'utf8', timeout: 60_000, maxBuffer: 2 * 1024 * 1024 });
  expect({ error: result.error?.message, status: result.status, stderr: result.stderr }).toEqual({ error: undefined, status: 0, stderr: '' });
  const parsed = JSON.parse(result.stdout);
  expect(parsed.actual.error).toBeUndefined();
  expect(parsed.actual).toMatchObject({ accepted: true, facts: { passed: true, closed: true, run_id: f.control.session, nonce: RF_BOOT_NONCE } });
  expect(Object.keys(parsed.actual.facts.steps)).toEqual(CCC_REJECT_STEPS);
  expect(Number(parsed.actual.facts.steps.silence.elapsedMs)).toBeGreaterThanOrEqual(1000);
  expect(parsed.actual.facts.events.filter((event: RfScanEvent) => event.kind === 'notificationReceived')).toMatchObject([{ subscriptionId: '33012', value: [0xef] }]);
  for (const name of Object.keys(cases).filter((name) => name !== 'actual')) {
    expect({ name, accepted: parsed[name].accepted, error: typeof parsed[name].error }).toEqual({ name, accepted: false, error: 'string' });
  }
});

test.each<Fault>(['code', 'platform', 'name', 'timeout', 'success', 'duplicate-terminal', 'failed-state', 'failed-value', 'early-terminal', 'early-read',
  'queued-before-terminal', 'queued-route', 'read-value', 'enable-route', 'enable-mode', 'enable-state', 'duplicate-state', 'missing-state', 'value-before-ready',
  'value-route', 'value-sub', 'value-byte', 'duplicate-value', 'disable-state', 'silence-value', 'late-value', 'unknown', 'warm-connections', 'warm-subscriptions',
  'warm-pendingIo', 'warm-watchdogs', 'close', 'closed-nonzero', 'close-event', 'snapshot', 'remove', 'missing-held', 'late-reject', 'dispatch', 'topology', 'descriptor', 'overflow', 'duplicate-disconnecting'])('%s cannot become an App pass', async (fault) => {
  const f = fixture(true, fault);
  expect(await execute(f)).toBeInstanceOf(Error);
  expect(f.lines.some((line) => line.startsWith(`${PREFIX}:failed:`))).toBe(true);
  expect(f.lines).not.toContain(`${PREFIX}:passed`);
  expect(f.lines.some((line) => line.startsWith(`${PREFIX}:runtime:`) && line.endsWith('phase=close'))).toBe(false);
  expect(f.probe.close).toHaveBeenCalledTimes(1);
  expect(f.probe.removeRfScanListener).toHaveBeenCalledTimes(1);
});

test.each([undefined, 'Multifunction', 'wrong-name', 'wrong-service'])('scan selection validates frozen identity: %s', async (name) => {
  const f = fixture();
  f.probe.startRfScan.mockImplementation((id, scanSessionId) => {
    f.emit({ kind: 'scanStarted', commandId: id, scanSessionId }); f.terminal(id);
    f.emit({ kind: 'advertisementObserved', scanSessionId, peripheralId: '0', localName: name === 'wrong-service' ? undefined : name,
      advertisedServices: [name === 'wrong-service' ? 'fd700000-0000-4000-8000-000000000000' : SERVICE] });
  });
  const error = await execute(f);
  if (name?.startsWith('wrong')) { expect(error).toBeInstanceOf(Error); expect(f.probe.connectRfPeripheral).not.toHaveBeenCalled(); }
  else { expect(error).toBeUndefined(); expect(f.probe.connectRfPeripheral).toHaveBeenCalledWith('33005', '33004', '0'); }
});

test('complete target-service projection is bounded without evidence from unrelated services', async () => {
  const f = fixture();
  for (let i = 0; i < 80; i++) f.gattServices.push({ id: String(100 + i), uuid: '00001800-0000-1000-8000-00805f9b34fb', primary: true, characteristics: [] });
  expect(await execute(f)).toBeUndefined();
  const line = f.lines.find((entry) => entry.includes('"kind":"gattDiscovered"'))!;
  const event = JSON.parse(line.slice(`${PREFIX}:event:`.length));
  expect(event.gattServices).toEqual([f.gattServices[0]]);
  expect(Buffer.byteLength(`BLEHUB_NITRO_RUNTIME ${line}`, 'utf8')).toBeLessThan(4000);
});

test('close and resource evidence must resolve before close/runtime/pass markers', async () => {
  const f = fixture();
  let release!: () => void;
  f.probe.close.mockImplementationOnce(() => new Promise<void>((resolveClose) => { release = resolveClose; }));
  const result = runRfCccEnableReject(f.native, f.profile, f.report, () => false).catch((error: unknown) => error);
  await jest.runAllTimersAsync();
  expect(f.lines.some((line) => line.startsWith(`${PREFIX}:step:disconnected:`))).toBe(true);
  expect(f.lines.some((line) => line.includes(':step:close:') || line.endsWith(':phase=close'))).toBe(false);
  f.probe.snapshotRfResources.mockImplementation(async () => Object.fromEntries(RF_RESOURCE_FIELDS.map((key) => [key, 0])));
  release(); await jest.runAllTimersAsync(); expect(await result).toBeUndefined();
  expect(f.lines.at(-1)).toBe(`${PREFIX}:passed`);
});

test('same mounted workbench retains the scenario while browsing and projects expected failure as progress', async () => {
  const f = fixture();
  jest.mocked(createBleHubProbe).mockReturnValue(f.native);
  jest.spyOn(console, 'log').mockImplementation((message: unknown) => { if (typeof message === 'string') f.report(message.replace(/^BLEHUB_NITRO_RUNTIME /, '')); });
  render(<App runtimeMode={PREFIX} {...f.profile} />);
  await act(async () => { await jest.advanceTimersByTimeAsync(100); });
  fireEvent.press(screen.getByTestId('rf-workbench-home'));
  fireEvent.press(screen.getByTestId('rf-workbench-evidence'));
  fireEvent.press(screen.getByTestId('rf-workbench-current'));
  await act(async () => { await jest.runAllTimersAsync(); });
  expect(createBleHubProbe).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId('rf-ccc-enable-reject-status').props.children).toBe('passed');
  expect(findScenario(PREFIX, { fullSuite: true })?.id).toBe(PREFIX);
  expect(RF_LAB_MODES).not.toContain(PREFIX);
  expect(projectRunLine(PREFIX, false, `${PREFIX}:event:{"error":{"category":"gatt"}}`)).toBeUndefined();
  expect(projectRunLine(PREFIX, false, `${PREFIX}:step:enable-rejected:passed:commandId=33010`)).toMatchObject({ state: 'running', completed: 8, total: 17 });
  expect(projectRunLine('rf-lab-suite', true, `${PREFIX}:passed`)).toBeUndefined();
});

test('invalid launch constructs no Probe and a consumed session/token cannot be replayed', async () => {
  const f = fixture();
  const log = jest.spyOn(console, 'log').mockImplementation(() => {});
  render(<App runtimeMode={PREFIX} {...f.profile} rfCccEnableRejectControl={JSON.stringify({ ...f.control, flow: 10 })} />);
  await act(async () => { await jest.runAllTimersAsync(); });
  expect(createBleHubProbe).not.toHaveBeenCalled();
  expect(screen.getByTestId('rf-ccc-enable-reject-status').props.children).toBe('failed');
  expect(log).toHaveBeenCalledWith(expect.stringContaining(`${PREFIX}:failed:`));
  expect(await execute(f)).toBeUndefined();
  const repeated = fixture();
  repeated.profile.rfCccEnableRejectControl = f.profile.rfCccEnableRejectControl;
  expect(await execute(repeated)).toBeInstanceOf(Error);
  expect(repeated.probe.initialize).not.toHaveBeenCalled();
  expect(repeated.probe.close).toHaveBeenCalledTimes(1);
  expect(repeated.lines).not.toContain(`${PREFIX}:passed`);
});

test('native source contract publishes CCC acceptance before RN Linking and shares installed/process identity', () => {
  const apple = readFileSync(resolve(__dirname, '../ios/BleHubNitroHost/AppDelegate.swift'), 'utf8');
  const handler = apple.slice(apple.indexOf('fileprivate func deliverRfControl('), apple.indexOf('private func installRfControl('));
  expect(handler.indexOf('cccEnableRejectControl!.accept(uri.absoluteString)')).toBeLessThan(handler.indexOf('DispatchQueue.main.async'));
  expect(handler.indexOf('self.writeRfHostRecord("signal", phase: phase, identity: identity)')).toBeLessThan(handler.indexOf('guard RCTLinkingManager.application'));
  expect(apple).toContain('--blehub-nitro-rf-ccc-enable-reject-service');
  expect(apple).toContain('--blehub-nitro-rf-ccc-enable-reject-control');
  const android = readFileSync(resolve(__dirname, '../android/app/src/main/java/com/blehubnitrohost/MainActivity.kt'), 'utf8');
  const intent = android.slice(android.indexOf('override fun onNewIntent'), android.indexOf('val permission = permissionRunId'));
  expect(intent.indexOf('rf-host:signal:')).toBeLessThan(intent.indexOf('super.onNewIntent(intent)'));
  expect(android).toContain('blehub.rfCccEnableRejectControl');
  expect(android).toContain('props.putString("rfCccEnableRejectControl", cccEnableRejectRaw)');
});
