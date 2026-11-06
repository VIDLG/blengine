import React, { Profiler } from 'react';
import { act, render, screen, within } from '@testing-library/react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import App from './App';

// Exercise only the UI reporter. These fixtures neither dispatch BLE nor authorize an RF result.
let mockReport: (line: string) => void;
let mockFinish: (passed: boolean) => void;
jest.mock('./rf_recovery_ten', () => ({
  runRfRecoveryTen: jest.fn((_factory, _stream, _profile, report) => {
    mockReport = report;
    return new Promise<boolean>((resolve) => { mockFinish = resolve; });
  }),
}));
jest.mock('@blehub/react-native-internal', () => ({ createBleHubProbe: jest.fn() }));
jest.mock('react-native/Libraries/Settings/Settings.ios', () => ({ __esModule: true, default: { get: jest.fn() } }));
jest.mock('./multi_rf', () => ({ runRfMulti: jest.fn(), runRfMultiSoak: jest.fn() }));
jest.mock('./multi_fault', () => ({ runRfMultiFault: jest.fn() }));

const RUN_ID = '12345678-1234-4234-8234-123456789abc';
const SERVICE = 'fd100000-0000-4000-8000-000000000000';
const uiRows = () => Array.from({ length: 10 }, (_, cycle) => screen.getByTestId(`rf-recovery-ten-cycle-${cycle}`));
const hostNodes = (): object[] => screen.root.findAll((node: { type: unknown }) => typeof node.type === 'string');
const diagnostics = (count: number): string[] => Array.from({ length: count }, (_, index) => index % 2 === 0
  ? `rf-recovery-ten:event:${JSON.stringify({ timestamp: 1789478400000 + index, cycle: 3, runId: RUN_ID, nonce: 'same-boot', kind: 'notificationReceived', connectionId: '10304', gattSessionId: '10306', subscriptionId: '10308', value: Array(512).fill(239) })}`
  : `rf-recovery-ten:resources:cycle=3:phase=active:snapshot=${JSON.stringify({ activeScans: 0, connections: 1, gattAttributes: 8, subscriptions: 1, pendingIo: 0, watchdogs: 0, callbackRoutes: 4, ingressRoots: 1, adapterDelegates: 1, peerCacheEntries: 1, backendLeases: 1, hubRoots: 1, handoffRoots: 1, handoffPending: 0, listeners: 1, workerThreads: 2, lifecycleOwners: 1 })}`);

beforeEach(() => { jest.clearAllMocks(); jest.spyOn(console, 'log').mockImplementation(() => {}); });
afterEach(() => { jest.restoreAllMocks(); });
function mount(onRender = (): void => {}): void {
  render(<Profiler id="recovery-ui" onRender={onRender}>
    <App runtimeMode="rf-recovery-ten" rfRecoveryRunId={RUN_ID} rfServiceUuid={SERVICE} rfExpectedLocalName="BleHubNrf52840" />
  </Profiler>);
}
async function report(line: string): Promise<void> { await act(async () => { mockReport(line); }); }
function expectSameHosts(previous: ReturnType<typeof hostNodes>): void {
  const current = hostNodes();
  expect(current).toHaveLength(previous.length);
  current.forEach((node, index) => expect(node).toBe(previous[index]));
}

