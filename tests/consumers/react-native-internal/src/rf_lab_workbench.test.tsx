import React, { Profiler } from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react-native';
import { Platform } from 'react-native';
import { createBleHubProbe } from '@blehub/react-native-internal';
import App from './App';
import { RfPermissionTransition } from './rf_permission_transition';
import type { PermissionTransitionFlow, PermissionTransitionProfile } from './rf_permission_transition';

let mockPermissionReport: (line: string) => void;
const mockPermissionUnmount = jest.fn();
jest.mock('./rf_permission_transition', () => ({ RfPermissionTransition: jest.fn((props: PermissionTransitionProfile & { runFlow: PermissionTransitionFlow }) => {
  const React = require('react');
  const { View, Text } = require('react-native');
  mockPermissionReport = require('./rf_lab_session').useRunReporter();
  React.useEffect(() => () => mockPermissionUnmount(), []);
  return React.createElement(View, { testID: 'permission-runtime' }, React.createElement(Text, null, props.rfPermissionPhase));
}) }));

jest.mock('@blehub/react-native-internal', () => ({ createBleHubProbe: jest.fn() }));
jest.mock('react-native/Libraries/Settings/Settings.ios', () => ({ __esModule: true, default: { get: jest.fn() } }));
jest.mock('./multi_rf', () => ({ runRfMulti: jest.fn(), runRfMultiSoak: jest.fn() }));
jest.mock('./multi_fault', () => ({ runRfMultiFault: jest.fn() }));
beforeEach(() => { jest.clearAllMocks(); jest.replaceProperty(Platform, 'OS', 'android'); });
afterEach(() => { jest.restoreAllMocks(); });

// Software-only navigation and information hierarchy; no native/radio evidence.
test('category → scenario → setup never dispatches BLE or starts a host-controlled permission gate', () => {
  render(<App />);
  expect(screen.getByTestId('rf-workbench-session-mode')).toHaveTextContent('No test started');
  expect(screen.getByTestId('rf-workbench-current').props.accessibilityState.disabled).toBe(true);
  expect(screen.getByTestId('rf-workbench-open-interactive').props.accessibilityState.disabled).toBe(true);
  expect(screen.queryByTestId('rf-workbench-setup')).toBeNull();
  fireEvent.press(screen.getByTestId('rf-workbench-scenario-full'));
  expect(screen.getByTestId('rf-workbench-view-scenario').props.accessibilityState.disabled).toBe(true);
  expect(within(screen.getByTestId('rf-workbench-setup')).getByText(/host-android-rf-lab-suite/)).toBeTruthy();
  fireEvent.press(screen.getByTestId('rf-workbench-category-lifecycle'));
  expect(screen.queryByTestId('rf-workbench-setup')).toBeNull();
  fireEvent.press(screen.getByTestId('rf-workbench-scenario-permission-transition'));
  expect(within(screen.getByTestId('rf-workbench-setup')).getByText('Host-controlled')).toBeTruthy();
    expect(within(screen.getByTestId('rf-workbench-setup')).getByText(/One attested DK/)).toBeTruthy();
    expect(RfPermissionTransition).not.toHaveBeenCalled();
  expect(screen.getByTestId('rf-workbench-view-scenario').props.accessibilityState.disabled).toBe(true);
  expect(screen.queryByTestId('rf-lab-suite-root')).toBeNull();
  expect(createBleHubProbe).not.toHaveBeenCalled();
});

