/// <reference types="node" />
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { Linking } from 'react-native';
import { RecoveryReadyBarrier } from './rf_recovery_ten';

// These are native-source contract checks, not native execution/encoding evidence.
// Keep schema and incoming-URL provenance covered without invoking parent-owned codegen/builds.
const android = readFileSync(resolve(__dirname, '../android/app/src/main/java/com/blehubnitrohost/MainActivity.kt'), 'utf8');
const apple = readFileSync(resolve(__dirname, '../ios/BleHubNitroHost/AppDelegate.swift'), 'utf8');
const baseKeys = ['run_id', 'pid', 'boot_id', 'bundle_id', 'executable', 'source'].sort();
const signalKeys = [...baseKeys, 'cycle', 'url'].sort();
const RUN = '12345678-1234-4234-8234-123456789abc';
const urlFor = (cycle: number): string => `blehub-rn-spike://recover/${RUN}/${cycle}`;
function section(source: string, start: string, end: string): string {
  const begin = source.indexOf(start);
  const finish = source.indexOf(end, begin + start.length);
  expect(begin).toBeGreaterThanOrEqual(0);
  expect(finish).toBeGreaterThan(begin);
  return source.slice(begin, finish);
}

describe('strict recovery native payload source contract', () => {
  test('Android bootstrap has exactly six keys; signal adds only cycle and actual URL', () => {
    const record = section(android, 'private fun recoveryRecord(', 'override fun onNewIntent(');
    const keys = [...record.matchAll(/put\("([^"]+)"/g)].map((match) => match[1]);
    expect(keys.sort()).toEqual(signalKeys);
    const conditional = record.slice(record.indexOf('if (cycle != null)'));
    expect([...conditional.matchAll(/put\("([^"]+)"/g)].map((match) => match[1]).sort()).toEqual(['cycle', 'url']);
    expect(keys.filter((key) => key !== 'cycle' && key !== 'url').sort()).toEqual(baseKeys);
    expect(record).toContain('put("source", if (url == null) "launch" else "payload-url")');
    expect(record).toContain('if (url != null) put("url", url)');
    expect(record).toContain('put("pid", Process.myPid())');
    expect(record).toContain('put("boot_id", recoveryBootId)');
    expect(record).toContain('java.io.File(applicationInfo.sourceDir).toURI().toString()');
    expect(android).toContain('recoveryRecord("bootstrap")');
  });

  test('Android records validated incoming dataString before Linking, without replacing launch intent', () => {
    const handler = section(android, 'val runId = recoveryRunId', '// Keep RN Linking propagation');
    const call = 'recoveryRecord("signal", recoveryNextCycle, requireNotNull(intent.dataString))';
    expect(handler).toContain('intent.dataString != expected');
    expect(handler).toContain(call);
    expect(handler.indexOf('intent.dataString != expected')).toBeLessThan(handler.indexOf(call));
    expect(handler.indexOf(call)).toBeLessThan(handler.indexOf('super.onNewIntent(intent)'));
    expect(handler).not.toContain('setIntent(');
    expect(handler).not.toContain('reload(');
  });

  test('Apple Codable identity has exactly six required bootstrap fields plus optional cycle/URL', () => {
    const identity = section(apple, 'private struct RecoveryHostIdentity:', '@main');
    const fields = [...identity.matchAll(/(?:let|var)\s+(\w+):\s+(\w+)(\?)?/g)];
    expect(fields.map((match) => match[1]).sort()).toEqual(signalKeys);
    expect(fields.filter((match) => !['cycle', 'url'].includes(match[1])).map((match) => match[1]).sort()).toEqual(baseKeys);
    expect(identity).toContain('let pid: Int32');
    expect(identity).toContain('var cycle: Int?');
    expect(identity).toContain('var url: String?');
    const launch = section(apple, 'let identity = RecoveryHostIdentity(run_id:', 'if let rfCancellationControl');
    expect(launch).toContain('source: "launch"');
    expect(launch).not.toMatch(/(?:cycle|url):/);
    expect(launch).toContain('writeRecoveryRecord("bootstrap", identity: identity)');
  });

  test('Apple signal copies actual validated URL, retaining boot/process identity before Linking', () => {
    const handler = section(apple, 'guard fullRecovery == nil ||', 'private func observeRecoveryMarker');
    expect(handler).toContain('var identity = recoveryIdentity');
    expect(handler).toContain('identity.cycle = recoveryNextCycle');
    expect(handler).toContain('identity.source = "payload-url"');
    expect(handler).toContain('identity.url = url.absoluteString');
    const validated = handler.indexOf('url.absoluteString == "blehub-rn-spike://recover/');
    const copied = handler.indexOf('identity.url = url.absoluteString');
    const recorded = handler.indexOf('writeRecoveryRecord("signal", identity: record)');
    const delivered = handler.indexOf('RCTLinkingManager.application(application, open: url, options: options)');
    expect(validated).toBeGreaterThanOrEqual(0);
    expect(validated).toBeLessThan(copied);
    expect(copied).toBeLessThan(recorded);
    expect(recorded).toBeLessThan(delivered);
    expect(handler).not.toMatch(/identity\.(?:run_id|pid|boot_id|bundle_id|executable)\s*=/);
  });
});

describe('combined host props and signal routing source contracts (not native execution)', () => {
  test('Android activates through existing suite props, shares boot identity and routes controls before standalone recovery', () => {
    expect(android).toContain('props.getString("runtimeMode") == "rf-lab-suite"');
    expect(android).toContain('intent.hasExtra("blehub.rfRecoveryRunId")');
    expect(android).toContain('props.putString("rfRecoveryRunId", runId)');
    expect(android).toContain('FullRecoverySignalGate(runId, requireNotNull(props.getString("rfCancellationControl")))');
    expect(android).toContain('gate.controlRecord(null, recoveryBootId, packageName, applicationInfo.sourceDir)');
    expect(android.indexOf('val full = fullRecovery')).toBeLessThan(android.indexOf('val runId = recoveryRunId'));
    expect(android).toContain('FullRecoverySignalGate.Signal.BEGIN -> recoveryRecord("begin", url = url)');
    expect(android).toContain('!begun && nextControl == urls.size');
  });
  test('Apple keeps suite runtimeMode, same session and boot, and records URL-only begin before Linking', () => {
    expect(apple).toContain('runtimeMode == "rf-lab-suite" && arguments.contains("--blehub-nitro-rf-recovery-run-id")');
    expect(apple).toContain('rfControlIdentity?.session == arguments[index + 1]');
    expect(apple).toContain('let bootId = hostBootId');
    expect(apple).toContain('boot_id: hostBootId');
    const begin = section(apple, 'if fullRecovery != nil && !recoveryLock', 'guard fullRecovery == nil ||');
    expect(begin).toContain('rfControlDelivered == rfControlSteps.count');
    expect(begin).toContain('fullRecovery!.acceptBegin(url.absoluteString, controlsComplete: complete)');
    expect(begin).toContain('identity.source = "payload-url"');
    expect(begin).toContain('identity.url = url.absoluteString');
    expect(begin).not.toContain('identity.cycle =');
    expect(begin.indexOf('writeRecoveryRecord("begin", identity: record)')).toBeLessThan(begin.indexOf('RCTLinkingManager.application(application, open: url, options: options)'));
    expect(apple).toContain('control-after-full-suite-transition');
  });
});

describe('payload URL readiness authority', () => {
  let listener: ((event: { url: string }) => void) | undefined;
  beforeEach(() => {
    jest.useFakeTimers();
    jest.spyOn(Linking, 'addEventListener').mockImplementation((_type, callback) => {
      listener = callback;
      return { remove: () => { listener = undefined; } } as any;
    });
  });
  afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); });

  test('bootstrap is not readiness: canonical payload is acknowledged only after JS awaiting', async () => {
    const report = jest.fn();
    const barrier = new RecoveryReadyBarrier(RUN, report);
    expect(report).not.toHaveBeenCalled();
    const waiting = barrier.wait(0, () => false, () => {});
    expect(report).toHaveBeenLastCalledWith(`rf-recovery-ten:cycle:0:recovery-awaiting:runId=${RUN}`);
    listener!({ url: urlFor(0) });
    await jest.runAllTimersAsync(); await waiting;
    expect(report).toHaveBeenLastCalledWith(`rf-recovery-ten:cycle:0:recovery-ready:runId=${RUN}:source=linking`);
    barrier.remove();
  });

  test.each(['/extra-token', '?token=unexpected', '#fragment', '/'])('does not normalize extra URL components into a valid ACK: %s', async (suffix) => {
    const report = jest.fn();
    const barrier = new RecoveryReadyBarrier(RUN, report);
    const waiting = barrier.wait(0, () => false, () => {});
    const rejected = expect(waiting).rejects.toThrow('foreign');
    listener!({ url: urlFor(0) + suffix });
    await jest.runAllTimersAsync(); await rejected;
    expect(report.mock.calls.some(([line]) => line.includes(':recovery-ready:'))).toBe(false);
    barrier.remove();
  });

  test('even a canonical current-cycle URL arriving before JS awaiting fails closed', () => {
    const barrier = new RecoveryReadyBarrier(RUN, () => {});
    listener!({ url: urlFor(0) });
    expect(() => barrier.assertHealthy()).toThrow('early');
    barrier.remove();
  });
});