describe('strict recovery fixed-host reporting UI', () => {
  test('every diagnostic reaches console unchanged, with zero UI commits or host-list churn', async () => {
    let commits = 0;
    mount(() => { commits += 1; });
    const initialCommits = commits;
    const nodes = hostNodes();
    const lines = diagnostics(600);
    for (const line of lines) await report(line); // Separate deliveries, not one artificial React batch.
    expect(commits).toBe(initialCommits);
    expectSameHosts(nodes);
    expect(jest.mocked(console.log).mock.calls).toEqual(lines.map((line) => [`BLEHUB_NITRO_RUNTIME ${line}`]));
    expect(screen.getByTestId('rf-recovery-ten-latest').props.children).toBe('Waiting for the host-controlled run.');
    expect(screen.getByTestId('rf-recovery-ten-failure').props.children).toBe('No failure reported.');
    expect(screen.getByTestId('rf-recovery-ten-result').props.children).toBe('Awaiting final result.');
    expect(createBleHubProbe).not.toHaveBeenCalled();
  });

  test('ten cycle rows and three summary Text nodes keep their identity from start through the final page', async () => {
    mount();
    const nodes = hostNodes();
    const rows = uiRows();
    const summaries = ['latest', 'failure', 'result'].map((part) => screen.getByTestId(`rf-recovery-ten-${part}`));
    rows.forEach((row) => expect(row.props.collapsable).toBe(false));
    expect(screen.getByTestId('runtime-report-card').props.collapsable).toBe(false);
    expect(screen.getByTestId('runtime-scroll').props.removeClippedSubviews).toBe(false);
    await report(`rf-recovery-ten:runtime:runId=${RUN_ID}:nonce=same-boot:phase=start`);
    for (let cycle = 0; cycle < 10; cycle += 1) {
      await report(`rf-recovery-ten:cycle:${cycle}:start`);
      expect(within(rows[cycle]).getByText('In progress: start')).toBeTruthy();
      await report(`rf-recovery-ten:cycle:${cycle}:scan:passed:scanSessionId=${10001 + cycle * 100}`);
      expect(within(rows[cycle]).getByText('In progress: scan')).toBeTruthy();
      await report(`rf-recovery-ten:cycle:${cycle}:recovery-awaiting:runId=${RUN_ID}`);
      expect(within(rows[cycle]).getByText('In progress: recovery awaiting')).toBeTruthy();
      await report(`rf-recovery-ten:cycle:${cycle}:cycle:passed:connectionId=${10004 + cycle * 100}`);
      await report(`rf-recovery-ten:cycle:${cycle}:passed`);
      expect(within(rows[cycle]).getByText('OK')).toBeTruthy();
      expectSameHosts(nodes);
    }
    await report('rf-recovery-ten:close:passed:listener-removed=true:probe-closed=true');
    await report(`rf-recovery-ten:runtime:runId=${RUN_ID}:nonce=same-boot:phase=close`);
    const final = `rf-recovery-ten:passed:cycles=10:runId=${RUN_ID}`;
    await report(final);
    await act(async () => { mockFinish(true); });
    expectSameHosts(nodes);
    uiRows().forEach((row, cycle) => expect(row).toBe(rows[cycle]));
    ['latest', 'failure', 'result'].forEach((part, index) => expect(screen.getByTestId(`rf-recovery-ten-${part}`)).toBe(summaries[index]));
    expect(screen.getByTestId('rf-recovery-ten-result').props.children).toBe(final);
    expect(screen.getByTestId('runtime-status').props.children).toBe('RF recovery 10 cycles passed');
    expect(screen.getByTestId('recreate-run-button').props.accessibilityState.disabled).toBe(true);
    expect(screen.getByTestId('close-probe-button').props.accessibilityState.disabled).toBe(true);
    expect(createBleHubProbe).not.toHaveBeenCalled();
  });

  test('cycle failure is immediately visible while running and cannot be evicted by diagnostics or overwritten by success', async () => {
    mount();
    const nodes = hostNodes();
    const reason = 'closed resource callbackRoutes: expected 0, got 1';
    const failure = `rf-recovery-ten:cycle:3:failed:${reason}`;
    await report(failure);
    expect(screen.getByTestId('runtime-status').props.children).toBe('RF recovery 10 cycles failed');
    expect(within(uiRows()[3]).getByText('FAIL')).toBeTruthy();
    for (const line of diagnostics(100)) await report(line);
    await report(`rf-recovery-ten:runtime:runId=${RUN_ID}:nonce=same-boot:phase=close`);
    await report(`rf-recovery-ten:passed:cycles=10:runId=${RUN_ID}`); // Adversarial reporter order, not RF evidence.
    await act(async () => { mockFinish(false); });
    expectSameHosts(nodes);
    expect(screen.getByTestId('rf-recovery-ten-failure').props.children).toBe(failure);
    expect(screen.getByTestId('rf-recovery-ten-result').props.children).toBe(failure);
    expect(screen.getByTestId('runtime-status').props.children).toBe('RF recovery 10 cycles failed');
    expect(jest.mocked(console.log).mock.calls.some(([line]) => line === `BLEHUB_NITRO_RUNTIME ${failure}`)).toBe(true);
  });

  test('a failure arriving after the result replaces the visible passed verdict without replacing native nodes', async () => {
    mount();
    const nodes = hostNodes();
    await report(`rf-recovery-ten:passed:cycles=10:runId=${RUN_ID}`);
    await act(async () => { mockFinish(true); });
    expect(screen.getByTestId('runtime-status').props.children).toBe('RF recovery 10 cycles passed');
    const failure = 'rf-recovery-ten:failed:JS callback after resolved native close';
    await report(failure);
    expectSameHosts(nodes);
    expect(screen.getByTestId('runtime-status').props.children).toBe('RF recovery 10 cycles failed');
    expect(screen.getByTestId('rf-recovery-ten-failure').props.children).toBe(failure);
    expect(screen.getByTestId('rf-recovery-ten-result').props.children).toBe(failure);
  });
});