test.each(['denied', 'active', 'restored'] as const)('permission %s stays in the same workbench across navigation and receives the RF executor', async (phase) => {
  const runId = '12345678-1234-4234-8234-123456789abc';
  const service = 'fd100000-0000-4000-8000-000000000000';
  let commits = 0;
  const view = render(<Profiler id="permission" onRender={() => { commits += 1; }}>
    <App runtimeMode="rf-permission-transition" rfPermissionRunId={runId} rfPermissionPhase={phase}
      rfServiceUuid={service} rfExpectedLocalName="BleHubNrf52840" />
  </Profiler>);
  const runtime = screen.getByTestId('permission-runtime');
  const injected = jest.mocked(RfPermissionTransition).mock.calls[0][0];
  expect(injected).toMatchObject({ rfPermissionRunId: runId, rfPermissionPhase: phase,
    rfServiceUuid: service, rfExpectedLocalName: 'BleHubNrf52840', runFlow: expect.any(Function) });
  expect(injected.runFlow.name).toBe('runRfLabFlow');
  await act(async () => { mockPermissionReport(`rf-permission-transition:runtime:runId=${runId}:phase=${phase}:nonce=private:state=start`); });
  expect(screen.getByTestId('rf-workbench-run-state')).toHaveTextContent('App checks · Running');
  const beforeAudit = commits;
  await act(async () => {
    for (let index = 0; index < 100; index += 1) {
      mockPermissionReport(`rf-permission-transition:active:event:{"failed":"raw-${index}"}`);
      mockPermissionReport('rf-permission-transition:active:pending-resources:{"pendingIo":2}');
      mockPermissionReport('rf-permission-transition:native-identity:{"pid":123,"boot":"private"}');
    }
  });
  expect(commits).toBe(beforeAudit);
  fireEvent.press(screen.getByTestId('rf-workbench-home'));
  expect(screen.getByTestId('rf-workbench-session-mode')).toHaveTextContent('Android permission transition');
  fireEvent.press(screen.getByTestId('rf-workbench-category-lifecycle'));
  fireEvent.press(screen.getByTestId('rf-workbench-scenario-permission-transition'));
  expect(screen.getByTestId('rf-workbench-view-scenario').props.accessibilityState.disabled).toBe(false);
  expect(screen.getByTestId('rf-workbench-open-interactive').props.accessibilityState.disabled).toBe(true);
  expect(screen.getByTestId('permission-runtime', { includeHiddenElements: true })).toBe(runtime);
  fireEvent.press(screen.getByTestId('rf-workbench-view-scenario'));
  expect(screen.getByTestId('permission-runtime')).toBe(runtime);
  fireEvent.press(screen.getByTestId('rf-workbench-evidence'));
  expect(screen.getByText(`Session: ${runId}`)).toBeTruthy();
  expect(screen.getByText(`Service: ${service}`)).toBeTruthy();
  expect(screen.getByText('Host gate · Verify on the host')).toBeTruthy();
  await act(async () => { mockPermissionReport(phase === 'active'
    ? `rf-permission-transition:active:reads-accepted:activeCommandId=52008:runId=${runId}`
    : `rf-permission-transition:passed:phase=${phase}:runId=${runId}`); });
  fireEvent.press(screen.getByTestId('rf-workbench-current'));
  expect(screen.getByTestId('permission-runtime')).toBe(runtime);
  expect(screen.getByTestId('rf-workbench-run-state')).toHaveTextContent(`App checks · ${phase === 'active' ? 'Running' : 'Passed'}`);
  expect(screen.getByTestId('rf-workbench-run-phase')).toHaveTextContent(phase === 'active' ? /awaiting host OS process death/ : /current phase App checks completed; host verdict pending/);
  fireEvent.press(screen.getByTestId('rf-workbench-evidence'));
  const failure = `rf-permission-transition:failed:phase=${phase}:runId=${runId}:software-only failure`;
  await act(async () => {
    mockPermissionReport(failure);
    mockPermissionReport(`rf-permission-transition:passed:phase=restored:runId=${runId}`);
  });
  expect(screen.getByTestId('rf-workbench-failure')).toHaveTextContent(failure);
  fireEvent.press(screen.getByTestId('rf-workbench-current'));
  expect(screen.getByTestId('rf-workbench-run-state')).toHaveTextContent('App checks · Failed');
  expect(screen.getByTestId('permission-runtime')).toBe(runtime);
  expect(mockPermissionUnmount).not.toHaveBeenCalled();
  expect(createBleHubProbe).not.toHaveBeenCalled();
  view.unmount();
  expect(mockPermissionUnmount).toHaveBeenCalledTimes(1);
});

test('interactive details retain selection across navigation; technical identity stays in Evidence', () => {
  const service = 'fd500000-0000-4000-8000-000000000000';
  render(<App runtimeMode="rf-workbench" rfServiceUuid={service} />);
  expect(screen.queryByText(`Service: ${service}`)).toBeNull();
  fireEvent.press(screen.getByTestId('rf-workbench-open-interactive'));
  fireEvent.press(screen.getByTestId('rf-lab-mode-rf-io'));
  const run = screen.getByTestId('rf-lab-run-button');
  fireEvent.press(screen.getByTestId('rf-workbench-home'));
  expect(screen.queryByTestId('rf-lab-run-button')).toBeNull();
  expect(screen.getByTestId('rf-workbench-active', { includeHiddenElements: true }).props.importantForAccessibility).toBe('no-hide-descendants');
  fireEvent.press(screen.getByTestId('rf-workbench-category-diagnostics'));
  fireEvent.press(screen.getByTestId('rf-workbench-scenario-rf-lab'));
  fireEvent.press(screen.getByTestId('rf-workbench-view-scenario'));
  expect(screen.getByTestId('rf-lab-run-button')).toBe(run);
  expect(run).toHaveTextContent('Run GATT I/O');
  fireEvent.press(screen.getByTestId('rf-workbench-evidence'));
  expect(screen.getByText(`Service: ${service}`)).toBeTruthy();
  expect(screen.getByText('Host gate · Verify on the host')).toBeTruthy();
  fireEvent.press(screen.getByTestId('rf-workbench-current'));
  expect(screen.getByTestId('rf-lab-run-button')).toBe(run);
  expect(createBleHubProbe).not.toHaveBeenCalled();
});

test.each(['rf-adapter-transition', 'permission-transition'])('iOS catalog marks Android-only %s unavailable, not planned', (scenario) => {
  jest.replaceProperty(Platform, 'OS', 'ios');
  render(<App runtimeMode="rf-workbench" />);
  fireEvent.press(screen.getByTestId('rf-workbench-category-lifecycle'));
  fireEvent.press(screen.getByTestId(`rf-workbench-scenario-${scenario}`));
  expect(screen.getByText('Not available on this platform')).toBeTruthy();
  expect(screen.getByText('No maintained host recipe on this platform.')).toBeTruthy();
  expect(screen.getByTestId('rf-workbench-view-scenario').props.accessibilityState.disabled).toBe(true);
  if (scenario === 'rf-adapter-transition') {
    expect(screen.getByText(/iOS startup-off candidate foundation only/)).toBeTruthy();
    expect(screen.getByText(/not an available maintained iOS live gate/)).toBeTruthy();
  }
  expect(createBleHubProbe).not.toHaveBeenCalled();
});
