import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { RuntimeMode } from './rf_contract';

export type RunState = 'ready' | 'running' | 'passed' | 'failed';
export interface RunSummary {
  state: RunState;
  phase: string;
  failure: string;
  completed: number;
  total: number;
}
type Update = Partial<Omit<RunSummary, 'failure'>> & { failure?: string };
const idle = (): RunSummary => ({ state: 'ready', phase: 'Ready', failure: '', completed: 0, total: 0 });
const noop = (): void => {};
const RunContext = createContext({ report: (_line: string): void => {}, begin: noop, update: (_value: Update): void => {} });
export const RunSessionProvider = RunContext.Provider;
export const useRunReporter = (): ((line: string) => void) => useContext(RunContext).report;
export const useBeginRun = (): (() => void) => useContext(RunContext).begin;

/** Existing UI owners can publish their computed outcome without another BLE state machine. */
export function useRunOutcome(state: RunState, phase: string): void {
  const { update } = useContext(RunContext);
  useEffect(() => { update({ state, phase }); }, [update, state, phase]);
}

/** Presentation only. Raw events, resource payloads and native identity records never enter React state. */
export function projectRunLine(mode: RuntimeMode | undefined, fullSuite: boolean, line: string): Update | undefined {
  if (/:event:|:resources:|:pending-resources:|:native:/.test(line)) return;
  if (mode === 'rf-permission-transition' || mode === 'permission-authorization') {
    if (line.startsWith('rf-permission-transition:failed:')) {
      return { state: 'failed', phase: 'Permission phase App checks failed', failure: line.slice(0, 320) };
    }
    const start = /^rf-permission-transition:runtime:runId=[^:]+:phase=(denied|active|restored|authorized):nonce=[^:]+:state=start$/.exec(line);
    const allowed = mode === 'permission-authorization' ? ['denied', 'authorized'] : ['denied', 'active', 'restored'];
    if (start && !allowed.includes(start[1])) return;
    if (start) return { state: 'running', phase: start[1] === 'denied' ? 'Denied start · checking Permission rejection'
      : start[1] === 'active' ? 'Active revocation · preparing baseline I/O'
            : start[1] === 'authorized' ? 'Authorized initialization · non-RF' : 'Restored permissions · fresh-App I/O' };
    if (mode === 'permission-authorization' && line.startsWith('rf-permission-transition:active:')) return;
    if (line === 'rf-permission-transition:active:baseline-passed') return { state: 'running', phase: 'Baseline passed · preparing held read' };
    if (line.startsWith('rf-permission-transition:active:discovery-ready:')) return { state: 'running', phase: 'GATT ready · waiting for host arm signal' };
    if (line.startsWith('rf-permission-transition:active:arm-signal:')) return { state: 'running', phase: 'Host armed · preparing active / queued reads' };
    if (line.startsWith('rf-permission-transition:active:reads-accepted:')) return { state: 'running', phase: 'Two reads pending · awaiting host OS process death (8s)' };
    const passed = /^rf-permission-transition:passed:phase=(denied|restored|authorized):runId=[^:]+$/.exec(line);
    if (passed && !allowed.includes(passed[1])) return;
    if (passed) return { state: 'passed', phase: `${passed[1] === 'denied' ? 'Denied start' : passed[1] === 'authorized' ? 'Authorized initialization (non-RF)' : 'Fresh-App recovery'} · current phase App checks completed; host verdict pending` };
    return;
  }
  const prefixes = mode === 'rf-lab-suite' && fullSuite
    ? ['rf-lab-full:', 'rf-lab-suite:', 'rf-recovery-ten:', 'rf-watchdog:', 'rf:']
    : mode ? [`${mode}:`] : [];
  if (!prefixes.some((prefix) => line.startsWith(prefix))) return;
  if (/:failed(?::|$)|:flow-failed:|:close:rejected:/.test(line)) {
    return { state: 'failed', phase: 'App checks failed', failure: line.slice(0, 320) };
  }
  const finalPrefix = mode === 'rf-lab-suite' && fullSuite ? 'rf-lab-full' : mode;
  if (line.startsWith(`${finalPrefix}:passed:`) || line === `${finalPrefix}:passed`) {
    return { state: 'passed', phase: 'App checks completed' };
  }
  if (mode === 'rf-ccc-disable-reject') {
    const stages = ['initialize', 'scan', 'connected', 'gatt', 'baseline-read', 'baseline-enabled', 'baseline-notified',
      'disable-dispatched', 'queued-read-dispatched', 'disable-rejected', 'queued-read', 'continued-notified', 'disabled',
      'post-disable-read', 'silence', 'end-silence-read', 'disconnected', 'close'];
    const step = /^rf-ccc-disable-reject:step:([^:]+):passed:/.exec(line);
    if (step && stages.includes(step[1])) return { state: 'running', phase: `CCC disable REJECT · ${step[1]}`, completed: stages.indexOf(step[1]) + 1, total: stages.length };
  }
  if (mode === 'rf-ccc-enable-reject') {
      const stages = ['initialize', 'scan', 'connected', 'gatt', 'baseline-read', 'enable-dispatched', 'queued-read-dispatched',
        'enable-rejected', 'queued-read', 'recovery-enabled', 'recovery-notified', 'disabled', 'post-disable-read', 'silence', 'end-silence-read', 'disconnected', 'close'];
      const step = /^rf-ccc-enable-reject:step:([^:]+):passed:/.exec(line);
      if (step && stages.includes(step[1])) return { state: 'running', phase: `CCC enable REJECT · ${step[1]}`, completed: stages.indexOf(step[1]) + 1, total: stages.length };
    }
    if (mode === 'rf-write-reject') {
    const step = /^rf-write-reject:step:(connected|gatt|baseline-read|write-dispatched|queued-read-dispatched|write-rejected|unchanged-read|recovery-write|recovery-read|disconnected|close):passed:/.exec(line);
    const stages = ['connected', 'gatt', 'baseline-read', 'write-dispatched', 'queued-read-dispatched', 'write-rejected', 'unchanged-read', 'recovery-write', 'recovery-read', 'disconnected', 'close'];
    if (step) return { state: 'running', phase: `Response-write REJECT · ${step[1]}`, completed: stages.indexOf(step[1]) + 1, total: stages.length };
  }
  if (mode === 'rf-lab-suite' && fullSuite) {
    const flow = /^rf-lab-suite:flow-(started|passed):index=([0-9]):/.exec(line);
    if (flow) return { state: 'running', phase: `RF flows · ${Number(flow[2]) + 1}/10`, completed: Number(flow[2]) + (flow[1] === 'passed' ? 1 : 0), total: 20 };
    const cycle = /^rf-recovery-ten:cycle:([0-9]):(start|passed)$/.exec(line);
    if (cycle) return { state: 'running', phase: `Recovery cycles · ${Number(cycle[1]) + 1}/10`, completed: 10 + Number(cycle[1]) + (cycle[2] === 'passed' ? 1 : 0), total: 20 };
  }
  if (mode === 'rf-adapter-transition') {
    if (line.includes(':active:setup-ready:')) return { state: 'running', phase: 'Preparing held read' };
    if (line.includes(':active:reads-accepted:')) return { state: 'running', phase: 'Waiting for synchronized radio loss' };
    if (line.includes(':active:settled:')) return { state: 'running', phase: 'Old connection retired' };
    if (line.includes(':awaiting-on:')) return { state: 'running', phase: 'Waiting for host to enable Bluetooth', completed: 1, total: 2 };
    if (line === 'rf-adapter-transition:on:started') return { state: 'running', phase: 'Fresh-Probe RF recovery', completed: 1, total: 2 };
  }
  if (/:runtime:.*:phase=start$/.test(line) || /:start(?::|$)/.test(line)) return { state: 'running', phase: 'Preparing test' };
}

export function applyRunUpdate(current: RunSummary, update: Update): RunSummary {
  if (current.state === 'failed') return current;
  if (current.state === 'passed' && update.state !== 'failed') return current;
  const next = { ...current, ...update, completed: Math.max(current.completed, update.completed ?? 0) };
  if (next.state === 'passed' && next.total) next.completed = next.total;
  return Object.keys(next).every((key) => next[key as keyof RunSummary] === current[key as keyof RunSummary]) ? current : next;
}

export function useRunPresentation(mode: RuntimeMode | undefined, fullSuite: boolean) {
  const [summary, setSummary] = useState(idle);
  const update = useCallback((value: Update): void => { setSummary((current) => applyRunUpdate(current, value)); }, []);
  const report = useCallback((line: string): void => {
    const value = projectRunLine(mode, fullSuite, line);
    if (value) update(value);
  }, [mode, fullSuite, update]);
  const begin = useCallback((): void => { setSummary({ ...idle(), state: 'running', phase: 'Preparing test' }); }, []);
  const value = useMemo(() => ({ report, begin, update }), [report, begin, update]);
  return { summary, value };
}
