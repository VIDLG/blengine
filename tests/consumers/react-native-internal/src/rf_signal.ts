import { Linking } from 'react-native';

export interface RfControl {
  session: string;
  flow: number;
  mode: 'rf-cancellation' | 'rf-watchdog' | 'rf-write-reject' | 'rf-ccc-enable-reject' | 'rf-ccc-disable-reject';
  token: string;
}
export type RfSignalPhase = 'armed' | 'held' | 'released' | 'recovery' | 'rejected' | 'close-allowed' | 'value-sent' | 'disable-allowed' | 'silence-started' | 'baseline-value-sent' | 'continued-value-sent' | 'retry-allowed';
const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;
const phases: readonly RfSignalPhase[] = ['armed', 'held', 'released'];

export function parseRfControl(raw: string | undefined, mode: RfControl['mode'] = 'rf-cancellation'): RfControl {
  const parsed = JSON.parse(raw ?? 'null');
  const value = (parsed?.controls ? parseRfControls(raw).find((control) => control.mode === mode) : parsed) as RfControl | null;
  if (!value || Object.keys(value).sort().join(',') !== 'flow,mode,session,token' ||
      typeof value.session !== 'string' || typeof value.token !== 'string' ||
      !UUID.test(value.session) || !UUID.test(value.token) || value.flow !== (mode === 'rf-ccc-disable-reject' ? 12 : mode === 'rf-ccc-enable-reject' ? 11 : mode === 'rf-write-reject' ? 10 : mode === 'rf-watchdog' ? 9 : 8) || value.mode !== mode) {
    throw new Error(`${mode} requires live-attested multifunction control metadata`);
  }
  return value;
}

export function parseRfControls(raw: string | undefined): readonly RfControl[] {
  const value = JSON.parse(raw ?? 'null');
  if (!value || Object.keys(value).join(',') !== 'controls' || !Array.isArray(value.controls) || value.controls.length !== 2) {
    throw new Error('continuous10 requires a pre-provisioned hold control bundle');
  }
  const controls = [parseRfControl(JSON.stringify(value.controls[0])), parseRfControl(JSON.stringify(value.controls[1]), 'rf-watchdog')];
  if (controls[0].session !== controls[1].session || controls[0].token === controls[1].token) throw new Error('RF control bundle identity drift');
  return controls;
}

export function rfSignalUri(control: RfControl, phase: RfSignalPhase): string {
  return `blehub-rf://control/${control.session}/${control.flow}/${control.mode}/${control.token}/${phase}`;
}

const activeBarriers = new Set<RfSignalBarrier>();

/** Keep early signals fail-closed even between the two flow-local ledgers. */
export class RfSuiteSignalGuard {
  private failure?: Error;
  private readonly subscription: ReturnType<typeof Linking.addEventListener>;
  private phase: 'suite' | 'begin' | 'recovery' = 'suite';
  private beginReceived = false;
  private recoveryAccepts?: (url: string) => boolean;
  constructor(controls: readonly RfControl[], private readonly runId?: string) {
    this.subscription = Linking.addEventListener('url', ({ url }) => {
      const accepted = this.phase === 'suite'
        ? [...activeBarriers].some((barrier) => controls.some((control) => control.session === barrier.control.session && control.flow === barrier.control.flow && control.mode === barrier.control.mode && control.token === barrier.control.token) && barrier.accepts(url))
        : this.phase === 'begin'
          ? !this.beginReceived && url === `blehub-rn-spike://recover/${this.runId}/begin`
          : this.recoveryAccepts?.(url) === true;
      if (!accepted) this.failure ??= new Error('foreign, early or duplicate suite RF signal');
      else if (this.phase === 'begin') this.beginReceived = true;
    });
  }
  async waitForBegin(report: (line: string) => void, cancelled: () => boolean, timeoutMs: number): Promise<void> {
    this.assertHealthy();
    if (!this.runId || this.phase !== 'suite') throw new Error('invalid full-suite begin boundary');
    this.phase = 'begin';
    report(`rf-lab-full:recovery-awaiting:runId=${this.runId}`);
    const deadline = Date.now() + timeoutMs;
    while (!this.beginReceived) {
      this.assertHealthy();
      if (cancelled()) throw new Error('full suite begin cancelled');
      if (Date.now() >= deadline) throw new Error('full suite begin timed out');
      await new Promise<void>((resolve) => setTimeout(resolve, 25));
    }
    this.assertHealthy();
    // Reject every URL in the handoff gap until the recovery executor arms its ledger.
    this.phase = 'recovery';
    report(`rf-lab-full:recovery-ready:runId=${this.runId}:source=linking`);
  }
  useRecovery(accepts?: (url: string) => boolean): void {
    this.assertHealthy();
    if (this.phase !== 'recovery') throw new Error('recovery before full-suite begin');
    this.recoveryAccepts = accepts;
  }
  assertHealthy(): void { if (this.failure) throw this.failure; }
  remove(): void { this.subscription.remove(); this.assertHealthy(); }
}

