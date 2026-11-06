import React, { Profiler } from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react-native';
import { Platform, StatusBar, StyleSheet } from 'react-native';
import App from './App';
import { runRfLabFull } from './rf_full';

let mockReport: (line: string) => void;
let mockFinish: (passed: boolean) => void;
jest.mock('./rf_full', () => ({ runRfLabFull: jest.fn((_run, _controls, _suite, _recovery, report) => {
  mockReport = report; return new Promise<boolean>((resolve) => { mockFinish = resolve; });
}) }));
jest.mock('@blehub/react-native-internal', () => ({ createBleHubProbe: jest.fn() }));
jest.mock('react-native/Libraries/Settings/Settings.ios', () => ({ __esModule: true, default: { get: jest.fn() } }));
jest.mock('./multi_rf', () => ({ runRfMulti: jest.fn(), runRfMultiSoak: jest.fn() }));
jest.mock('./multi_fault', () => ({ runRfMultiFault: jest.fn() }));
const RUN = '12345678-1234-4234-8234-123456789abc';
const MODES = ['rf', 'rf-connect', 'rf-gatt', 'rf-io', 'rf-rssi', 'rf-notification', 'rf-indication', 'rf-passive-disconnect', 'rf-cancellation', 'rf-watchdog'];
const nodes = (): object[] => screen.root.findAll((node: { type: unknown }) => typeof node.type === 'string');
const launch = <App runtimeMode="rf-lab-suite" rfRecoveryRunId={RUN} rfCancellationControl="controls" rfServiceUuid={RUN} />;
beforeEach(() => { jest.clearAllMocks(); jest.spyOn(console, 'log').mockImplementation(() => {}); });
afterEach(() => { jest.restoreAllMocks(); });
const report = async (line: string): Promise<void> => { await act(async () => { mockReport(line); }); };

test('App maps combined props to the same suite mount with 20 fixed rows; diagnostics cause zero commits', async () => {
  jest.replaceProperty(Platform, 'OS', 'android');
  jest.replaceProperty(StatusBar, 'currentHeight', 32);
  let commits = 0;
  const view = render(<Profiler id="full" onRender={() => { commits += 1; }}>{launch}</Profiler>);
  await act(async () => {});
  expect(StyleSheet.flatten(screen.getByTestId('rf-workbench-root').props.style)).toMatchObject({ backgroundColor: '#f5f7fa', paddingTop: 32 });
  expect(screen.queryByTestId('rf-adapter-transition-page')).toBeNull();
  expect(runRfLabFull).toHaveBeenCalledTimes(1);
  expect(jest.mocked(runRfLabFull).mock.calls[0].slice(0, 2)).toEqual([RUN, 'controls']);
  const hosts = nodes(); const initialCommits = commits;
  for (let index = 0; index < 300; index += 1) await report(index % 2 ? `rf-recovery-ten:resources:cycle=0:phase=active:snapshot={"connections":1}` : `rf-recovery-ten:event:{"kind":"notificationReceived","cycle":0,"index":${index}}`);
  expect(commits).toBe(initialCommits);
  nodes().forEach((node, index) => expect(node).toBe(hosts[index]));
  expect(jest.mocked(console.log).mock.calls).toHaveLength(300);
  expect(screen.getByTestId('rf-lab-full-latest')).toHaveTextContent('Waiting for the host-controlled run.');
  for (let index = 0; index < 10; index += 1) {
    await report(`rf-lab-suite:flow-started:index=${index}:mode=${MODES[index]}`);
    await report(`rf-lab-suite:flow-passed:index=${index}:mode=${MODES[index]}`);
    expect(within(screen.getByTestId(`rf-lab-full-flow-${index}`)).getByText('OK')).toBeTruthy();
  }
  await report(`rf-lab-full:recovery-awaiting:runId=${RUN}`);
  await report(`rf-lab-full:recovery-ready:runId=${RUN}:source=linking`);
  for (let cycle = 0; cycle < 10; cycle += 1) {
    await report(`rf-recovery-ten:cycle:${cycle}:start`);
    await report(`rf-recovery-ten:cycle:${cycle}:passed`);
    expect(within(screen.getByTestId(`rf-lab-full-cycle-${cycle}`)).getByText('OK')).toBeTruthy();
  }
  const final = `rf-lab-full:passed:flows=10:cycles=10:runId=${RUN}`;
  await report(final); await act(async () => { mockFinish(true); });
  expect(screen.getByTestId('rf-lab-full-summary')).toHaveTextContent(final);
  expect(screen.getByTestId('rf-lab-suite-runtime-status')).toHaveTextContent('RF Lab full suite passed');
  expect(screen.queryAllByRole('button')).toHaveLength(3); // Tests, current run and evidence navigation only.
  expect(nodes()).toHaveLength(hosts.length);
  nodes().forEach((node, index) => expect(node).toBe(hosts[index]));
  view.rerender(<Profiler id="full" onRender={() => { commits += 1; }}><App runtimeMode="rf-lab-suite" rfRecoveryRunId="changed" /></Profiler>);
  expect(runRfLabFull).toHaveBeenCalledTimes(1);
});

