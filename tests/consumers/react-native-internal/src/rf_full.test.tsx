import { Linking } from 'react-native';
import { runRfLabFull } from './rf_full';
import { RecoveryReadyBarrier } from './rf_recovery_ten';
import { RF_BOOT_NONCE } from './rf_runtime';
import { RfSignalBarrier, rfSignalUri, type RfControl } from './rf_signal';

jest.mock('@blehub/react-native-internal', () => ({ createBleHubProbe: jest.fn() }));
const listeners = new Set<(event: { url: string }) => void>();
const signal = (url: string): void => { for (const listener of listeners) listener({ url }); };
let sequence = 0;
function profile() {
  const run = `12345678-1234-4234-8234-${String(++sequence).padStart(12, '0')}`;
  const cancellation: RfControl = { session: run, flow: 8, mode: 'rf-cancellation', token: '11111111-1111-4111-8111-111111111111' };
  const watchdog: RfControl = { ...cancellation, flow: 9, mode: 'rf-watchdog', token: '22222222-2222-4222-8222-222222222222' };
  return { run, controls: [cancellation, watchdog], raw: JSON.stringify({ controls: [cancellation, watchdog] }), begin: `blehub-rn-spike://recover/${run}/begin` };
}
beforeEach(() => {
  listeners.clear(); jest.useFakeTimers();
  jest.spyOn(Linking, 'addEventListener').mockImplementation((_type, callback) => {
    listeners.add(callback); return { remove: () => { listeners.delete(callback); } } as any;
  });
});
afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); });

// Mock executor verdicts here test orchestration, not RF or native execution.
test('same-session controls, begin and ten cycle URLs share a continuous guard and boot nonce', async () => {
  const p = profile(); const ledger: string[] = [];
  const report = (line: string): void => {
    ledger.push(line);
    if (line === `rf-lab-full:recovery-awaiting:runId=${p.run}`) signal(p.begin);
    const cycle = /^rf-recovery-ten:cycle:(\d):recovery-awaiting:/.exec(line);
    if (cycle) signal(`blehub-rn-spike://recover/${p.run}/${cycle[1]}`);
  };
  const suite = jest.fn(async (guard, output) => {
    for (const control of p.controls) {
      const barrier = new RfSignalBarrier(control, output);
      for (const phase of control.flow === 8 ? ['armed', 'held', 'released'] as const : ['armed', 'held', 'released', 'recovery'] as const) {
        barrier.expect(phase);
        barrier.ready(({ armed: 'arm', held: 'read-dispatched', released: 'release', recovery: 'recover' } as const)[phase]);
        signal(rfSignalUri(control, phase));
        await barrier.wait(() => false, () => guard.assertHealthy());
      }
      barrier.remove();
    }
    return true;
  });
  const recovery = jest.fn(async (guard, output) => {
    const barrier = new RecoveryReadyBarrier(p.run, output);
    guard.useRecovery((url: string) => barrier.accepts(url));
    for (let cycle = 0; cycle < 10; cycle += 1) await barrier.wait(cycle, () => false, () => guard.assertHealthy());
    barrier.remove(); guard.useRecovery(undefined);
    return true;
  });
  const done = runRfLabFull(p.run, p.raw, suite, recovery, report, () => false);
  await jest.runAllTimersAsync(); expect(await done).toBe(true);
  expect(suite).toHaveBeenCalledTimes(1); expect(recovery).toHaveBeenCalledTimes(1);
  expect(ledger).toContain(`rf-lab-full:recovery-ready:runId=${p.run}:source=linking`);
  expect(ledger.filter((line) => line.startsWith('rf-lab-full:runtime:'))).toEqual([
    `rf-lab-full:runtime:session=${p.run}:nonce=${RF_BOOT_NONCE}:phase=start`,
    `rf-lab-full:runtime:session=${p.run}:nonce=${RF_BOOT_NONCE}:phase=close`,
  ]);
  expect(ledger.at(-1)).toBe(`rf-lab-full:passed:flows=10:cycles=10:runId=${p.run}`);
  expect(listeners.size).toBe(0);
});

test.each(['false', 'throw', 'sticky', 'close'])('base %s failure never announces awaiting or runs recovery', async (fault) => {
  const p = profile(); const ledger: string[] = []; const recovery = jest.fn();
  const done = runRfLabFull(p.run, p.raw, async (_guard, report) => {
    if (fault === 'throw') throw new Error('base rejected');
    if (fault === 'sticky') report('rf-lab-suite:flow-failed:index=3:mode=rf-io:detail=failed');
    if (fault === 'close') report('rf:close:rejected:native dispose');
    return fault !== 'false';
  }, recovery, (line) => ledger.push(line), () => false);
  await jest.runAllTimersAsync(); expect(await done).toBe(false);
  expect(recovery).not.toHaveBeenCalled();
  expect(ledger.some((line) => line.includes('recovery-awaiting'))).toBe(false);
  expect(ledger.at(-1)).toMatch(/^rf-lab-full:failed:/);
});

test.each(['early-begin', 'early-cycle', 'foreign', 'duplicate', 'query', 'old-control', 'gap-cycle', 'missing'])('rejects %s without a total pass', async (fault) => {
  const p = profile(); const ledger: string[] = []; const recovery = jest.fn(async () => true);
  const done = runRfLabFull(p.run, p.raw, async () => {
    if (fault === 'early-begin') signal(p.begin);
    if (fault === 'early-cycle') signal(`blehub-rn-spike://recover/${p.run}/0`);
    return true;
  }, recovery, (line) => {
    ledger.push(line);
    if (line.includes('rf-lab-full:recovery-awaiting:') && fault !== 'missing') {
      signal(fault === 'foreign' ? p.begin.replace(p.run, '00000000-0000-0000-0000-000000000000') : fault === 'query' ? `${p.begin}?extra=1` : p.begin);
      if (fault === 'duplicate') signal(p.begin);
    }
    if (line.includes('rf-lab-full:recovery-ready:')) {
      if (fault === 'old-control') signal(rfSignalUri(p.controls[1], 'recovery'));
      if (fault === 'gap-cycle') signal(`blehub-rn-spike://recover/${p.run}/0`);
    }
  }, () => false);
  await jest.runAllTimersAsync(); expect(await done).toBe(false);
  expect(recovery).not.toHaveBeenCalled();
  expect(ledger.at(-1)).toMatch(/^rf-lab-full:failed:/);
  expect(listeners.size).toBe(0);
});

test('foreign session and rerun cannot start either executor', async () => {
  const p = profile(); const suite = jest.fn(async () => false); const recovery = jest.fn(); const report = jest.fn();
  expect(await runRfLabFull(p.run.replace('12345678', 'ABCDEFAB'), p.raw, suite, recovery, report, () => false)).toBe(false);
  expect(suite).not.toHaveBeenCalled();
  await runRfLabFull(p.run, p.raw, suite, recovery, report, () => false);
  await runRfLabFull(p.run, p.raw, suite, recovery, report, () => false);
  expect(suite).toHaveBeenCalledTimes(1);
  expect(report).toHaveBeenLastCalledWith('rf-lab-full:failed:full suite cannot be rerun');
});