/** Host signals authorize a test boundary, never fabricate a BLE event. */
export class RfSignalBarrier {
  private readonly subscription: ReturnType<typeof Linking.addEventListener>;
  private failure?: Error;
  private awaiting?: RfSignalPhase;
  private received = false;
  private readySent = false;
  private next = 0;
  private removed = false;
  constructor(readonly control: RfControl, private readonly report: (line: string) => void) {
    this.subscription = Linking.addEventListener('url', ({ url }) => {
      if (this.failure || this.removed) return;
      const phase = this.awaiting;
      if (!phase || !this.readySent) this.failure = new Error('early or duplicate RF control signal');
      else if (url !== rfSignalUri(control, phase)) this.failure = new Error('wrong RF control token/phase');
      else if (this.received) this.failure = new Error('duplicate RF control signal');
      else {
        this.received = true;
        this.report(this.marker('observed', phase));
      }
    });
    activeBarriers.add(this);
    try { report(this.marker('listener', 'registered')); }
    catch (error) { activeBarriers.delete(this); this.subscription.remove(); throw error; }
  }
  accepts(url: string): boolean {
    return !this.failure && !this.removed && !!this.awaiting && this.readySent && !this.received && url === rfSignalUri(this.control, this.awaiting);
  }
  private marker(kind: string, phase: string): string {
    const c = this.control;
    return `rf-control:${kind}:session=${c.session}:flow=${c.flow}:mode=${c.mode}:token=${c.token}:phase=${phase}`;
  }
  assertHealthy(): void { if (this.failure) throw this.failure; }
  expect(phase: RfSignalPhase): void {
    this.assertHealthy();
    if (this.awaiting || phase !== (this.control.mode === 'rf-ccc-disable-reject' ? ['baseline-value-sent', 'armed', 'held', 'rejected', 'continued-value-sent', 'retry-allowed', 'silence-started', 'close-allowed'] : this.control.mode === 'rf-ccc-enable-reject' ? ['armed', 'held', 'rejected', 'value-sent', 'disable-allowed', 'silence-started', 'close-allowed'] : this.control.mode === 'rf-write-reject' ? ['armed', 'held', 'rejected', 'close-allowed'] : this.control.mode === 'rf-watchdog' ? [...phases, 'recovery'] : phases)[this.next]) throw new Error('RF control phase out of order');
    this.awaiting = phase;
    this.received = false;
    this.readySent = false;
  }
  ready(phase: 'arm' | 'read-dispatched' | 'release' | 'recover' | 'write-dispatched' | 'reject' | 'recovery-done' | 'enable-dispatched' | 'recovery-enabled' | 'recovery-notified' | 'disabled' | 'silence-done' | 'baseline-enabled' | 'disable-dispatched' | 'reject-observed' | 'continued-notified'): void {
    this.assertHealthy();
    const expected = { 'baseline-enabled': 'baseline-value-sent', 'disable-dispatched': 'held', 'reject-observed': 'continued-value-sent', 'continued-notified': 'retry-allowed', arm: 'armed', 'read-dispatched': 'held', release: 'released', recover: 'recovery', 'write-dispatched': 'held', reject: 'rejected', 'recovery-done': 'close-allowed', 'enable-dispatched': 'held', 'recovery-enabled': 'value-sent', 'recovery-notified': 'disable-allowed', disabled: 'silence-started', 'silence-done': 'close-allowed' } as const;
    const allowed = this.control.mode === 'rf-ccc-disable-reject'
          ? ['baseline-enabled', 'arm', 'disable-dispatched', 'reject', 'reject-observed', 'continued-notified', 'disabled', 'silence-done'] : this.control.mode === 'rf-ccc-enable-reject'
          ? ['arm', 'enable-dispatched', 'reject', 'recovery-enabled', 'recovery-notified', 'disabled', 'silence-done'] : this.control.mode === 'rf-write-reject'
      ? ['arm', 'write-dispatched', 'reject', 'recovery-done'] : ['arm', 'read-dispatched', 'release', ...(this.control.mode === 'rf-watchdog' ? ['recover'] : [])];
    if (!allowed.includes(phase) || this.awaiting !== expected[phase] || this.readySent) throw new Error('RF control readiness before listener/expectation or duplicate');
    this.readySent = true;
    this.report(this.marker('ready', phase));
  }
  async wait(cancelled: () => boolean, healthy: () => void, timeoutMs = 3000): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    while (true) {
      this.assertHealthy();
      healthy();
      if (cancelled()) throw new Error('RF control wait cancelled');
      if (Date.now() >= deadline) throw new Error('RF control signal deadline exceeded');
      if (this.received) break;
      await new Promise<void>((resolve) => setTimeout(resolve, 25));
    }
    this.awaiting = undefined;
    this.next += 1;
  }
  remove(): void {
    this.removed = true;
    activeBarriers.delete(this);
    this.subscription.remove();
    this.assertHealthy();
  }
}