test('home navigation preserves the mounted full-suite run and late results without executing another scenario', async () => {
  render(launch); await act(async () => {});
  const runRoot = screen.getByTestId('rf-lab-suite-root');
  fireEvent.press(screen.getByTestId('rf-workbench-home'));
  expect(screen.queryByTestId('rf-lab-suite-root')).toBeNull();
  expect(screen.getByTestId('rf-workbench-open-interactive').props.accessibilityState.disabled).toBe(true);
  fireEvent.press(screen.getByTestId('rf-workbench-category-lifecycle'));
  fireEvent.press(screen.getByTestId('rf-workbench-scenario-rf-adapter-transition'));
  expect(screen.getByTestId('rf-workbench-view-scenario').props.accessibilityState.disabled).toBe(true);
  await report(`rf-lab-full:passed:flows=10:cycles=10:runId=${RUN}`);
  await act(async () => { mockFinish(true); });
  fireEvent.press(screen.getByTestId('rf-workbench-current'));
  expect(screen.getByTestId('rf-lab-suite-root')).toBe(runRoot);
  expect(screen.getByTestId('rf-lab-suite-runtime-status')).toHaveTextContent('RF Lab full suite passed');
  fireEvent.press(screen.getByTestId('rf-workbench-home'));
  fireEvent.press(screen.getByTestId('rf-workbench-current'));
  expect(runRfLabFull).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId('rf-workbench-run-state')).toHaveTextContent('App checks · Passed');
  fireEvent.press(screen.getByTestId('rf-workbench-evidence'));
  expect(screen.getByText('Host gate · Verify on the host')).toBeTruthy();
  expect(screen.getByTestId('rf-lab-suite-root', { includeHiddenElements: true })).toBe(runRoot);
});

test('failures stay visible and failed rows/verdict cannot be overwritten by later success', async () => {
  render(launch); await act(async () => {});
  const hosts = nodes();
  const failure = 'rf-recovery-ten:cycle:3:failed:native close rejected';
  await report(failure);
  await report('rf-recovery-ten:cycle:3:passed');
  await report(`rf-lab-full:passed:flows=10:cycles=10:runId=${RUN}`);
  await act(async () => { mockFinish(false); });
  expect(within(screen.getByTestId('rf-lab-full-cycle-3')).getByText('FAIL')).toBeTruthy();
  expect(screen.getByTestId('rf-lab-full-failure')).toHaveTextContent(failure);
  expect(screen.getByTestId('rf-lab-full-summary')).toHaveTextContent(failure);
  expect(screen.getByTestId('rf-lab-suite-runtime-status')).toHaveTextContent('RF Lab full suite failed');
  nodes().forEach((node, index) => expect(node).toBe(hosts[index]));
});
