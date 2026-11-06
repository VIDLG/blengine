import { applyRunUpdate, projectRunLine, type RunSummary } from './rf_lab_session';
test('expected write rejection remains progress, raw native failure payloads never become UI failures', () => {
  expect(projectRunLine('rf-write-reject', false, 'rf-write-reject:event:{"error":{"category":"gatt","message":"failed"}}')).toBeUndefined();
  expect(projectRunLine('rf-write-reject', false, 'rf-write-reject:step:write-rejected:passed:commandId=30010')).toMatchObject({ state: 'running', completed: 6, total: 11 });
  expect(projectRunLine('rf-write-reject', false, 'rf-write-reject:passed')).toMatchObject({ state: 'passed' });
  expect(projectRunLine('rf-lab-suite', true, 'rf-write-reject:passed')).toBeUndefined();
  expect(projectRunLine('rf-write-reject', false, 'rf-write-reject:failed:close rejected')).toMatchObject({ state: 'failed' });
});

const initial = (): RunSummary => ({ state: 'ready', phase: 'Ready', failure: '', completed: 0, total: 0 });

test('raw payloads and subflow success cannot authorize full-suite success or enter UI summary', () => {
  for (const line of ['rf-recovery-ten:event:{"message":"failed"}', 'rf-recovery-ten:resources:large-json',
    'rf-lab-full:native:bootstrap:{"run_id":"private"}', 'rf-watchdog:passed', 'rf:passed:service=fixture']) {
    expect(projectRunLine('rf-lab-suite', true, line)).toBeUndefined();
  }
  expect(projectRunLine('rf-adapter-transition', false, 'rf-lab-full:passed:flows=10')).toBeUndefined();
  expect(projectRunLine('rf-lab-suite', true, 'rf-lab-full:passed:flows=10:cycles=10')).toMatchObject({ state: 'passed' });
});

test('adapter host-ready, UI request, native identity and nested I/O success never authorize an App or host verdict', () => {
  for (const line of [
    'rf-adapter-transition:host-window-ready:runId=private:source=linking',
    'rf-adapter-transition:on-request:runId=private:source=ui',
    'rf-adapter-transition:native:bootstrap:{"pid":123,"boot_id":"private"}',
    'rf-adapter-transition:on:flow:rf-io:passed:software-only',
  ]) expect(projectRunLine('rf-adapter-transition', false, line)).toBeUndefined();
  expect(projectRunLine('rf-adapter-transition', false, 'rf-adapter-transition:awaiting-on:runId=private'))
    .toMatchObject({ state: 'running', completed: 1, total: 2 });
  expect(projectRunLine('rf-adapter-transition', false, 'rf-adapter-transition:passed:runId=private'))
    .toMatchObject({ state: 'passed', phase: 'App checks completed' });
});

test('permission summary is bounded and phase-local; audit payloads and subflow success never authorize a verdict', () => {
  const prefix = 'rf-permission-transition';
  for (const phase of ['denied', 'active', 'restored']) {
    const start = projectRunLine('rf-permission-transition', false, `${prefix}:runtime:runId=private-run:phase=${phase}:nonce=private-boot:state=start`)!;
    expect(start.state).toBe('running');
    expect(JSON.stringify(start)).not.toMatch(/private-run|private-boot/);
  }
  const markers = [
    ['baseline-passed', 'preparing held read'], ['discovery-ready:characteristicId=secret', 'waiting for host arm'],
    ['arm-signal:runId=secret:source=linking', 'preparing active / queued reads'],
    ['reads-accepted:activeCommandId=secret', 'awaiting host OS process death'],
  ];
  for (const [marker, expected] of markers) {
    const update = projectRunLine('rf-permission-transition', false, `${prefix}:active:${marker}`)!;
    expect(update).toMatchObject({ state: 'running', phase: expect.stringContaining(expected) });
    expect(JSON.stringify(update)).not.toContain('secret');
  }
  const raw = '{"failed":"private-native-identity","payload":"' + 'x'.repeat(10_000) + '"}';
  for (const line of [
    `${prefix}:active:event:${raw}`, `${prefix}:denied:resources:${raw}`, `${prefix}:active:pending-resources:${raw}`,
    `${prefix}:native:bootstrap:${raw}`, `${prefix}:native-identity:${raw}`,
    `${prefix}:active:flow:baseline:rf-io:passed:software-only`, `${prefix}:denied:passed`, `${prefix}:restored:passed`,
    `${prefix}:runtime:runId=secret:phase=restored:nonce=secret:state=close`, `${prefix}:passed`,
    `${prefix}:passed:phase=active:runId=secret`, 'rf-adapter-transition:passed',
  ]) expect(projectRunLine('rf-permission-transition', false, line)).toBeUndefined();
  for (const phase of ['denied', 'restored']) {
    const passed = projectRunLine('rf-permission-transition', false, `${prefix}:passed:phase=${phase}:runId=secret`)!;
    expect(passed).toMatchObject({ state: 'passed', phase: expect.stringContaining('current phase App checks completed; host verdict pending') });
    expect(JSON.stringify(passed)).not.toContain('secret');
    const failed = applyRunUpdate(applyRunUpdate(initial(), passed), projectRunLine('rf-permission-transition', false, `${prefix}:failed:phase=${phase}:runId=secret:${'x'.repeat(1000)}`)!);
    expect(failed.state).toBe('failed');
    expect(failed.failure).toHaveLength(320);
    expect(applyRunUpdate(failed, passed)).toBe(failed);
  }
});

test('iOS authorization summary cannot adopt Android active/death/RF phases', () => {
  const prefix = 'rf-permission-transition';
  expect(projectRunLine('permission-authorization', false, `${prefix}:runtime:runId=private:phase=authorized:nonce=private:state=start`)).toMatchObject({ state: 'running', phase: 'Authorized initialization · non-RF' });
  expect(projectRunLine('permission-authorization', false, `${prefix}:passed:phase=authorized:runId=private`)).toMatchObject({ state: 'passed', phase: expect.stringContaining('non-RF') });
  for (const line of [`${prefix}:passed:phase=restored:runId=private`, `${prefix}:active:reads-accepted:private`, `${prefix}:runtime:runId=private:phase=active:nonce=private:state=start`]) {
    expect(projectRunLine('permission-authorization', false, line)).toBeUndefined();
  }
  expect(projectRunLine('rf-permission-transition', false, `${prefix}:passed:phase=authorized:runId=private`)).toBeUndefined();
});

test('presentation progress is monotonic, failure is sticky and may replace an earlier passed display', () => {
  let state = applyRunUpdate(initial(), projectRunLine('rf-lab-suite', true, 'rf-lab-suite:flow-passed:index=9:mode=rf-watchdog')!);
  expect(state).toMatchObject({ completed: 10, total: 20 });
  state = applyRunUpdate(state, projectRunLine('rf-lab-suite', true, 'rf-recovery-ten:cycle:3:passed')!);
  expect(state.completed).toBe(14);
  state = applyRunUpdate(state, projectRunLine('rf-lab-suite', true, 'rf-lab-full:passed:flows=10:cycles=10')!);
  expect(state).toMatchObject({ state: 'passed', completed: 20 });
  state = applyRunUpdate(state, projectRunLine('rf-lab-suite', true, 'rf-recovery-ten:cycle:3:failed:late callback')!);
  expect(state.state).toBe('failed');
  expect(applyRunUpdate(state, { state: 'passed', phase: 'late success' })).toBe(state);
  expect(applyRunUpdate(state, { state: 'failed', failure: 'second failure' })).toBe(state);
});
