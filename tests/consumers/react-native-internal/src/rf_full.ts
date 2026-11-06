import { RF_CONNECTION_TIMEOUT_MS } from './rf_contract';
import { RF_BOOT_NONCE } from './rf_runtime';
import { parseRfControls, RfSuiteSignalGuard } from './rf_signal';

const attempts = new Set<string>();
const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;

/** Coordinates real executors; host readiness is not a substitute for either RF verdict. */
export async function runRfLabFull(
  runId: string, control: string | undefined,
  suite: (guard: RfSuiteSignalGuard, report: (line: string) => void) => Promise<boolean>,
  recovery: (guard: RfSuiteSignalGuard, report: (line: string) => void) => Promise<boolean>,
  report: (line: string) => void, cancelled: () => boolean,
): Promise<boolean> {
  let guard: RfSuiteSignalGuard | undefined;
  let failure: unknown;
  let started = false;
  let reportedFailure: string | undefined;
  const checkedReport = (line: string): void => {
    guard?.assertHealthy();
    if (/:failed(?::|$)|:flow-failed:|:close:rejected:/.test(line)) reportedFailure ??= line;
    report(line);
  };
  try {
    const controls = parseRfControls(control);
    if (!UUID.test(runId) || controls.some((value) => value.session !== runId)) throw new Error('full suite requires the same canonical fixture session UUID');
    if (attempts.has(runId)) throw new Error('full suite cannot be rerun');
    attempts.add(runId);
    guard = new RfSuiteSignalGuard(controls, runId);
    checkedReport(`rf-lab-full:runtime:session=${runId}:nonce=${RF_BOOT_NONCE}:phase=start`);
    started = true;
    if (!await suite(guard, checkedReport) || reportedFailure) throw new Error(reportedFailure ?? 'base suite did not pass all ten flows and native close');
    if (cancelled()) throw new Error('full suite cancelled');
    await guard.waitForBegin(checkedReport, cancelled, RF_CONNECTION_TIMEOUT_MS);
    guard.assertHealthy();
    if (!await recovery(guard, checkedReport) || reportedFailure) throw new Error(reportedFailure ?? 'recovery did not pass all ten cycles and native close');
    guard.assertHealthy();
    if (cancelled()) throw new Error('full suite cancelled');
  } catch (error) { failure = error instanceof Error ? error : new Error(String(error)); }
  finally {
    try { guard?.remove(); } catch (error) { failure ??= error; }
    if (started) report(`rf-lab-full:runtime:session=${runId}:nonce=${RF_BOOT_NONCE}:phase=close`);
  }
  report(failure ? `rf-lab-full:failed:${failure instanceof Error ? failure.message : String(failure)}`
    : `rf-lab-full:passed:flows=10:cycles=10:runId=${runId}`);
  return !failure;
}
