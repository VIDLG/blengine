import React, { useCallback, useEffect, useRef, useState } from 'react';
import { RfWatchdogLab, runRfWatchdog } from './rf_watchdog';
import { RfWriteRejectLab, writeRejectSession } from './rf_write_reject';
import { RfCccEnableRejectLab, cccEnableRejectSession } from './rf_ccc_enable_reject';
import { RfCccDisableRejectLab, cccDisableRejectSession } from './rf_ccc_disable_reject';
import { RfAdapterTransition } from './rf_adapter_transition';
import { RfPermissionTransition } from './rf_permission_transition';
import type { PermissionTransitionProfile } from './rf_permission_transition';
import { RfLabRunScreen, styles } from './rf_lab_screen';
import { RfLabWorkbench } from './rf_lab_workbench';
import { findScenario } from './rf_lab_catalog';
import { useBeginRun, useRunOutcome, useRunReporter } from './rf_lab_session';
import { runRfRecoveryTen } from './rf_recovery_ten';
import { runRfLabFull } from './rf_full';
import { RF_BOOT_NONCE } from './rf_runtime';
import { bindPendingReloadProbe } from './pending_reload_runtime';
import { parseRfControl, parseRfControls, RfSignalBarrier, RfSuiteSignalGuard } from './rf_signal';
import {
  Pressable,
  ScrollView,
  Linking,
  TextInput,
  Platform,

  Text,
  View,
} from 'react-native';

import type {
  EventError,
  RfScanEvent,
} from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import { createBleHubProbe } from '@blehub/react-native-internal';
import { runRfMulti, runRfMultiSoak } from './multi_rf';
import { runRfMultiFault } from './multi_fault';

import {
  COMPLETION_TIMEOUT_MS,
  INITIAL_RF_CANCELLATION_STEPS,
  INITIAL_RF_CONNECT_STEPS,
  INITIAL_RF_GATT_STEPS,
  INITIAL_RF_INDICATION_STEPS,
  INITIAL_RF_IO_STEPS,
  INITIAL_RF_NOTIFICATION_STEPS,
  INITIAL_RF_PASSIVE_DISCONNECT_STEPS,
  INITIAL_RF_RECOVERY_STEPS,
  INITIAL_RF_RECOVERY_TEN_STEPS,
  INITIAL_RF_RSSI_STEPS,
  INITIAL_RF_STEPS,
  INITIAL_STEPS,
  INVALID_COMMAND_IDS,
  isCanonicalU64,
  isRfMode,
  LISTENER_REMOVAL_COMMAND,
  LISTENER_REMOVAL_WAIT_MS,
  MAX_RF_EVENT_COUNT,
  MAX_U64,
  PENDING_READ_ARM_TIMEOUT_MS,
  RF_CANCELLATION_ACTIVE_COMMAND,
  RF_CANCELLATION_QUEUED_COMMAND,
  RF_CANCELLATION_RECOVERY_COMMAND,
  RF_CANCELLATION_UNKNOWN_TARGET,
  RF_CONNECTION_ID,
  RF_CONNECTION_TIMEOUT_MS,
  RF_CONNECT_COMMAND,
  RF_DISCONNECT_COMMAND,
  RF_EXPECTED_LOCAL_NAME,
  RF_GATT_COMMAND,
  RF_GATT_SESSION,
  RF_INDICATION_DISABLE_COMMAND,
  RF_INDICATION_ENABLE_COMMAND,
  RF_INDICATION_REJECTED_SUBSCRIPTION,
  RF_INDICATION_REJECTION_COMMAND,
  RF_INDICATION_SUBSCRIPTION,
  RF_INITIALIZATION_TIMEOUT_MS,
  RF_INITIALIZE_COMMAND,
  RF_NOTIFICATION_DISABLE_COMMAND,
  RF_NOTIFICATION_ENABLE_COMMAND,
  RF_NOTIFICATION_SUBSCRIPTION,
  RF_POST_DISCONNECT_OBSERVATION_MS,
  RF_POST_NOTIFICATION_DISABLE_OBSERVATION_MS,
  RF_POST_STOP_OBSERVATION_MS,
  RF_READ_INITIAL_COMMAND,
  RF_READ_RESPONSE_COMMAND,
  RF_READ_WITHOUT_RESPONSE_COMMAND,
  RF_RECOVERY_CONNECTION_ID,
  RF_RECOVERY_CONNECT_COMMAND,
  RF_RECOVERY_DISCONNECT_COMMAND,
  RF_RSSI_COMMAND,
  RF_SCAN_SESSION,
  RF_SCAN_TIMEOUT_MS,
  RF_START_COMMAND,
  RF_STOP_COMMAND,
  RF_WRITE_RESPONSE_COMMAND,
  RF_WRITE_WITHOUT_RESPONSE_COMMAND,
  rfMarkerPrefix,
  SAMPLE_BYTES,
  STRESS_CYCLES,
  STRESS_WAIT_MS,
  validateRfGattDatabase,
} from './rf_contract';
import type {
  RfStep,
  RfStepId,
  RfSubscriptionMode,
  RuntimeMode,
  Step,
  StepId,
  StepState,
} from './rf_contract';

type Probe = ReturnType<typeof createBleHubProbe>;

// One identity per JS boot, not per invocation or React render.
const PENDING_READ_RUNTIME_NONCE = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
const pendingReadRuntimeAttempts = new Set<string>();

interface AppProps {
  runtimeMode?: RuntimeMode;
  rfServiceUuid?: string;
  rfExpectedLocalName?: string;
  rfMultiNotificationMode?: string;
  rfMultiSubscriptionMode?: string;
  rfMultiCycles?: string;
  rfReloadNotificationName?: string;
  rfPendingReadRunId?: string;
  rfRecoveryRunId?: string;
  rfAdapterRunId?: string;
  rfAdapterMode?: 'startup-off' | 'active-loss';
  rfAdapterNativeIdentity?: string;
  rfPermissionRunId?: string;
  rfPermissionPhase?: PermissionTransitionProfile['rfPermissionPhase'];
  rfPermissionNativeIdentity?: string;
  rfPendingReadGeneration?: number;
  rfLabSuiteFlow?: string;
  rfCancellationControl?: string;
  rfWriteRejectControl?: string;
    rfCccEnableRejectControl?: string;
      rfCccDisableRejectControl?: string;
}




function sameBytes(left: ArrayBuffer, right: Uint8Array): boolean {
  const actual = new Uint8Array(left);
  return actual.length === right.length && actual.every((value, index) => value === right[index]);
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}



function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}


function rfStepState(line: string): { id: RfStepId; state: StepState; detail: string } | undefined {
  const match = /^rf:step:(initialize|scan-start|advertisement|scan-stop|silence|connecting|connected|gatt-discovery|rssi|read-initial|write-response|read-response|write-without-response|read-without-response|indication-rejection|notification-enable|notification-value|notification-disable|indication-enable|indication-value|indication-disable|cancellation-active-rejection|cancellation-queued-accepted|cancellation-terminal|cancellation-active-completion|cancellation-recovery|cancellation-late-silence|cancellation-unknown-target|cancellation-completed-target|disconnected|connection-silence|recovery-connecting|recovery-connected|recovery-disconnected|recovery-connection-silence|close):(passed|failed):/.exec(line);
  if (!match) return undefined;
  return {
    id: match[1] as RfStepId,
    state: match[2] as StepState,
    detail: line,
  };
}

interface RfEventStream {
  events: RfScanEvent[];
  waitFor(
    predicate: (event: RfScanEvent) => boolean,
    timeoutMilliseconds: number,
    label: string,
    isCancelled: () => boolean,
  ): Promise<RfScanEvent>;
  waitForNext(timeoutMilliseconds: number, label: string, isCancelled: () => boolean): Promise<RfScanEvent>;
  expectCommandFailure(commandId: string, category: string): void;
  consumeAll(predicate: (event: RfScanEvent) => boolean): number;
  assertHealthy(): void;
  remove(): void;
}

function rfEventFailure(event: RfScanEvent, expectedCommandFailures: Map<string, string>): Error | undefined {
  if (event.kind === 'adapterSnapshotChanged' &&
      (event.adapterState !== 'PoweredOn' || event.scanAuthorization !== 'Authorized')) {
    return new Error(
      `adapter unavailable:state=${event.adapterState ?? 'missing'}:scanAuthorization=${event.scanAuthorization ?? 'missing'}`,
    );
  }
  if (event.kind === 'backendError') {
    return new Error(`backendError:${event.error?.category ?? 'unknown'}:${event.error?.message ?? 'backend reported an error'}`);
  }
  if (event.kind === 'scanStopped' && event.error) {
    return new Error(`scanStopped.err:${event.error.category}:${event.error.message}`);
  }
  if (event.kind === 'commandCompleted' && event.error) {
    if (event.commandId && expectedCommandFailures.get(event.commandId) === event.error.category) {
      expectedCommandFailures.delete(event.commandId);
      return undefined;
    }
    return new Error(`commandCompleted.error:${event.commandId ?? 'missing'}:${event.error.category}:${event.error.message}`);
  }
  return undefined;
}

export function createRfEventStream(
  probe: Probe,
  connectMode = false,
  passiveDisconnectMode = false,
  passiveConnectionIds: ReadonlySet<string> = new Set([RF_CONNECTION_ID]),
  maxEvents = MAX_RF_EVENT_COUNT,
  rejectEvent?: (event: RfScanEvent) => boolean | Error,
  guardAfterRemove = false,
): RfEventStream {
  const events: RfScanEvent[] = [];
  const expectedCommandFailures = new Map<string, string>();
  let wake: (() => void) | undefined;
  let removed = false;
  let terminalError: Error | undefined;
  // Native callbacks are not required to preserve domain-event/terminal-event
  // order across the backend queue and the Nitro handoff. A wait must claim
  // only its matching event; otherwise an earlier, unrelated event can be
  // silently discarded and a later wait can time out on an event already
  // delivered to JavaScript.
  const consumedEventIndexes = new Set<number>();
  const failClosed = (error: Error): void => {
    if (terminalError) return;
    terminalError = error;
    wake?.();
    wake = undefined;
  };
  const listener = (event: RfScanEvent): void => {
    if (removed) {
      if (guardAfterRemove) {
        const rejection = rejectEvent?.(event);
        failClosed(rejection instanceof Error ? rejection : new Error('JS callback after stream removal'));
      }
      return;
    }
    if (terminalError) return;
    if (events.length >= maxEvents) {
      failClosed(new Error(`RF event stream exceeded ${maxEvents} events`));
      return;
    }
    const rejection = rejectEvent?.(event);
    if (rejection) {
      failClosed(rejection instanceof Error ? rejection : new Error(`previous generation event leaked:kind=${event.kind}:commandId=${event.commandId ?? 'none'}:connectionId=${event.connectionId ?? 'none'}`));
      return;
    }
    const expectedPassiveDisconnect = passiveDisconnectMode &&
      event.kind === 'connectionStateChanged' &&
      event.commandId == null &&
      passiveConnectionIds.has(event.connectionId ?? '') &&
      event.connectionState == null &&
      event.error?.category === 'connection';
    const nativeError = event.error?.platformDetails;
    const nativeCode = nativeError ? `:native=${nativeError.name ?? 'unknown'}/${nativeError.code ?? 'unknown'}` : '';
    const eventFailure = rfEventFailure(event, expectedCommandFailures) || (connectMode && event.kind === 'connectionStateChanged' && event.error && !expectedPassiveDisconnect
      ? new Error(`connectionStateChanged.err:${event.error.category}:${event.error.message}${nativeCode}`)
      : undefined);
    if (eventFailure) {
      failClosed(eventFailure);
      return;
    }
    events.push(event);
    wake?.();
    wake = undefined;
  };

  // The RF listener is deliberately installed before initialize/start dispatch.
  probe.setRfScanListener(listener);
  return {
    events,
    async waitFor(predicate, timeoutMilliseconds, label, isCancelled): Promise<RfScanEvent> {
      const deadline = Date.now() + timeoutMilliseconds;
      while (true) {
        if (terminalError) throw terminalError;
        for (let index = 0; index < events.length; index += 1) {
          if (consumedEventIndexes.has(index)) continue;
          const event = events[index];
          if (predicate(event)) {
            consumedEventIndexes.add(index);
            return event;
          }
        }
        if (isCancelled()) throw new Error(`${label} cancelled`);
        const remaining = deadline - Date.now();
        if (remaining <= 0) throw new Error(`${label} timed out after ${timeoutMilliseconds}ms`);
        await Promise.race([
          delay(Math.min(remaining, 100)),
          new Promise<void>((resolve) => { wake = resolve; }),
        ]);
      }
    },
    waitForNext(timeoutMilliseconds, label, isCancelled): Promise<RfScanEvent> {
      return this.waitFor(() => true, timeoutMilliseconds, label, isCancelled);
    },
    expectCommandFailure(commandId: string, category: string): void {
      expectedCommandFailures.set(commandId, category);
    },
    consumeAll(predicate: (event: RfScanEvent) => boolean): number {
      let consumed = 0;
      for (let index = 0; index < events.length; index += 1) {
        if (!consumedEventIndexes.has(index) && predicate(events[index])) {
          consumedEventIndexes.add(index);
          consumed += 1;
        }
      }
      return consumed;
    },
    assertHealthy(): void {
      if (terminalError) throw terminalError;
    },
    remove(): void {
      if (removed) return;
      removed = true;
      probe.removeRfScanListener();
    },
  };
}

function stepState(line: string): { id: StepId; state: StepState; detail: string } | undefined {
  if (line.startsWith('round-trip:')) {
    return { id: 'round-trip', state: line === 'round-trip:passed' ? 'passed' : 'failed', detail: line };
  }
  if (line.startsWith('dispatch:')) {
    return { id: 'dispatch', state: line === 'dispatch:accepted' ? 'passed' : 'failed', detail: line };
  }
  if (line.startsWith('completion:')) {
    return {
      id: 'completion',
      state: line === `completion:${MAX_U64}:ok` ? 'passed' : 'failed',
      detail: line,
    };
  }
  if (line.startsWith('listener-removal:')) {
    return { id: 'listener-removal', state: line === 'listener-removal:passed' ? 'passed' : 'failed', detail: line };
  }
  if (line.startsWith('close:')) {
    return { id: 'close', state: line === 'close:resolved' ? 'passed' : 'failed', detail: line };
  }
  return undefined;
}

async function runCloseDuringCompletionCallback(
  report: (line: string) => void,
  isCancelled: () => boolean,
  setCurrentProbe: (probe: Probe | undefined) => void,
): Promise<boolean> {
  const commandId = '40000';
  const probe = createBleHubProbe();
  setCurrentProbe(probe);
  let closePromise: Promise<void> | undefined;
  let resolveCompletion: (() => void) | undefined;
  const completion = new Promise<void>((resolve) => {
    resolveCompletion = resolve;
  });

  try {
    probe.setCompletionListener((event) => {
      if (event.commandId !== commandId) return;
      // Exercise the real JS -> Nitro close path while a native completion callback
      // is being delivered. The callback must not synchronously deadlock native cleanup.
      closePromise = probe.close();
      resolveCompletion?.();
    });
    const dispatchError = probe.initialize(commandId);
    if (dispatchError) throw new Error(`callback-close dispatch rejected: ${dispatchError.category}`);
    const completed = await Promise.race([
      completion.then(() => true),
      delay(COMPLETION_TIMEOUT_MS).then(() => false),
    ]);
    if (!completed || !closePromise) throw new Error('callback-close completion timed out');
    await closePromise;
    const postCloseError = probe.initialize('40001');
    if (postCloseError?.category !== 'closed') {
      throw new Error(`callback-close post-close dispatch was ${postCloseError?.category ?? 'accepted'}`);
    }
    report('stress:close-during-callback:passed');
    return true;
  } catch (error: unknown) {
    if (!isCancelled()) report(`stress:close-during-callback:failed:${describeError(error)}`);
    return false;
  } finally {
    setCurrentProbe(undefined);
    await probe.close().catch(() => undefined);
  }
}

async function runLifecycleStress(
  report: (line: string) => void,
  isCancelled: () => boolean,
  setCurrentProbe: (probe: Probe | undefined) => void,
): Promise<void> {
  report(`stress:start:${STRESS_CYCLES}`);
  let recreateClosePassed = true;
  let lateCallbackPassed = true;
  let postCloseDispatchPassed = true;
  const closeDuringCallbackPassed = await runCloseDuringCompletionCallback(
    report,
    isCancelled,
    setCurrentProbe,
  );

  for (let cycle = 0; cycle < STRESS_CYCLES && !isCancelled(); cycle += 1) {
    const commandId = String(10_000 + cycle);
    const probe = createBleHubProbe();
    setCurrentProbe(probe);
    let completionCount = 0;
    let resolveCompletion: (() => void) | undefined;
    const completion = new Promise<void>((resolve) => {
      resolveCompletion = resolve;
    });

    try {
      const roundTrip = probe.roundTrip({ id: `stress-${cycle}`, bytes: SAMPLE_BYTES.buffer });
      if (roundTrip.id !== `stress-${cycle}` || !sameBytes(roundTrip.bytes, SAMPLE_BYTES)) {
        throw new Error('ArrayBuffer round-trip failed');
      }

      probe.setCompletionListener((event) => {
        completionCount += 1;
        if (event.commandId === commandId) resolveCompletion?.();
      });
      const dispatchError = probe.initialize(commandId);
      if (dispatchError) throw new Error(`dispatch rejected: ${dispatchError.category}`);

      const completed = await Promise.race([
        completion.then(() => true),
        delay(COMPLETION_TIMEOUT_MS).then(() => false),
      ]);
      if (!completed) throw new Error('completion timed out');

      probe.removeCompletionListener();
      const countAfterListenerRemoval = completionCount;
      const removedListenerDispatchError = probe.initialize(String(20_000 + cycle));
      if (removedListenerDispatchError && removedListenerDispatchError.category !== 'invalid_state') {
        throw new Error(`post-removal dispatch rejected: ${removedListenerDispatchError.category}`);
      }
      await delay(STRESS_WAIT_MS);
      if (completionCount !== countAfterListenerRemoval) {
        throw new Error('completion delivered after listener removal');
      }

      await Promise.all([probe.close(), probe.close()]);
      await delay(STRESS_WAIT_MS);
      if (completionCount !== countAfterListenerRemoval) {
        lateCallbackPassed = false;
        throw new Error('late completion delivered after close resolved');
      }

      const postCloseError = probe.initialize(String(30_000 + cycle));
      if (postCloseError?.category !== 'closed') {
        postCloseDispatchPassed = false;
        throw new Error(`post-close dispatch was ${postCloseError?.category ?? 'accepted'}`);
      }

      report(`stress:cycle:${cycle}:passed`);
    } catch (error: unknown) {
      recreateClosePassed = false;
      report(`stress:cycle:${cycle}:failed:${describeError(error)}`);
    } finally {
      setCurrentProbe(undefined);
      await probe.close().catch(() => undefined);
    }
  }

  if (isCancelled()) return;
  report(`stress:recreate-close:${recreateClosePassed ? 'passed' : 'failed'}`);
  report(`stress:late-callback:${lateCallbackPassed ? 'passed' : 'failed'}`);
  report(`stress:post-close-dispatch:${postCloseDispatchPassed ? 'passed' : 'failed'}`);
  report(`stress:complete:${closeDuringCallbackPassed && recreateClosePassed && lateCallbackPassed && postCloseDispatchPassed ? 'passed' : 'failed'}`);
}

async function runReloadProbe(
  probe: Probe,
  report: (line: string) => void,
): Promise<void> {
  report('reload:start');

  const roundTrip = probe.roundTrip({ id: 'reload', bytes: SAMPLE_BYTES.buffer });
  const roundTripPassed = roundTrip.id === 'reload' && sameBytes(roundTrip.bytes, SAMPLE_BYTES);
  report(`reload:round-trip:${roundTripPassed ? 'passed' : 'failed'}`);
  if (!roundTripPassed) return;

  const completion = new Promise<void>((resolve) => {
    probe.setCompletionListener((event) => {
      report(`reload:completion:${event.commandId}:${event.error?.category ?? 'ok'}`);
      if (event.commandId === MAX_U64) resolve();
    });
  });
  const dispatchError = probe.initialize(MAX_U64);
  report(`reload:dispatch:${dispatchError ? `rejected:${dispatchError.category}` : 'accepted'}`);
  if (dispatchError) return;

  const completed = await Promise.race([
    completion.then(() => true),
    delay(COMPLETION_TIMEOUT_MS).then(() => false),
  ]);
  if (!completed) {
    report('reload:completion:failed:timeout');
    return;
  }

  // Keep this initialized probe and its listener active. The native reload gate
  // invalidates the JS runtime after this marker, so React cleanup must tear down
  // this generation before the restarted surface creates the next probe.
  report('reload:ready');
}

async function runContractSmoke(
  probe: Probe,
  report: (line: string) => void,
): Promise<void> {
  let completionCount = 0;

  try {
    const commandIdValidationPassed = INVALID_COMMAND_IDS.every(
      (commandId) => probe.initialize(commandId)?.category === 'invalid_request',
    );
    report(`command-id-validation:${commandIdValidationPassed ? 'passed' : 'failed'}`);
    if (!commandIdValidationPassed) return;

    const sampleError: EventError = {
      category: 'gatt',
      message: 'synthetic transport error',
    };
    const roundTrip = probe.roundTrip({
      id: 'bytes',
      bytes: SAMPLE_BYTES.buffer,
      error: sampleError,
    });
    const roundTripPassed =
      roundTrip.id === 'bytes' &&
      sameBytes(roundTrip.bytes, SAMPLE_BYTES) &&
      roundTrip.error?.category === sampleError.category &&
      roundTrip.error.message === sampleError.message;
    report(`round-trip:${roundTripPassed ? 'passed' : 'failed'}`);

    const completion = new Promise<void>((resolve) => {
      probe.setCompletionListener((event) => {
        completionCount += 1;
        report(`completion:${event.commandId}:${event.error?.category ?? 'ok'}`);
        if (event.commandId === MAX_U64 && completionCount === 1) resolve();
      });
    });

    const dispatchError = probe.initialize(MAX_U64);
    report(`dispatch:${dispatchError ? `rejected:${dispatchError.category}` : 'accepted'}`);
    if (dispatchError) return;

    const completed = await Promise.race([
      completion.then(() => true),
      delay(COMPLETION_TIMEOUT_MS).then(() => false),
    ]);
    if (!completed) {
      report('completion:failed:timeout');
      return;
    }

    probe.removeCompletionListener();
    report('listener-removed:dispatched');
    const removedListenerDispatchError = probe.initialize(LISTENER_REMOVAL_COMMAND);
    report(
      `listener-removal-dispatch:${removedListenerDispatchError ? removedListenerDispatchError.category : 'accepted'}`,
    );
    await delay(LISTENER_REMOVAL_WAIT_MS);
    report(`listener-removal:${completionCount === 1 ? 'passed' : 'failed'}`);
  } finally {
    probe.removeCompletionListener();
    await probe.close().then(
      () => report('close:resolved'),
      (error: unknown) => report(`close:rejected:${describeError(error)}`),
    );
  }
}

async function expectRfConnectionEvent(
  stream: RfEventStream,
  kind: string,
  commandId: string,
  connectionState: string | undefined,
  deadline: number,
  isCancelled: () => boolean,
  expectedConnectionId = RF_CONNECTION_ID,
): Promise<RfScanEvent> {
  const label = connectionState ?? `${kind} ${commandId}`;
  while (true) {
    const event = await stream.waitForNext(Math.max(1, deadline - Date.now()), label, isCancelled);
    if (kind === 'connectionStateChanged' && connectionState === 'Disconnected' &&
        event.kind === kind && event.commandId === commandId &&
        event.connectionId === expectedConnectionId && event.connectionState === 'Disconnecting' &&
        !event.error) {
      // Disconnect is a two-phase native lifecycle: accept the request, then wait
      // for the terminal callback instead of mistaking Disconnecting for success.
      continue;
    }
    if (event.kind !== kind || event.commandId !== commandId ||
        (connectionState !== undefined && (event.connectionId !== expectedConnectionId || event.connectionState !== connectionState)) ||
        event.error) {
      throw new Error(`expected ${kind}:${commandId}:${RF_CONNECTION_ID}:${connectionState ?? 'success'}, got ${event.kind}:${event.commandId ?? 'missing'}:${event.connectionId ?? 'missing'}:${event.connectionState ?? 'missing'}:${event.error?.message ?? 'no error'}`);
    }
    return event;
  }
}

// Each cycle owns a disjoint range, including scan, GATT and recovery IDs.
function recoveryId(cycle: number, offset: number): string {
  return String(10_000 + cycle * 100 + offset);
}

// Separate private RF gate: generation is owned by the native host across JS engine reloads.
// ID ranges are disjoint; a late event from generation 1 is never eligible for generation 2.
export async function runRfPendingReadReload(
  probe: Probe,
  rfServiceUuid: string | undefined,
  rfReloadNotificationName: string | undefined,
  generation: number | undefined,
  report: (line: string) => void,
  isCancelled: () => boolean,
  runId?: string,
  expectedLocalName?: string,
): Promise<boolean> {
  const prefix = `rf-pending-read-reload:generation=${generation}`;
  const android = Platform.OS === 'android';
  let stream: RfEventStream | undefined;
  let armSubscription: ReturnType<typeof Linking.addEventListener> | undefined;
  let armAwaiting = false;
  let armReceived = false;
  let armFailure: Error | undefined;
  let eventFailureBeforeCancel: Error | undefined;
  try {
    if (generation !== 1 && generation !== 2) throw new Error(`invalid generation ${generation}`);
    const serviceUuid = rfServiceUuid?.trim();
    if (!serviceUuid) throw new Error('rfServiceUuid is required');
    const notificationToken = /^dev\.blehub\.rn\.reload\.([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$/.exec(rfReloadNotificationName ?? '')?.[1];
    if (!notificationToken) throw new Error('rfReloadNotificationName must contain a canonical UUID');
    const localName = expectedLocalName;
    {
      if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(runId ?? '')) throw new Error('rfPendingReadRunId must be a canonical UUID');
      if (notificationToken !== runId) throw new Error('rfReloadNotificationName does not match rfPendingReadRunId');
      if ((android && !localName?.trim()) || (localName !== undefined && !localName.trim())) throw new Error('rfExpectedLocalName cannot be blank');
      const attempt = `${runId}:${generation}`;
      if (pendingReadRuntimeAttempts.has(attempt)) throw new Error('generation cannot rerun in the same JS runtime');
      pendingReadRuntimeAttempts.add(attempt);
    }
    const expectedArmURL = `blehub-rn-spike://arm/${notificationToken.toLowerCase()}`;
    {
      armSubscription = Linking.addEventListener('url', ({ url }) => {
        if (armFailure) return;
        if (!armAwaiting) armFailure = new Error('arm URL before awaiting-arm');
        else if (armReceived) armFailure = new Error('duplicate arm URL');
        else if (url !== expectedArmURL) armFailure = new Error('unexpected arm URL');
        else {
          armReceived = true;
          report(`${prefix}:arm-js-observed:source=linking:url=matched`);
        }
      });
    }
    const base = generation === 1 ? 12000 : 13000;
    const id = (offset: number): string => String(base + offset);
    // iOS uses the maintained pending-read route; Android's native ABI/test IDs are unchanged.
    const gattSession = id(android ? 6 : 8);
    const discoveryCommand = id(android ? 7 : 9);
    const readCommand = id(android ? 8 : 11);
    const stale = (value: string | undefined): boolean => value !== undefined &&
      /^120[0-9]{2}$/.test(value);
    stream = createRfEventStream(probe, true, false, undefined, MAX_RF_EVENT_COUNT, (event) => {
      if (event.error && !isCancelled()) {
        eventFailureBeforeCancel = new Error(`${event.kind}.error:${event.commandId ?? 'none'}:${event.error.category}:${event.error.message}`);
        return eventFailureBeforeCancel;
      }
      return generation === 2 && (stale(event.commandId) || stale(event.scanSessionId) ||
        stale(event.connectionId) || stale(event.gattSessionId) || stale(event.subscriptionId));
    });
    // Both routes must already be guarded when the host first observes this boot.
    report(`${prefix}:runtime-start:runId=${runId}:nonce=${PENDING_READ_RUNTIME_NONCE}`);
    if (armSubscription) report(`${prefix}:arm-linking-listener:registered-before-initialize`);
    report(`${prefix}:listener:registered-before-initialize`);
    {
      stream.assertHealthy();
      if (armFailure) throw armFailure;
      await probe.awaitPendingReloadReady(runId!, generation);
      stream.assertHealthy();
      if (armFailure) throw armFailure;
      if (isCancelled()) throw new Error('native readiness cancelled');
      report(`${prefix}:native-ready:passed`);
    }
    if (probe.initialize(id(0))) throw new Error('initialize dispatch rejected');
    const initDeadline = Date.now() + RF_INITIALIZATION_TIMEOUT_MS;
    await stream.waitFor((e) => e.kind === 'adapterSnapshotChanged' && e.adapterState === 'PoweredOn' && e.scanAuthorization === 'Authorized', RF_INITIALIZATION_TIMEOUT_MS, 'authorized adapter', isCancelled);
    await expectRfConnectionEvent(stream, 'commandCompleted', id(0), undefined, initDeadline, isCancelled);
    report(`${prefix}:initialize:passed:commandId=${id(0)}`);

    if (probe.startRfScan(id(2), id(1), serviceUuid)) throw new Error('scan dispatch rejected');
    const scanDeadline = Date.now() + RF_SCAN_TIMEOUT_MS;
    await stream.waitFor((e) => e.kind === 'scanStarted' && e.commandId === id(2) && e.scanSessionId === id(1), RF_SCAN_TIMEOUT_MS, 'scan started', isCancelled);
    await expectRfConnectionEvent(stream, 'commandCompleted', id(2), undefined, scanDeadline, isCancelled);
    const advertisement = await stream.waitFor((e) => e.kind === 'advertisementObserved' && e.scanSessionId === id(1) && (!android || e.localName === localName), Math.max(1, scanDeadline - Date.now()), 'advertisement', isCancelled);
    // The baseline provider may omit the name, but an explicit wrong name is never ignored.
    if (localName !== undefined && advertisement.localName != null && advertisement.localName !== localName) throw new Error('unexpected peripheral local name');
    const peripheral = advertisement.peripheralId;
    if (!isCanonicalU64(peripheral)) throw new Error('invalid peripheral ID');
    if (probe.stopRfScan(id(3), id(1))) throw new Error('stop scan dispatch rejected');
    const stopped = await stream.waitFor((e) => e.kind === 'scanStopped' && e.commandId === id(3) && e.scanSessionId === id(1), Math.max(1, scanDeadline - Date.now()), 'scan stopped', isCancelled);
    await expectRfConnectionEvent(stream, 'commandCompleted', id(3), undefined, scanDeadline, isCancelled);
    await delay(RF_POST_STOP_OBSERVATION_MS);
    stream.assertHealthy();
    if (stream.events.slice(stream.events.indexOf(stopped) + 1).some((e) => e.kind === 'advertisementObserved' && e.scanSessionId === id(1))) throw new Error('post-stop advertisement');
    report(`${prefix}:scan:passed:serviceUuid=${serviceUuid}:peripheralId=${peripheral}:scanSessionId=${id(1)}`);

    if (probe.connectRfPeripheral(id(5), id(4), peripheral)) throw new Error('connect dispatch rejected');
    const deadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
    await expectRfConnectionEvent(stream, 'connectionStateChanged', id(5), 'Connecting', deadline, isCancelled, id(4));
    await expectRfConnectionEvent(stream, 'connectionStateChanged', id(5), 'Connected', deadline, isCancelled, id(4));
    await expectRfConnectionEvent(stream, 'commandCompleted', id(5), undefined, deadline, isCancelled);
    report(`${prefix}:connected:passed:commandId=${id(5)}:connectionId=${id(4)}:peripheralId=${peripheral}`);
    if (probe.discoverRfGatt(discoveryCommand, id(4), gattSession)) throw new Error('GATT dispatch rejected');
    const discovered = await stream.waitFor((e) => e.kind === 'gattDiscovered' && e.commandId === discoveryCommand && e.connectionId === id(4), RF_CONNECTION_TIMEOUT_MS, 'GATT database', isCancelled);
    const database = validateRfGattDatabase(discovered, serviceUuid, id(4));
    if (database.gattSessionId !== gattSession) throw new Error('GATT session ID mismatch');
    await expectRfConnectionEvent(stream, 'commandCompleted', discoveryCommand, undefined, Date.now() + RF_CONNECTION_TIMEOUT_MS, isCancelled);
    report(`${prefix}:gatt:passed:commandId=${discoveryCommand}:connectionId=${id(4)}:gattSessionId=${gattSession}`);

    const characteristic = database.readWriteCharacteristicId;
    {
      if (armFailure) throw armFailure;
      armAwaiting = true;
      report(`${prefix}:awaiting-arm:commandId=${readCommand}:connectionId=${id(4)}:gattSessionId=${gattSession}:characteristicId=${characteristic}`);
      const armDeadline = Date.now() + PENDING_READ_ARM_TIMEOUT_MS;
      while (!armReceived) {
        if (isCancelled()) throw new Error('read arm cancelled');
        if (armFailure) throw armFailure;
        stream.assertHealthy();
        if (Date.now() >= armDeadline) throw new Error(`read arm timed out after ${PENDING_READ_ARM_TIMEOUT_MS}ms`);
        await delay(Math.min(100, Math.max(1, armDeadline - Date.now())));
      }
      if (isCancelled()) throw new Error('read arm cancelled');
      if (armFailure) throw armFailure;
      stream.assertHealthy();
    }
    const dispatchError = probe.readRfCharacteristic(readCommand, id(4), gattSession, characteristic);
    if (dispatchError) throw new Error(`read dispatch rejected: ${dispatchError.category}:${dispatchError.detail ?? ''}`);
    if (generation === 1) {
      // The host must receive this marker before the external reload notification.
      // Do not consume the read callback or command terminal in this generation.
      report(`${prefix}:read-dispatched:commandId=${readCommand}:connectionId=${id(4)}:gattSessionId=${gattSession}:characteristicId=${characteristic}:peripheralId=${peripheral}`);
      while (true) {
        stream.assertHealthy();
        if (armFailure) throw armFailure;
        if (isCancelled()) break;
        await delay(100);
      }
      return false;
    } else {
      report(`${prefix}:read-dispatched:commandId=${readCommand}:connectionId=${id(4)}:gattSessionId=${gattSession}:characteristicId=${characteristic}`);
      const readDeadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
      const read = await stream.waitForNext(Math.max(1, readDeadline - Date.now()), 'characteristic read', isCancelled);
      if (read.kind !== 'characteristicRead' || read.commandId !== readCommand || read.connectionId !== id(4) ||
          read.gattSessionId !== gattSession || read.characteristicId !== characteristic || read.error ||
          !sameBytes(read.value ?? new ArrayBuffer(0), new Uint8Array([0x00]))) {
        throw new Error(`unexpected generation 2 read:kind=${read.kind}:commandId=${read.commandId ?? 'none'}`);
      }
      await expectRfConnectionEvent(stream, 'commandCompleted', readCommand, undefined, readDeadline, isCancelled);
      // Keep the second listener alive briefly to catch a late first-generation callback.
      const settledEventCount = stream.events.length;
      await delay(RF_POST_DISCONNECT_OBSERVATION_MS);
      stream.assertHealthy();
      if (isCancelled()) throw new Error('read cancelled');
      if (stream.events.length !== settledEventCount) throw new Error('generation 2 post-read silence violated');
      if (armFailure) throw armFailure;
      report(`${prefix}:read:passed:commandId=${readCommand}:connectionId=${id(4)}:gattSessionId=${gattSession}:characteristicId=${characteristic}:value=00`);
      return true;
    }
  } catch (error: unknown) {
    if (eventFailureBeforeCancel || !isCancelled()) report(`${prefix}:failed:${describeError(eventFailureBeforeCancel ?? error)}`);
    return false;
  } finally {
    armSubscription?.remove();
    stream?.remove();
  }
}

async function runRfReload(
  probe: Probe,
  rfServiceUuid: string | undefined,
  report: (line: string) => void,
  isCancelled: () => boolean,
): Promise<void> {
  let stream: RfEventStream | undefined;
  try {
    const serviceUuid = rfServiceUuid?.trim();
    if (!serviceUuid) throw new Error('rfServiceUuid is required');
    stream = createRfEventStream(probe, true);
    report('rf-reload:listener:registered-before-initialize');
    if (probe.initialize('11000')) throw new Error('initialize dispatch rejected');
    await stream.waitFor((e) => e.kind === 'adapterSnapshotChanged' && e.adapterState === 'PoweredOn' && e.scanAuthorization === 'Authorized', RF_INITIALIZATION_TIMEOUT_MS, 'authorized adapter', isCancelled);
    await expectRfConnectionEvent(stream, 'commandCompleted', '11000', undefined, Date.now() + RF_INITIALIZATION_TIMEOUT_MS, isCancelled);
    report('rf-reload:initialize:passed:commandId=11000');

    if (probe.startRfScan('11002', '11001', serviceUuid)) throw new Error('scan dispatch rejected');
    const scanDeadline = Date.now() + RF_SCAN_TIMEOUT_MS;
    await stream.waitFor((e) => e.kind === 'scanStarted' && e.commandId === '11002' && e.scanSessionId === '11001', RF_SCAN_TIMEOUT_MS, 'scan started', isCancelled);
    await expectRfConnectionEvent(stream, 'commandCompleted', '11002', undefined, scanDeadline, isCancelled);
    const advertisement = await stream.waitFor((e) => e.kind === 'advertisementObserved' && e.scanSessionId === '11001' && (e.localName == null || e.localName === RF_EXPECTED_LOCAL_NAME), Math.max(1, scanDeadline - Date.now()), 'advertisement', isCancelled);
    const peripheral = advertisement.peripheralId;
    if (!isCanonicalU64(peripheral)) throw new Error('invalid peripheral ID');
    if (probe.stopRfScan('11003', '11001')) throw new Error('stop scan dispatch rejected');
    const stopped = await stream.waitFor((e) => e.kind === 'scanStopped' && e.commandId === '11003' && e.scanSessionId === '11001', Math.max(1, scanDeadline - Date.now()), 'scan stopped', isCancelled);
    await expectRfConnectionEvent(stream, 'commandCompleted', '11003', undefined, scanDeadline, isCancelled);
    await delay(RF_POST_STOP_OBSERVATION_MS);
    stream.assertHealthy();
    if (stream.events.slice(stream.events.indexOf(stopped) + 1).some((e) => e.kind === 'advertisementObserved' && e.scanSessionId === '11001')) throw new Error('post-stop advertisement');
    report(`rf-reload:scan:passed:serviceUuid=${serviceUuid}:peripheralId=${peripheral}:scanSessionId=11001`);

    if (probe.connectRfPeripheral('11005', '11004', peripheral)) throw new Error('connect dispatch rejected');
    const deadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
    await expectRfConnectionEvent(stream, 'connectionStateChanged', '11005', 'Connecting', deadline, isCancelled, '11004');
    await expectRfConnectionEvent(stream, 'connectionStateChanged', '11005', 'Connected', deadline, isCancelled, '11004');
    await expectRfConnectionEvent(stream, 'commandCompleted', '11005', undefined, deadline, isCancelled);
    report(`rf-reload:connected:passed:commandId=11005:connectionId=11004:peripheralId=${peripheral}`);
    if (probe.discoverRfGatt('11007', '11004', '11006')) throw new Error('GATT dispatch rejected');
    const discovered = await stream.waitFor((e) => e.kind === 'gattDiscovered' && e.commandId === '11007' && e.connectionId === '11004', RF_CONNECTION_TIMEOUT_MS, 'GATT database', isCancelled);
    const database = validateRfGattDatabase(discovered, serviceUuid, '11004');
    if (database.gattSessionId !== '11006') throw new Error('GATT session ID mismatch');
    await expectRfConnectionEvent(stream, 'commandCompleted', '11007', undefined, Date.now() + RF_CONNECTION_TIMEOUT_MS, isCancelled);
    report('rf-reload:gatt:passed:commandId=11007:connectionId=11004:gattSessionId=11006');
    const characteristic = database.notificationCharacteristicId;
    if (probe.setRfNotifications('11009', '11004', '11006', '11008', characteristic, true, 'platform_default')) throw new Error('notification enable rejected');
    const enabled = await stream.waitFor((e) => e.kind === 'notificationStateChanged' && e.commandId === '11009' && e.connectionId === '11004' && e.gattSessionId === '11006' && e.subscriptionId === '11008' && e.characteristicId === characteristic && e.notificationState === 'Enabled', RF_CONNECTION_TIMEOUT_MS, 'notification enabled', isCancelled);
    if (enabled.actualNotificationMode !== 'Notification') throw new Error('unexpected notification mode');
    await expectRfConnectionEvent(stream, 'commandCompleted', '11009', undefined, Date.now() + RF_CONNECTION_TIMEOUT_MS, isCancelled);
    report(`rf-reload:notification-enable:passed:commandId=11009:connectionId=11004:gattSessionId=11006:subscriptionId=11008:characteristicId=${characteristic}:actualMode=Notification`);
    const value = await stream.waitFor((e) => e.kind === 'notificationReceived' && e.connectionId === '11004' && e.gattSessionId === '11006' && e.subscriptionId === '11008' && e.characteristicId === characteristic, RF_CONNECTION_TIMEOUT_MS, 'notification value', isCancelled);
    if (!sameBytes(value.value ?? new ArrayBuffer(0), new Uint8Array([0xef]))) throw new Error('notification value mismatch');
    report('rf-reload:notification-value:passed:connectionId=11004:gattSessionId=11006:subscriptionId=11008:value=ef');
    report(`rf-reload:active:peripheralId=${peripheral}:connectionId=11004:subscriptionId=11008`);
    while (!isCancelled()) {
      await delay(100);
      stream.assertHealthy();
      if (stream.events.slice(stream.events.indexOf(value) + 1).some((e) => e.connectionId === '11004' || e.subscriptionId === '11008')) throw new Error('unexpected event while RF route active');
    }
  } catch (error: unknown) {
    if (!isCancelled()) report(`rf-reload:failed:${describeError(error)}`);
  } finally {
    stream?.remove();
    // React's effect cleanup owns close during a real engine reload.
  }
}

async function runRfRecovery(
  probe: Probe,
  rfServiceUuid: string | undefined,
  report: (line: string) => void,
  isCancelled: () => boolean,
  cycles = 3,
): Promise<void> {
  const prefix = cycles === 10 ? 'rf-recovery-ten' : 'rf-recovery';
  const serviceUuid = rfServiceUuid?.trim();
  let stream: RfEventStream | undefined;
  let failure: unknown;
  let cycle = 0;
  try {
    if (!serviceUuid) throw new Error('rfServiceUuid is required');
    stream = createRfEventStream(probe, true, true, new Set(Array.from({ length: cycles }, (_, n) => recoveryId(n, 4))), cycles === 10 ? 512 : MAX_RF_EVENT_COUNT);
    report(`${prefix}:listener:registered-before-initialize`);
    const init = recoveryId(0, 0);
    if (probe.initialize(init)) throw new Error('initialize dispatch rejected');
    await stream.waitFor((e) => e.kind === 'adapterSnapshotChanged' && e.adapterState === 'PoweredOn' && e.scanAuthorization === 'Authorized', RF_INITIALIZATION_TIMEOUT_MS, 'authorized adapter', isCancelled);
    await expectRfConnectionEvent(stream, 'commandCompleted', init, undefined, Date.now() + RF_INITIALIZATION_TIMEOUT_MS, isCancelled);
    report(`${prefix}:initialize:passed:commandId=${init}`);

    for (cycle = 0; cycle < cycles; cycle += 1) {
      const id = (offset: number): string => recoveryId(cycle, offset);
      const session = id(1);
      const connection = id(4);
      const gattSession = id(6);
      const subscription = id(8);
      const recoveryConnection = id(10);
      const step = (name: string, fields: string): void => report(`${prefix}:cycle:${cycle}:${name}:passed:${fields}`);
      const cycleStartIndex = stream.events.length;
      report(`${prefix}:cycle:${cycle}:start`);
      if (probe.startRfScan(id(2), session, serviceUuid)) throw new Error('scan dispatch rejected');
      const scanDeadline = Date.now() + RF_SCAN_TIMEOUT_MS;
      const started = await stream.waitFor((e) => e.kind === 'scanStarted' && e.commandId === id(2) && e.scanSessionId === session, RF_SCAN_TIMEOUT_MS, 'scan started', isCancelled);
      await expectRfConnectionEvent(stream, 'commandCompleted', id(2), undefined, scanDeadline, isCancelled);
      const advertisement = await stream.waitFor((e) => e.kind === 'advertisementObserved' && e.scanSessionId === session && (e.localName == null || e.localName === RF_EXPECTED_LOCAL_NAME), Math.max(1, scanDeadline - Date.now()), 'advertisement', isCancelled);
      const peripheral = advertisement.peripheralId;
      if (!isCanonicalU64(peripheral)) throw new Error('invalid peripheral ID');
      if (probe.stopRfScan(id(3), session)) throw new Error('stop scan dispatch rejected');
      const stopped = await stream.waitFor((e) => e.kind === 'scanStopped' && e.commandId === id(3) && e.scanSessionId === session && !e.error, Math.max(1, scanDeadline - Date.now()), 'scan stopped', isCancelled);
      await expectRfConnectionEvent(stream, 'commandCompleted', id(3), undefined, scanDeadline, isCancelled);
      const stopIndex = stream.events.indexOf(stopped);
      await delay(RF_POST_STOP_OBSERVATION_MS);
      stream.assertHealthy();
      if (stream.events.slice(stopIndex + 1).some((e) => e.kind === 'advertisementObserved' && e.scanSessionId === session)) throw new Error('post-stop advertisement');
      step('scan', `scanSessionId=${session}:startCommandId=${started.commandId}:stopCommandId=${id(3)}:peripheralId=${peripheral}:serviceUuid=${serviceUuid}:silenceMs=2000`);

      if (probe.connectRfPeripheral(id(5), connection, peripheral)) throw new Error('connect dispatch rejected');
      const deadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
      await expectRfConnectionEvent(stream, 'connectionStateChanged', id(5), 'Connecting', deadline, isCancelled, connection);
      await expectRfConnectionEvent(stream, 'connectionStateChanged', id(5), 'Connected', deadline, isCancelled, connection);
      await expectRfConnectionEvent(stream, 'commandCompleted', id(5), undefined, deadline, isCancelled);
      step('connected', `commandId=${id(5)}:connectionId=${connection}:peripheralId=${peripheral}`);
      if (probe.discoverRfGatt(id(7), connection, gattSession)) throw new Error('GATT dispatch rejected');
      const discovered = await stream.waitFor((e) => e.kind === 'gattDiscovered' && e.commandId === id(7) && e.connectionId === connection, RF_CONNECTION_TIMEOUT_MS, 'GATT database', isCancelled);
      const database = validateRfGattDatabase(discovered, serviceUuid, connection);
      if (database.gattSessionId !== gattSession) throw new Error('GATT session ID mismatch');
      await expectRfConnectionEvent(stream, 'commandCompleted', id(7), undefined, Date.now() + RF_CONNECTION_TIMEOUT_MS, isCancelled);
      step('gatt', `commandId=${id(7)}:connectionId=${connection}:gattSessionId=${gattSession}:serviceCount=${database.serviceCount}:characteristicCount=${database.characteristicCount}:descriptorCount=${database.descriptorCount}`);
      const characteristic = database.notificationCharacteristicId;
      if (probe.setRfNotifications(id(9), connection, gattSession, subscription, characteristic, true, 'platform_default')) throw new Error('notification enable rejected');
      const enabled = await stream.waitFor((e) => e.kind === 'notificationStateChanged' && e.commandId === id(9) && e.connectionId === connection && e.gattSessionId === gattSession && e.subscriptionId === subscription && e.characteristicId === characteristic && e.notificationState === 'Enabled', RF_CONNECTION_TIMEOUT_MS, 'notification enabled', isCancelled);
      if (enabled.actualNotificationMode !== 'Notification') throw new Error('unexpected notification mode');
      await expectRfConnectionEvent(stream, 'commandCompleted', id(9), undefined, Date.now() + RF_CONNECTION_TIMEOUT_MS, isCancelled);
      step('notification-enable', `commandId=${id(9)}:connectionId=${connection}:gattSessionId=${gattSession}:subscriptionId=${subscription}:characteristicId=${characteristic}:actualMode=Notification`);
      const value = await stream.waitFor((e) => e.kind === 'notificationReceived' && e.connectionId === connection && e.gattSessionId === gattSession && e.subscriptionId === subscription && e.characteristicId === characteristic, RF_CONNECTION_TIMEOUT_MS, 'notification value', isCancelled);
      if (!sameBytes(value.value ?? new ArrayBuffer(0), new Uint8Array([0xef]))) throw new Error('notification value mismatch');
      step('notification-value', `connectionId=${connection}:gattSessionId=${gattSession}:subscriptionId=${subscription}:value=ef`);
      const passive = await stream.waitFor((e) => e.kind === 'connectionStateChanged' && e.connectionId === connection && e.commandId == null && e.connectionState == null && e.error?.category === 'connection', RF_CONNECTION_TIMEOUT_MS, 'passive connection error', isCancelled);
      step('passive-disconnect', `commandId=none:connectionId=${connection}:error=connection`);
      const passiveIndex = stream.events.indexOf(passive);
      await delay(RF_POST_DISCONNECT_OBSERVATION_MS);
      stream.assertHealthy();
      if (stream.events.slice(passiveIndex + 1).some((e) => e.connectionId === connection || e.gattSessionId === gattSession || e.subscriptionId === subscription || e.kind === 'commandCompleted')) throw new Error('stale route after passive disconnect');
      step('old-route-silence', `connectionId=${connection}:gattSessionId=${gattSession}:subscriptionId=${subscription}:observationMs=1000`);
      // The host ACKs passive disconnect; the fixture advertises again before this bounded recovery window.
      await delay(2_000);
      if (probe.connectRfPeripheral(id(11), recoveryConnection, peripheral)) throw new Error('recovery connect rejected');
      const recoveryDeadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
      await expectRfConnectionEvent(stream, 'connectionStateChanged', id(11), 'Connecting', recoveryDeadline, isCancelled, recoveryConnection);
      await expectRfConnectionEvent(stream, 'connectionStateChanged', id(11), 'Connected', recoveryDeadline, isCancelled, recoveryConnection);
      await expectRfConnectionEvent(stream, 'commandCompleted', id(11), undefined, recoveryDeadline, isCancelled);
      step('recovery-connected', `commandId=${id(11)}:connectionId=${recoveryConnection}:peripheralId=${peripheral}`);
      if (probe.disconnectRfPeripheral(id(12), recoveryConnection)) throw new Error('recovery disconnect rejected');
      await expectRfConnectionEvent(stream, 'connectionStateChanged', id(12), 'Disconnected', recoveryDeadline, isCancelled, recoveryConnection);
      const terminal = await expectRfConnectionEvent(stream, 'commandCompleted', id(12), undefined, recoveryDeadline, isCancelled);
      const terminalIndex = stream.events.indexOf(terminal);
      await delay(RF_POST_DISCONNECT_OBSERVATION_MS);
      stream.assertHealthy();
      if (stream.events.slice(terminalIndex + 1).some((e) => e.connectionId === connection || e.connectionId === recoveryConnection || e.gattSessionId === gattSession || e.subscriptionId === subscription || e.kind === 'commandCompleted')) throw new Error('late event after recovery disconnect');
      step('recovery-disconnected', `commandId=${id(12)}:connectionId=${recoveryConnection}:observationMs=1000`);
      const cycleEvents = stream.events.slice(cycleStartIndex);
      for (const command of [2, 3, 5, 7, 9, 11, 12]) {
        if (cycleEvents.filter((event) => event.kind === 'commandCompleted' && event.commandId === id(command)).length !== 1) {
          throw new Error(`cycle ${cycle} command ${id(command)} did not terminal exactly once`);
        }
      }
      if (cycleEvents.some((event) => event.kind === 'commandCompleted' && ![2, 3, 5, 7, 9, 11, 12].some((offset) => event.commandId === id(offset))) ||
          cycleEvents.filter((event) => event.kind === 'scanStarted' && event.scanSessionId === session).length !== 1 ||
          cycleEvents.filter((event) => event.kind === 'scanStopped' && event.scanSessionId === session).length !== 1 ||
          cycleEvents.filter((event) => event.kind === 'connectionStateChanged' && event.connectionId === connection && event.commandId == null).length !== 1 ||
          cycleEvents.filter((event) => event.kind === 'notificationReceived' && event.subscriptionId === subscription).length !== 1 ||
          cycleEvents.filter((event) => event.kind === 'notificationStateChanged' && event.subscriptionId === subscription).length !== 1 ||
          cycleEvents.some((event) => event.connectionId != null && ![connection, recoveryConnection].includes(event.connectionId))) {
        throw new Error(`cycle ${cycle} contained duplicate or foreign route events`);
      }
      step('cycle', `scanSessionId=${session}:connectionId=${connection}:recoveryConnectionId=${recoveryConnection}`);
    }
  } catch (error: unknown) {
    failure = error;
    if (!isCancelled()) report(`${prefix}:cycle:${cycle}:failed:${describeError(error)}`);
  } finally {
    try {
      stream?.remove();
      await probe.close();
      if (!isCancelled()) report(`${prefix}:close:passed:listener-removed=true:probe-closed=true`);
    } catch (error: unknown) {
      failure = failure ?? error;
    }
    if (!isCancelled()) report(failure ? `${prefix}:failed:${describeError(failure)}` : `${prefix}:passed:cycles=${cycles}`);
  }
}

async function runRfScan(
  probe: Probe,
  rfServiceUuid: string | undefined,
  report: (line: string) => void,
  isCancelled: () => boolean,
  connectMode = false,
  gattMode = false,
  ioMode = false,
  subscriptionMode: RfSubscriptionMode | undefined,
  passiveDisconnectMode = false,
  rssiMode = false,
  cancellationMode = false,
  expectedLocalName = RF_EXPECTED_LOCAL_NAME,
  rfCancellationControl?: string,
): Promise<void> {
  const serviceUuid = rfServiceUuid?.trim();
  let stream: RfEventStream | undefined;
  let failure: unknown;
  let cleanupFailure: unknown;
  let currentStep: RfStepId = 'initialize';
  let matchedPeripheralId = 'unknown';
  let payloadAdvertisedServices = 'none';
  let payloadContainsFilterUuid = false;
  let connectionEvidencePassed = false;
  let gattEvidencePassed = false;
  let rssiEvidencePassed = false;
  let rssiValue: number | undefined;
  let ioEvidencePassed = false;
  let subscriptionEvidencePassed = false;
  let cancellationEvidencePassed = false;
  let barrier: RfSignalBarrier | undefined;
  const cancellationRecords: RfScanEvent[] = [];
  const cancellationExpected = new Map<string, { domain: boolean; terminal: boolean }>();
  let releaseRequested = false;
  let cancellationCharacteristic: string | undefined;

  report(
    `rf:start:serviceUuid=${serviceUuid ?? 'missing'}:expectedLocalName=${expectedLocalName}:initCommandId=${RF_INITIALIZE_COMMAND}:startCommandId=${RF_START_COMMAND}:stopCommandId=${RF_STOP_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`,
  );

  try {
    if (!serviceUuid) throw new Error('rfServiceUuid is required for RF mode');
    if (cancellationMode) barrier = new RfSignalBarrier(parseRfControl(rfCancellationControl), report);
    stream = createRfEventStream(probe, connectMode, passiveDisconnectMode, undefined, MAX_RF_EVENT_COUNT,
      cancellationMode ? (event) => {
        try { barrier?.assertHealthy(); } catch { return true; }
        const id = event.commandId;
        if (!id || ![RF_CANCELLATION_ACTIVE_COMMAND, RF_CANCELLATION_QUEUED_COMMAND, RF_CANCELLATION_RECOVERY_COMMAND].includes(id)) return false;
        const state = cancellationExpected.get(id);
        if (!state || state.terminal) return true;
        if (event.kind === 'commandCompleted') {
          if (event.error?.category !== (id === RF_CANCELLATION_QUEUED_COMMAND ? 'cancelled' : undefined) ||
              (id !== RF_CANCELLATION_QUEUED_COMMAND && !state.domain)) return true;
          state.terminal = true;
        } else {
          if (event.kind !== 'characteristicRead' || id === RF_CANCELLATION_QUEUED_COMMAND || state.domain ||
              !releaseRequested || event.connectionId !== RF_CONNECTION_ID || event.gattSessionId !== RF_GATT_SESSION || event.characteristicId !== cancellationCharacteristic ||
              !sameBytes(event.value ?? new ArrayBuffer(0), new Uint8Array([0]))) return true;
          state.domain = true;
        }
        cancellationRecords.push(event);
        return false;
      } : undefined);
    report('rf:listener:registered-before-initialize');

    const initializeError = probe.initialize(RF_INITIALIZE_COMMAND);
    if (initializeError) {
      throw new Error(`initialize dispatch rejected: ${initializeError.category}:${initializeError.detail ?? ''}`);
    }

    const initializeDeadline = Date.now() + RF_INITIALIZATION_TIMEOUT_MS;
    const adapterEvent = await stream.waitFor(
      (event) =>
        event.kind === 'adapterSnapshotChanged' &&
        event.adapterState === 'PoweredOn' &&
        event.scanAuthorization === 'Authorized',
      Math.max(1, initializeDeadline - Date.now()),
      'adapter PoweredOn + Authorized',
      isCancelled,
    );
    const initializeCompletion = await stream.waitFor(
      (event) => event.kind === 'commandCompleted' && event.commandId === RF_INITIALIZE_COMMAND,
      Math.max(1, initializeDeadline - Date.now()),
      'initialize completion',
      isCancelled,
    );
    if (initializeCompletion.error) {
      throw new Error(`initialize completion failed: ${initializeCompletion.error.category}:${initializeCompletion.error.message}`);
    }
    report(
      `rf:step:initialize:passed:commandId=${RF_INITIALIZE_COMMAND}:adapterState=${adapterEvent.adapterState}:scanAuthorization=${adapterEvent.scanAuthorization}`,
    );

    currentStep = 'scan-start';
    stream.assertHealthy();
    const scanDeadline = Date.now() + RF_SCAN_TIMEOUT_MS;
    const startError = probe.startRfScan(RF_START_COMMAND, RF_SCAN_SESSION, serviceUuid);
    if (startError) {
      throw new Error(`start scan dispatch rejected: ${startError.category}:${startError.detail ?? ''}`);
    }
    const started = await stream.waitFor(
      (event) =>
        event.kind === 'scanStarted' &&
        event.commandId === RF_START_COMMAND &&
        event.scanSessionId === RF_SCAN_SESSION,
      Math.max(1, scanDeadline - Date.now()),
      'scanStarted',
      isCancelled,
    );
    const startCompletion = await stream.waitFor(
      (event) => event.kind === 'commandCompleted' && event.commandId === RF_START_COMMAND,
      Math.max(1, scanDeadline - Date.now()),
      'scan start completion',
      isCancelled,
    );
    if (startCompletion.error) {
      throw new Error(`scan start completion failed: ${startCompletion.error.category}:${startCompletion.error.message}`);
    }
    report(
      `rf:step:scan-start:passed:startCommandId=${started.commandId}:scanSessionId=${started.scanSessionId}`,
    );

    currentStep = 'advertisement';
    const advertisement = await stream.waitFor(
      (event) =>
        event.kind === 'advertisementObserved' &&
        event.scanSessionId === RF_SCAN_SESSION &&
        (event.localName === undefined || event.localName === expectedLocalName),
      Math.max(1, scanDeadline - Date.now()),
      'matching advertisement',
      isCancelled,
    );
    if (!isCanonicalU64(advertisement.peripheralId)) {
      throw new Error(`advertisement peripheralId is not canonical u64: ${advertisement.peripheralId ?? 'missing'}`);
    }
    matchedPeripheralId = advertisement.peripheralId;
    payloadAdvertisedServices = advertisement.advertisedServices?.join(',') || 'none';
    payloadContainsFilterUuid = advertisement.advertisedServices?.includes(serviceUuid) === true;
    report(
      `rf:step:advertisement:passed:scanSessionId=${advertisement.scanSessionId}:peripheralId=${matchedPeripheralId}:localName=${advertisement.localName ?? 'missing'}:filterServiceUuid=${serviceUuid}:payloadAdvertisedServices=${payloadAdvertisedServices}:payloadContainsFilterUuid=${payloadContainsFilterUuid}`,
    );

    currentStep = 'scan-stop';
    stream.assertHealthy();
    const stopError = probe.stopRfScan(RF_STOP_COMMAND, RF_SCAN_SESSION);
    if (stopError) {
      throw new Error(`stop scan dispatch rejected: ${stopError.category}:${stopError.detail ?? ''}`);
    }
    const stopped = await stream.waitFor(
      (event) => event.kind === 'scanStopped' && event.scanSessionId === RF_SCAN_SESSION,
      Math.max(1, scanDeadline - Date.now()),
      'scanStopped',
      isCancelled,
    );
    if (stopped.commandId !== RF_STOP_COMMAND || stopped.error) {
      throw new Error(
        `scanStopped was not requested-success: commandId=${stopped.commandId ?? 'missing'}:${stopped.error?.message ?? 'no error'}`,
      );
    }
    const stopCompletion = await stream.waitFor(
      (event) => event.kind === 'commandCompleted' && event.commandId === RF_STOP_COMMAND,
      Math.max(1, scanDeadline - Date.now()),
      'scan stop completion',
      isCancelled,
    );
    if (stopCompletion.error) {
      throw new Error(`scan stop completion failed: ${stopCompletion.error.category}:${stopCompletion.error.message}`);
    }
    report(
      `rf:step:scan-stop:passed:stopCommandId=${RF_STOP_COMMAND}:scanSessionId=${RF_SCAN_SESSION}:requested=true`,
    );

    currentStep = 'silence';
    const stopIndex = stream.events.indexOf(stopped);
    await delay(RF_POST_STOP_OBSERVATION_MS);
    stream.assertHealthy();
    const lateAdvertisement = stream.events.slice(stopIndex + 1).find(
      (event) => event.kind === 'advertisementObserved' && event.scanSessionId === RF_SCAN_SESSION,
    );
    if (lateAdvertisement) {
      throw new Error('same scanSessionId advertisement arrived after scanStopped');
    }
    report(
      `rf:step:silence:passed:scanSessionId=${RF_SCAN_SESSION}:observationMs=${RF_POST_STOP_OBSERVATION_MS}`,
    );

    if (connectMode) {
      // The scan waits filter events, so audit their entire history before the
      // connection waits start consuming the next event strictly in order.
      const scanCompletions = new Set<string>();
      let scanStartedCount = 0;
      let scanStoppedCount = 0;
      for (const event of stream.events) {
        if (event.kind === 'connectionStateChanged') {
          throw new Error(`connection event before connect dispatch: ${event.commandId ?? 'missing'}`);
        }
        if (event.kind === 'commandCompleted') {
          const id = event.commandId;
          if (!id || ![RF_INITIALIZE_COMMAND, RF_START_COMMAND, RF_STOP_COMMAND].includes(id) || scanCompletions.has(id)) {
            throw new Error(`unexpected or duplicate scan completion before connect: ${id ?? 'missing'}`);
          }
          scanCompletions.add(id);
        } else if (event.kind === 'scanStarted') {
          if (event.commandId !== RF_START_COMMAND || event.scanSessionId !== RF_SCAN_SESSION || ++scanStartedCount !== 1) {
            throw new Error('unexpected or duplicate scanStarted before connect');
          }
        } else if (event.kind === 'scanStopped') {
          if (event.commandId !== RF_STOP_COMMAND || event.scanSessionId !== RF_SCAN_SESSION || ++scanStoppedCount !== 1) {
            throw new Error('unexpected or duplicate scanStopped before connect');
          }
        } else if (event.kind === 'advertisementObserved' && event.scanSessionId !== RF_SCAN_SESSION) {
          throw new Error('advertisement from a different scanSessionId before connect');
        } else if (event.kind !== 'adapterSnapshotChanged' && event.kind !== 'advertisementObserved') {
          throw new Error(`unexpected event before connect: ${event.kind}`);
        }
      }
      if (scanCompletions.size !== 3 || scanStartedCount !== 1 || scanStoppedCount !== 1) {
        throw new Error('missing scan evidence before connect');
      }
      // The first matching advertisement was consumed while selecting the peripheral.
      // Consume any additional valid advertisements from the completed scan before
      // waitForNext starts the connection event sequence; otherwise a duplicate
      // advertisement can be mistaken for the first connection callback.
      stream.consumeAll((event) => event.kind === 'advertisementObserved' && event.scanSessionId === RF_SCAN_SESSION);
      currentStep = 'connecting';
      const connectDeadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
      const connectError = probe.connectRfPeripheral(RF_CONNECT_COMMAND, RF_CONNECTION_ID, matchedPeripheralId);
      if (connectError) {
        throw new Error(`connect dispatch rejected: ${connectError.category}:${connectError.detail ?? ''}`);
      }
      await expectRfConnectionEvent(stream, 'connectionStateChanged', RF_CONNECT_COMMAND, 'Connecting', connectDeadline, isCancelled);
      report(`rf:step:connecting:passed:commandId=${RF_CONNECT_COMMAND}:connectionId=${RF_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}`);

      currentStep = 'connected';
      await expectRfConnectionEvent(stream, 'connectionStateChanged', RF_CONNECT_COMMAND, 'Connected', connectDeadline, isCancelled);
      await expectRfConnectionEvent(stream, 'commandCompleted', RF_CONNECT_COMMAND, undefined, connectDeadline, isCancelled);
      report(`rf:step:connected:passed:commandId=${RF_CONNECT_COMMAND}:connectionId=${RF_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}`);

      if (rssiMode) {
        currentStep = 'rssi';
        const rssiDeadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
        stream.assertHealthy();
        const rssiDispatchError = probe.readRfRssi(RF_RSSI_COMMAND, RF_CONNECTION_ID);
        if (rssiDispatchError) throw new Error(`RSSI dispatch rejected: ${rssiDispatchError.category}:${rssiDispatchError.detail ?? ''}`);
        const reading = await stream.waitForNext(Math.max(1, rssiDeadline - Date.now()), 'connected RSSI result', isCancelled);
        if (reading.kind !== 'rssiRead' || reading.commandId !== RF_RSSI_COMMAND ||
            reading.connectionId !== RF_CONNECTION_ID || typeof reading.rssi !== 'number' ||
            !Number.isInteger(reading.rssi) || reading.rssi < -32768 || reading.rssi > 32767) {
          throw new Error(`invalid or uncorrelated RSSI result: ${reading.kind}:${reading.commandId ?? 'missing'}:${reading.connectionId ?? 'missing'}:${reading.rssi ?? 'missing'}`);
        }
        const terminal = await stream.waitForNext(Math.max(1, rssiDeadline - Date.now()), 'RSSI terminal', isCancelled);
        if (terminal.kind !== 'commandCompleted' || terminal.commandId !== RF_RSSI_COMMAND || terminal.error) {
          throw new Error(`RSSI terminal missing or failed: ${terminal.kind}:${terminal.commandId ?? 'missing'}:${terminal.error?.category ?? 'no error'}`);
        }
        rssiValue = reading.rssi;
        rssiEvidencePassed = true;
        report(`rf:step:rssi:passed:commandId=${RF_RSSI_COMMAND}:connectionId=${RF_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}:rssiDbm=${rssiValue}`);
      }

      if (gattMode) {
        currentStep = 'gatt-discovery';
        const gattDeadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
        const gattError = probe.discoverRfGatt(RF_GATT_COMMAND, RF_CONNECTION_ID, RF_GATT_SESSION);
        if (gattError) {
          throw new Error(`GATT discovery dispatch rejected: ${gattError.category}:${gattError.detail ?? ''}`);
        }
        const discovered = await stream.waitFor(
          (event) =>
            event.kind === 'gattDiscovered' &&
            event.commandId === RF_GATT_COMMAND &&
            event.connectionId === RF_CONNECTION_ID,
          Math.max(1, gattDeadline - Date.now()),
          'GATT discovery result',
          isCancelled,
        );
        const gatt = validateRfGattDatabase(discovered, serviceUuid);
        const discoveryCompletion = await stream.waitFor(
          (event) => event.kind === 'commandCompleted' && event.commandId === RF_GATT_COMMAND,
          Math.max(1, gattDeadline - Date.now()),
          'GATT discovery completion',
          isCancelled,
        );
        if (discoveryCompletion.error) {
          throw new Error(`GATT discovery completion failed: ${discoveryCompletion.error.category}:${discoveryCompletion.error.message}`);
        }
        gattEvidencePassed = true;
        report(
          `rf:step:gatt-discovery:passed:commandId=${RF_GATT_COMMAND}:connectionId=${RF_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}:gattSessionId=${gatt.gattSessionId}:requestedGattSessionId=${RF_GATT_SESSION}:serviceCount=${gatt.serviceCount}:discoveredServiceCount=${gatt.discoveredServiceCount}:characteristicCount=${gatt.characteristicCount}:descriptorCount=${gatt.descriptorCount}`,
        );

        if (cancellationMode) {
          const cancellationCharacteristicId = gatt.readWriteCharacteristicId;
          cancellationCharacteristic = cancellationCharacteristicId;
          const cancellationDeadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
          const eventStream = stream;
          if (!eventStream) throw new Error('RF event stream was unavailable during cancellation');
          const cancellationEvent = (kind: string, commandId: string): ((event: RfScanEvent) => boolean) =>
            (event) => event.kind === kind && event.commandId === commandId &&
              event.connectionId === RF_CONNECTION_ID && event.gattSessionId === RF_GATT_SESSION &&
              event.characteristicId === cancellationCharacteristicId;
          const cancellationCompletion = async (commandId: string, label: string): Promise<RfScanEvent> => {
            const event = await eventStream.waitFor(
              (candidate) => candidate.kind === 'commandCompleted' && candidate.commandId === commandId,
              Math.max(1, cancellationDeadline - Date.now()), label, isCancelled,
            );
            return event;
          };

          if (!barrier) throw new Error('missing multifunction cancellation barrier');
          barrier.expect('armed');
          barrier.ready('arm');
          await barrier.wait(isCancelled, () => eventStream.assertHealthy());
          barrier.expect('held');
          cancellationExpected.set(RF_CANCELLATION_ACTIVE_COMMAND, { domain: false, terminal: false });
          const activeDispatchError = probe.readRfCharacteristic(
            RF_CANCELLATION_ACTIVE_COMMAND, RF_CONNECTION_ID, RF_GATT_SESSION, cancellationCharacteristicId,
          );
          if (activeDispatchError) throw new Error(`cancellation active read dispatch rejected: ${activeDispatchError.category}:${activeDispatchError.detail ?? ''}`);
          currentStep = 'cancellation-active-rejection';
          barrier.ready('read-dispatched');
          await barrier.wait(isCancelled, () => eventStream.assertHealthy());
          const activeCancellationError = probe.cancelOperation(RF_CANCELLATION_ACTIVE_COMMAND);
          if (activeCancellationError?.category !== 'operation_not_cancellable') {
            throw new Error(`active cancellation returned ${activeCancellationError?.category ?? 'accepted'}`);
          }
          report(`rf:step:cancellation-active-rejection:passed:targetCommandId=${RF_CANCELLATION_ACTIVE_COMMAND}:category=${activeCancellationError.category}`);
          eventStream.expectCommandFailure(RF_CANCELLATION_QUEUED_COMMAND, 'cancelled');
          cancellationExpected.set(RF_CANCELLATION_QUEUED_COMMAND, { domain: false, terminal: false });
          const queuedDispatchError = probe.readRfCharacteristic(
            RF_CANCELLATION_QUEUED_COMMAND, RF_CONNECTION_ID, RF_GATT_SESSION, cancellationCharacteristicId,
          );
          if (queuedDispatchError) throw new Error(`cancellation queued read dispatch rejected: ${queuedDispatchError.category}:${queuedDispatchError.detail ?? ''}`);
          currentStep = 'cancellation-queued-accepted';
          const queuedCancellationError = probe.cancelOperation(RF_CANCELLATION_QUEUED_COMMAND);
          if (queuedCancellationError) {
            throw new Error(`queued cancellation rejected: ${queuedCancellationError.category}:${queuedCancellationError.detail ?? ''}`);
          }
          report(`rf:step:cancellation-queued-accepted:passed:targetCommandId=${RF_CANCELLATION_QUEUED_COMMAND}`);

          currentStep = 'cancellation-terminal';
          const queuedCompletion = await eventStream.waitFor(
            (event) => event.kind === 'commandCompleted' && event.commandId === RF_CANCELLATION_QUEUED_COMMAND,
            Math.max(1, cancellationDeadline - Date.now()), 'queued cancellation terminal', isCancelled,
          );
          if (queuedCompletion.error?.category !== 'cancelled') {
            throw new Error(`queued cancellation terminal was ${queuedCompletion.error?.category ?? 'successful'}`);
          }
          report(`rf:step:cancellation-terminal:passed:commandId=${RF_CANCELLATION_QUEUED_COMMAND}:error=${queuedCompletion.error.category}`);
          barrier.expect('released');
          releaseRequested = true;
          barrier.ready('release');
          await barrier.wait(isCancelled, () => eventStream.assertHealthy());
          const activeRead = await eventStream.waitFor(
            cancellationEvent('characteristicRead', RF_CANCELLATION_ACTIVE_COMMAND),
            Math.max(1, cancellationDeadline - Date.now()), 'active cancellation read', isCancelled,
          );
          const activeCompletion = await cancellationCompletion(RF_CANCELLATION_ACTIVE_COMMAND, 'active cancellation read terminal');
          if (activeCompletion.error || !sameBytes(activeRead.value ?? new ArrayBuffer(0), new Uint8Array([0]))) {
            throw new Error('active read did not complete with 00');
          }
          currentStep = 'cancellation-active-completion';
          report(`rf:step:cancellation-active-completion:passed:commandId=${RF_CANCELLATION_ACTIVE_COMMAND}`);

          currentStep = 'cancellation-late-silence';
          const cancelledIndex = eventStream.events.indexOf(queuedCompletion);
          await delay(RF_POST_DISCONNECT_OBSERVATION_MS);
          eventStream.assertHealthy();
          if (eventStream.events.slice(cancelledIndex + 1).some((event) => event.commandId === RF_CANCELLATION_QUEUED_COMMAND && event.kind !== 'commandCompleted')) {
            throw new Error('cancelled command emitted a late domain result');
          }
          report(`rf:step:cancellation-late-silence:passed:commandId=${RF_CANCELLATION_QUEUED_COMMAND}:observationMs=${RF_POST_DISCONNECT_OBSERVATION_MS}`);

          currentStep = 'cancellation-unknown-target';
          const unknownCancellationError = probe.cancelOperation(RF_CANCELLATION_UNKNOWN_TARGET);
          if (unknownCancellationError?.category !== 'operation_not_found') {
            throw new Error(`unknown cancellation returned ${unknownCancellationError?.category ?? 'accepted'}`);
          }
          report(`rf:step:cancellation-unknown-target:passed:targetCommandId=${RF_CANCELLATION_UNKNOWN_TARGET}:category=${unknownCancellationError.category}`);
          currentStep = 'cancellation-completed-target';
          const completedCancellationError = probe.cancelOperation(RF_CANCELLATION_ACTIVE_COMMAND);
          if (completedCancellationError?.category !== 'operation_not_found') {
            throw new Error(`completed cancellation returned ${completedCancellationError?.category ?? 'accepted'}`);
          }
          report(`rf:step:cancellation-completed-target:passed:targetCommandId=${RF_CANCELLATION_ACTIVE_COMMAND}:category=${completedCancellationError.category}`);

          cancellationExpected.set(RF_CANCELLATION_RECOVERY_COMMAND, { domain: false, terminal: false });
          const recoveryDispatchError = probe.readRfCharacteristic(
            RF_CANCELLATION_RECOVERY_COMMAND, RF_CONNECTION_ID, RF_GATT_SESSION, cancellationCharacteristicId,
          );
          if (recoveryDispatchError) throw new Error(`cancellation recovery read dispatch rejected: ${recoveryDispatchError.category}:${recoveryDispatchError.detail ?? ''}`);
          const recoveryRead = await eventStream.waitFor(
            cancellationEvent('characteristicRead', RF_CANCELLATION_RECOVERY_COMMAND),
            Math.max(1, cancellationDeadline - Date.now()), 'cancellation recovery read', isCancelled,
          );
          const recoveryCompletion = await cancellationCompletion(RF_CANCELLATION_RECOVERY_COMMAND, 'cancellation recovery terminal');
          if (recoveryCompletion.error || recoveryRead.value === undefined) {
            throw new Error(`cancellation recovery did not complete normally: ${recoveryCompletion.error?.category ?? 'missing value'}`);
          }
          currentStep = 'cancellation-recovery';
          report(`rf:step:cancellation-recovery:passed:commandId=${RF_CANCELLATION_RECOVERY_COMMAND}`);

          cancellationEvidencePassed = true;
        } else if (ioMode) {
          const ioCharacteristicId = gatt.readWriteCharacteristicId;
          const eventStream = stream;
          if (!eventStream) throw new Error('RF event stream was unavailable during I/O');
          const waitIoEvent = (
            predicate: (event: RfScanEvent) => boolean,
            label: string,
          ): Promise<RfScanEvent> => eventStream.waitFor(predicate, RF_CONNECTION_TIMEOUT_MS, label, isCancelled);
          const waitIoCompletion = async (commandId: string, label: string): Promise<RfScanEvent> => {
            const completion = await waitIoEvent(
              (event) => event.kind === 'commandCompleted' && event.commandId === commandId,
              label,
            );
            if (completion.error) throw new Error(`${label} failed: ${completion.error.category}:${completion.error.message}`);
            return completion;
          };
          const characteristicEvent = (kind: string, commandId: string): ((event: RfScanEvent) => boolean) =>
            (event) => event.kind === kind && event.commandId === commandId &&
              event.connectionId === RF_CONNECTION_ID && event.gattSessionId === RF_GATT_SESSION &&
              event.characteristicId === ioCharacteristicId;

          currentStep = 'read-initial';
          const initialReadError = probe.readRfCharacteristic(
            RF_READ_INITIAL_COMMAND, RF_CONNECTION_ID, RF_GATT_SESSION, ioCharacteristicId,
          );
          if (initialReadError) throw new Error(`initial read dispatch rejected: ${initialReadError.category}:${initialReadError.detail ?? ''}`);
          const initialRead = await waitIoEvent(
            characteristicEvent('characteristicRead', RF_READ_INITIAL_COMMAND),
            'initial characteristic read',
          );
          if (!sameBytes(initialRead.value ?? new ArrayBuffer(0), new Uint8Array([0x00]))) {
            throw new Error(`initial characteristic value mismatch: ${Array.from(new Uint8Array(initialRead.value ?? new ArrayBuffer(0))).join(',')}`);
          }
          await waitIoCompletion(RF_READ_INITIAL_COMMAND, 'initial characteristic read completion');
          report(`rf:step:read-initial:passed:commandId=${RF_READ_INITIAL_COMMAND}:connectionId=${RF_CONNECTION_ID}:gattSessionId=${RF_GATT_SESSION}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}:characteristicId=${ioCharacteristicId}:value=00`);

          currentStep = 'write-response';
          const writeResponseError = probe.writeRfCharacteristic(
            RF_WRITE_RESPONSE_COMMAND, RF_CONNECTION_ID, RF_GATT_SESSION, ioCharacteristicId,
            new Uint8Array([0x10]).buffer, 'with_response',
          );
          if (writeResponseError) throw new Error(`write-with-response dispatch rejected: ${writeResponseError.category}:${writeResponseError.detail ?? ''}`);
          await waitIoEvent(
            characteristicEvent('characteristicWritten', RF_WRITE_RESPONSE_COMMAND),
            'write with response event',
          );
          await waitIoCompletion(RF_WRITE_RESPONSE_COMMAND, 'write with response completion');
          report(`rf:step:write-response:passed:commandId=${RF_WRITE_RESPONSE_COMMAND}:connectionId=${RF_CONNECTION_ID}:gattSessionId=${RF_GATT_SESSION}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}:characteristicId=${ioCharacteristicId}:value=10`);

          currentStep = 'read-response';
          const responseReadError = probe.readRfCharacteristic(
            RF_READ_RESPONSE_COMMAND, RF_CONNECTION_ID, RF_GATT_SESSION, ioCharacteristicId,
          );
          if (responseReadError) throw new Error(`readback dispatch rejected: ${responseReadError.category}:${responseReadError.detail ?? ''}`);
          const responseRead = await waitIoEvent(
            characteristicEvent('characteristicRead', RF_READ_RESPONSE_COMMAND),
            'readback after write with response',
          );
          if (!sameBytes(responseRead.value ?? new ArrayBuffer(0), new Uint8Array([0x10]))) {
            throw new Error(`write-with-response readback mismatch: ${Array.from(new Uint8Array(responseRead.value ?? new ArrayBuffer(0))).join(',')}`);
          }
          await waitIoCompletion(RF_READ_RESPONSE_COMMAND, 'readback after write with response completion');
          report(`rf:step:read-response:passed:commandId=${RF_READ_RESPONSE_COMMAND}:connectionId=${RF_CONNECTION_ID}:gattSessionId=${RF_GATT_SESSION}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}:characteristicId=${ioCharacteristicId}:value=10`);

          currentStep = 'write-without-response';
          const writeWithoutResponseError = probe.writeRfCharacteristic(
            RF_WRITE_WITHOUT_RESPONSE_COMMAND, RF_CONNECTION_ID, RF_GATT_SESSION, ioCharacteristicId,
            new Uint8Array([0xab]).buffer, 'without_response',
          );
          if (writeWithoutResponseError) throw new Error(`write-without-response dispatch rejected: ${writeWithoutResponseError.category}:${writeWithoutResponseError.detail ?? ''}`);
          // No-response Written + terminal confirm only local/native submission,
          // not a native callback or remote ACK; readback is separate fixture-side evidence.
          await waitIoEvent(
            characteristicEvent('characteristicWritten', RF_WRITE_WITHOUT_RESPONSE_COMMAND),
            'write without response event',
          );
          await waitIoCompletion(RF_WRITE_WITHOUT_RESPONSE_COMMAND, 'write without response submission');
          report(`rf:step:write-without-response:passed:commandId=${RF_WRITE_WITHOUT_RESPONSE_COMMAND}:connectionId=${RF_CONNECTION_ID}:gattSessionId=${RF_GATT_SESSION}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}:characteristicId=${ioCharacteristicId}:value=ab:terminal=local-submission`);

          currentStep = 'read-without-response';
          const withoutResponseReadError = probe.readRfCharacteristic(
            RF_READ_WITHOUT_RESPONSE_COMMAND, RF_CONNECTION_ID, RF_GATT_SESSION, ioCharacteristicId,
          );
          if (withoutResponseReadError) throw new Error(`no-response readback dispatch rejected: ${withoutResponseReadError.category}:${withoutResponseReadError.detail ?? ''}`);
          const withoutResponseRead = await waitIoEvent(
            characteristicEvent('characteristicRead', RF_READ_WITHOUT_RESPONSE_COMMAND),
            'readback after write without response',
          );
          if (!sameBytes(withoutResponseRead.value ?? new ArrayBuffer(0), new Uint8Array([0xab]))) {
            throw new Error(`write-without-response readback mismatch: ${Array.from(new Uint8Array(withoutResponseRead.value ?? new ArrayBuffer(0))).join(',')}`);
          }
          await waitIoCompletion(RF_READ_WITHOUT_RESPONSE_COMMAND, 'readback after write without response completion');
          report(`rf:step:read-without-response:passed:commandId=${RF_READ_WITHOUT_RESPONSE_COMMAND}:connectionId=${RF_CONNECTION_ID}:gattSessionId=${RF_GATT_SESSION}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}:characteristicId=${ioCharacteristicId}:value=ab`);
          ioEvidencePassed = true;
        } else if (subscriptionMode) {
          const subscriptionStream = stream;
          if (!subscriptionStream) throw new Error('RF event stream was unavailable during subscription I/O');
          const indication = subscriptionMode === 'indication';
          const waitSubscriptionEvent = (
            predicate: (event: RfScanEvent) => boolean,
            label: string,
          ): Promise<RfScanEvent> => subscriptionStream.waitFor(predicate, RF_CONNECTION_TIMEOUT_MS, label, isCancelled);
          const waitSubscriptionCompletion = async (commandId: string, label: string): Promise<RfScanEvent> => {
            const completion = await waitSubscriptionEvent(
              (event) => event.kind === 'commandCompleted' && event.commandId === commandId,
              label,
            );
            if (completion.error) throw new Error(`${label} failed: ${completion.error.category}:${completion.error.message}`);
            return completion;
          };

          if (indication) {
            const dualPropertyCharacteristicId = gatt.notificationCharacteristicId;
            subscriptionStream.expectCommandFailure(RF_INDICATION_REJECTION_COMMAND, 'not_supported');
            currentStep = 'indication-rejection';
            const rejectionError = probe.setRfNotifications(
              RF_INDICATION_REJECTION_COMMAND,
              RF_CONNECTION_ID,
              RF_GATT_SESSION,
              RF_INDICATION_REJECTED_SUBSCRIPTION,
              dualPropertyCharacteristicId,
              true,
              'require_indication',
            );
            if (rejectionError) throw new Error(`strict Indication rejection dispatch failed: ${rejectionError.category}:${rejectionError.detail ?? ''}`);
            const rejection = await waitSubscriptionEvent(
              (event) => event.kind === 'commandCompleted' && event.commandId === RF_INDICATION_REJECTION_COMMAND,
              'strict Indication rejection',
            );
            if (rejection.error?.category !== 'not_supported') {
              throw new Error(`strict Indication rejection returned ${rejection.error?.category ?? 'success'}`);
            }
            report(`rf:step:indication-rejection:passed:commandId=${RF_INDICATION_REJECTION_COMMAND}:connectionId=${RF_CONNECTION_ID}:gattSessionId=${RF_GATT_SESSION}:subscriptionId=${RF_INDICATION_REJECTED_SUBSCRIPTION}:characteristicId=${dualPropertyCharacteristicId}:error=${rejection.error.category}`);
          }

          const subscriptionCharacteristicId = indication ? gatt.indicationCharacteristicId : gatt.notificationCharacteristicId;
          const enableCommandId = indication ? RF_INDICATION_ENABLE_COMMAND : RF_NOTIFICATION_ENABLE_COMMAND;
          const disableCommandId = indication ? RF_INDICATION_DISABLE_COMMAND : RF_NOTIFICATION_DISABLE_COMMAND;
          const subscriptionId = indication ? RF_INDICATION_SUBSCRIPTION : RF_NOTIFICATION_SUBSCRIPTION;
          const requestedMode = indication ? 'require_indication' : 'platform_default';
          const expectedMode = indication ? 'Indication' : 'Notification';
          const stepPrefix = indication ? 'indication' : 'notification';
          currentStep = indication ? 'indication-enable' : 'notification-enable';
          const enableError = probe.setRfNotifications(
            enableCommandId,
            RF_CONNECTION_ID,
            RF_GATT_SESSION,
            subscriptionId,
            subscriptionCharacteristicId,
            true,
            requestedMode,
          );
          if (enableError) throw new Error(`${stepPrefix} enable dispatch rejected: ${enableError.category}:${enableError.detail ?? ''}`);
          const enabled = await waitSubscriptionEvent(
            (event) => event.kind === 'notificationStateChanged' &&
              event.commandId === enableCommandId &&
              event.connectionId === RF_CONNECTION_ID &&
              event.gattSessionId === RF_GATT_SESSION &&
              event.subscriptionId === subscriptionId &&
              event.characteristicId === subscriptionCharacteristicId &&
              event.notificationState === 'Enabled',
            `${stepPrefix} enable state`,
          );
          if (enabled.actualNotificationMode !== expectedMode) {
            throw new Error(`unexpected ${stepPrefix} mode: ${enabled.actualNotificationMode ?? 'missing'}`);
          }
          await waitSubscriptionCompletion(enableCommandId, `${stepPrefix} enable completion`);
          report(`rf:step:${stepPrefix}-enable:passed:commandId=${enableCommandId}:connectionId=${RF_CONNECTION_ID}:gattSessionId=${RF_GATT_SESSION}:subscriptionId=${subscriptionId}:characteristicId=${subscriptionCharacteristicId}:actualMode=${enabled.actualNotificationMode}`);

          currentStep = indication ? 'indication-value' : 'notification-value';
          const value = await waitSubscriptionEvent(
            (event) => event.kind === 'notificationReceived' &&
              event.connectionId === RF_CONNECTION_ID &&
              event.gattSessionId === RF_GATT_SESSION &&
              event.subscriptionId === subscriptionId &&
              event.characteristicId === subscriptionCharacteristicId,
            `${stepPrefix} value`,
          );
          if (!sameBytes(value.value ?? new ArrayBuffer(0), new Uint8Array([0xef]))) {
            throw new Error(`${stepPrefix} value mismatch: ${Array.from(new Uint8Array(value.value ?? new ArrayBuffer(0))).join(',')}`);
          }
          report(`rf:step:${stepPrefix}-value:passed:connectionId=${RF_CONNECTION_ID}:gattSessionId=${RF_GATT_SESSION}:subscriptionId=${subscriptionId}:characteristicId=${subscriptionCharacteristicId}:value=ef`);

          if (passiveDisconnectMode) {
            subscriptionEvidencePassed = true;
          } else {
            currentStep = indication ? 'indication-disable' : 'notification-disable';
          const disableError = probe.setRfNotifications(
            disableCommandId,
            RF_CONNECTION_ID,
            RF_GATT_SESSION,
            subscriptionId,
            subscriptionCharacteristicId,
            false,
            requestedMode,
          );
          if (disableError) throw new Error(`${stepPrefix} disable dispatch rejected: ${disableError.category}:${disableError.detail ?? ''}`);
          const disabled = await waitSubscriptionEvent(
            (event) => event.kind === 'notificationStateChanged' &&
              event.commandId === disableCommandId &&
              event.connectionId === RF_CONNECTION_ID &&
              event.gattSessionId === RF_GATT_SESSION &&
              event.subscriptionId === subscriptionId &&
              event.characteristicId === subscriptionCharacteristicId &&
              event.notificationState === 'Disabled',
            `${stepPrefix} disable state`,
          );
          const disableCompletion = await waitSubscriptionCompletion(disableCommandId, `${stepPrefix} disable completion`);
          const disableIndex = subscriptionStream.events.indexOf(disableCompletion);
          await delay(RF_POST_NOTIFICATION_DISABLE_OBSERVATION_MS);
          subscriptionStream.assertHealthy();
          if (subscriptionStream.events.slice(disableIndex + 1).some((event) =>
            event.kind === 'notificationReceived' &&
            event.connectionId === RF_CONNECTION_ID &&
            event.gattSessionId === RF_GATT_SESSION &&
            event.subscriptionId === subscriptionId &&
            event.characteristicId === subscriptionCharacteristicId)) {
            throw new Error(`${stepPrefix} value arrived after disable completion`);
          }
            subscriptionEvidencePassed = true;
            report(`rf:step:${stepPrefix}-disable:passed:commandId=${disableCommandId}:connectionId=${RF_CONNECTION_ID}:gattSessionId=${RF_GATT_SESSION}:subscriptionId=${subscriptionId}:characteristicId=${subscriptionCharacteristicId}:state=${disabled.notificationState}:postDisableSilenceMs=${RF_POST_NOTIFICATION_DISABLE_OBSERVATION_MS}`);
          }
        }
      }

      if (passiveDisconnectMode) {
        currentStep = 'disconnected';
        const passiveDisconnectDeadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
        const passiveDisconnect = await stream.waitFor(
          (event) => event.kind === 'connectionStateChanged' &&
            event.commandId == null &&
            event.connectionId === RF_CONNECTION_ID &&
            (event.connectionState === 'Disconnected' ||
              (event.connectionState == null && event.error?.category === 'connection')),
          Math.max(1, passiveDisconnectDeadline - Date.now()),
          'passive disconnect',
          isCancelled,
        );
        report(`rf:step:disconnected:passed:commandId=none:connectionId=${RF_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}`);

        currentStep = 'connection-silence';
        const passiveDisconnectIndex = stream.events.indexOf(passiveDisconnect);
        await delay(RF_POST_DISCONNECT_OBSERVATION_MS);
        stream.assertHealthy();
        if (stream.events.slice(passiveDisconnectIndex + 1).some((event) =>
          event.connectionId === RF_CONNECTION_ID ||
          event.gattSessionId === RF_GATT_SESSION ||
          event.subscriptionId === RF_NOTIFICATION_SUBSCRIPTION ||
          event.kind === 'commandCompleted')) {
          throw new Error('stale connection, GATT, subscription, or command event arrived after passive disconnect');
        }
        report(`rf:step:connection-silence:passed:connectionId=${RF_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}:observationMs=${RF_POST_DISCONNECT_OBSERVATION_MS}`);

        // The runner signals the fixture after this marker. The fixture restarts
        // advertising before the bounded delay below, allowing CoreBluetooth to
        // reuse the retained CBPeripheral for a recovery connect.
        await delay(2_000);
        const recoveryDeadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
        currentStep = 'recovery-connecting';
        const recoveryConnectError = probe.connectRfPeripheral(
          RF_RECOVERY_CONNECT_COMMAND,
          RF_RECOVERY_CONNECTION_ID,
          matchedPeripheralId,
        );
        if (recoveryConnectError) {
          throw new Error(`recovery connect dispatch rejected: ${recoveryConnectError.category}:${recoveryConnectError.detail ?? ''}`);
        }
        await expectRfConnectionEvent(
          stream, 'connectionStateChanged', RF_RECOVERY_CONNECT_COMMAND, 'Connecting',
          recoveryDeadline, isCancelled, RF_RECOVERY_CONNECTION_ID,
        );
        report(`rf:step:recovery-connecting:passed:commandId=${RF_RECOVERY_CONNECT_COMMAND}:connectionId=${RF_RECOVERY_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}`);

        currentStep = 'recovery-connected';
        await expectRfConnectionEvent(
          stream, 'connectionStateChanged', RF_RECOVERY_CONNECT_COMMAND, 'Connected',
          recoveryDeadline, isCancelled, RF_RECOVERY_CONNECTION_ID,
        );
        await expectRfConnectionEvent(
          stream, 'commandCompleted', RF_RECOVERY_CONNECT_COMMAND, undefined,
          recoveryDeadline, isCancelled, RF_RECOVERY_CONNECTION_ID,
        );
        report(`rf:step:recovery-connected:passed:commandId=${RF_RECOVERY_CONNECT_COMMAND}:connectionId=${RF_RECOVERY_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}`);

        currentStep = 'recovery-disconnected';
        const recoveryDisconnectError = probe.disconnectRfPeripheral(
          RF_RECOVERY_DISCONNECT_COMMAND,
          RF_RECOVERY_CONNECTION_ID,
        );
        if (recoveryDisconnectError) {
          throw new Error(`recovery disconnect dispatch rejected: ${recoveryDisconnectError.category}:${recoveryDisconnectError.detail ?? ''}`);
        }
        await expectRfConnectionEvent(
          stream, 'connectionStateChanged', RF_RECOVERY_DISCONNECT_COMMAND, 'Disconnected',
          recoveryDeadline, isCancelled, RF_RECOVERY_CONNECTION_ID,
        );
        const recoveryDisconnectCompletion = await expectRfConnectionEvent(
          stream, 'commandCompleted', RF_RECOVERY_DISCONNECT_COMMAND, undefined,
          recoveryDeadline, isCancelled, RF_RECOVERY_CONNECTION_ID,
        );
        report(`rf:step:recovery-disconnected:passed:commandId=${RF_RECOVERY_DISCONNECT_COMMAND}:connectionId=${RF_RECOVERY_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}`);

        currentStep = 'recovery-connection-silence';
        const recoveryDisconnectIndex = stream.events.indexOf(recoveryDisconnectCompletion);
        await delay(RF_POST_DISCONNECT_OBSERVATION_MS);
        stream.assertHealthy();
        if (stream.events.slice(recoveryDisconnectIndex + 1).some((event) =>
          event.connectionId === RF_RECOVERY_CONNECTION_ID || event.kind === 'commandCompleted')) {
          throw new Error('recovery connection or command event arrived after disconnect completion');
        }
        connectionEvidencePassed = true;
        report(`rf:step:recovery-connection-silence:passed:connectionId=${RF_RECOVERY_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}:observationMs=${RF_POST_DISCONNECT_OBSERVATION_MS}`);
      } else {
        currentStep = 'disconnected';
      const disconnectDeadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
      const disconnectError = probe.disconnectRfPeripheral(RF_DISCONNECT_COMMAND, RF_CONNECTION_ID);
      if (disconnectError) {
        throw new Error(`disconnect dispatch rejected: ${disconnectError.category}:${disconnectError.detail ?? ''}`);
      }
      await expectRfConnectionEvent(stream, 'connectionStateChanged', RF_DISCONNECT_COMMAND, 'Disconnected', disconnectDeadline, isCancelled);
      const disconnectCompletion = await expectRfConnectionEvent(
        stream, 'commandCompleted', RF_DISCONNECT_COMMAND, undefined, disconnectDeadline, isCancelled,
      );
      report(`rf:step:disconnected:passed:commandId=${RF_DISCONNECT_COMMAND}:connectionId=${RF_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}`);

      currentStep = 'connection-silence';
      const completionIndex = stream.events.indexOf(disconnectCompletion);
      await delay(RF_POST_DISCONNECT_OBSERVATION_MS);
      stream.assertHealthy();
      if (stream.events.slice(completionIndex + 1).some((event) =>
        event.kind === 'connectionStateChanged' || event.kind === 'commandCompleted' ||
        (event.scanSessionId === RF_SCAN_SESSION &&
          (event.kind === 'advertisementObserved' || event.kind === 'scanStarted' || event.kind === 'scanStopped')))) {
        throw new Error('unexpected connection, command, or same-session scan event after disconnect completion');
      }
      connectionEvidencePassed = true;
      report(`rf:step:connection-silence:passed:connectionId=${RF_CONNECTION_ID}:peripheralId=${matchedPeripheralId}:serviceUuid=${serviceUuid}:observationMs=${RF_POST_DISCONNECT_OBSERVATION_MS}`);
      }
    }
  } catch (error: unknown) {
    failure = error;
    if (!isCancelled()) {
      report(`rf:step:${currentStep}:failed:${describeError(error)}`);
    }
  } finally {
    try {
      if (!cancellationMode) {
        stream?.remove();
        if (stream) report('rf:listener:removed');
      }
    } catch (error: unknown) {
      cleanupFailure = error;
      if (!isCancelled()) report(`rf:listener-removal:failed:${describeError(error)}`);
    }

    let closeSucceeded = false;
    try {
      await probe.close();
      if (cancellationMode) {
        stream?.assertHealthy();
        barrier?.assertHealthy();
        if (cancellationEvidencePassed) {
          const records = cancellationRecords;
          const hex = (value: ArrayBuffer | undefined): string => value === undefined ? 'none' : Array.from(new Uint8Array(value)).map((byte) => byte.toString(16).padStart(2, '0')).join('');
          const field = (event: RfScanEvent, name: 'connectionId' | 'gattSessionId' | 'characteristicId'): string => event[name] ?? 'none';
          report(`rf-cancellation:event:kind=${records.map((event) => event.kind).join('|')}:commandId=${records.map((event) => event.commandId ?? 'none').join('|')}:connectionId=${records.map((event) => field(event, 'connectionId')).join('|')}:gattSessionId=${records.map((event) => field(event, 'gattSessionId')).join('|')}:characteristicId=${records.map((event) => field(event, 'characteristicId')).join('|')}:error=${records.map((event) => event.error?.category ?? 'ok').join('|')}:value=${records.map((event) => hex(event.value)).join('|')}`);
        }
        stream?.remove();
        if (stream) report('rf:listener:removed');
      }
      closeSucceeded = true;
      if (!isCancelled()) report('rf:close:resolved');
    } catch (error: unknown) {
      cleanupFailure = cleanupFailure ?? error;
      if (!isCancelled()) report(`rf:close:rejected:${describeError(error)}`);
    }
    try {
      barrier?.remove();
    } catch (error: unknown) { cleanupFailure ??= error; }
    try {
      if (cancellationMode) stream?.remove();
    } catch (error: unknown) { cleanupFailure ??= error; }
    if (!isCancelled()) {
      if (cleanupFailure || !closeSucceeded) {
        report(`rf:step:close:failed:${describeError(cleanupFailure ?? new Error('probe close did not resolve'))}`);
      } else {
        report('rf:step:close:passed:probe-closed=true:listener-removed=true');
      }
    }

    if (!isCancelled()) {
      const finalFailure = failure ?? cleanupFailure ??
        (connectMode && !connectionEvidencePassed ? new Error('missing verified connection evidence') : undefined) ??
        (gattMode && !gattEvidencePassed ? new Error('missing verified GATT discovery evidence') : undefined) ??
        (ioMode && !ioEvidencePassed ? new Error('missing verified GATT I/O evidence') : undefined) ??
        (rssiMode && !rssiEvidencePassed ? new Error('missing verified connected RSSI evidence') : undefined) ??
        (cancellationMode && !cancellationEvidencePassed ? new Error('missing verified native cancellation evidence') : undefined) ??
        (subscriptionMode && !subscriptionEvidencePassed ? new Error(`missing verified ${subscriptionMode} evidence`) : undefined);
      if (finalFailure) {
        report(
          `${passiveDisconnectMode ? 'rf-passive-disconnect' : cancellationMode ? 'rf-cancellation' : subscriptionMode === 'indication' ? 'rf-indication' : subscriptionMode === 'notification' ? 'rf-notification' : ioMode ? 'rf-io' : rssiMode ? 'rf-rssi' : gattMode ? 'rf-gatt' : connectMode ? 'rf-connect' : 'rf'}:failed:${describeError(finalFailure)}:serviceUuid=${serviceUuid ?? 'missing'}:scanSessionId=${RF_SCAN_SESSION}:peripheralId=${matchedPeripheralId}${connectMode ? `:connectionId=${RF_CONNECTION_ID}` : ''}`,
        );
      } else if (passiveDisconnectMode) {
        report(`rf-passive-disconnect:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:gattCommandId=${RF_GATT_COMMAND}:subscriptionId=${RF_NOTIFICATION_SUBSCRIPTION}:recoveryConnectCommandId=${RF_RECOVERY_CONNECT_COMMAND}:recoveryConnectionId=${RF_RECOVERY_CONNECTION_ID}:recoveryDisconnectCommandId=${RF_RECOVERY_DISCONNECT_COMMAND}:notificationValue=ef:passiveDisconnectCommandId=none:scanSessionId=${RF_SCAN_SESSION}`);
      } else if (subscriptionMode === 'indication') {
        report(`rf-indication:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:gattCommandId=${RF_GATT_COMMAND}:rejectionCommandId=${RF_INDICATION_REJECTION_COMMAND}:enableCommandId=${RF_INDICATION_ENABLE_COMMAND}:disableCommandId=${RF_INDICATION_DISABLE_COMMAND}:subscriptionId=${RF_INDICATION_SUBSCRIPTION}:indicationValue=ef:postDisableSilenceMs=${RF_POST_NOTIFICATION_DISABLE_OBSERVATION_MS}:disconnectCommandId=${RF_DISCONNECT_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`);
      } else if (subscriptionMode === 'notification') {
        report(`rf-notification:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:gattCommandId=${RF_GATT_COMMAND}:enableCommandId=${RF_NOTIFICATION_ENABLE_COMMAND}:disableCommandId=${RF_NOTIFICATION_DISABLE_COMMAND}:subscriptionId=${RF_NOTIFICATION_SUBSCRIPTION}:notificationValue=ef:postDisableSilenceMs=${RF_POST_NOTIFICATION_DISABLE_OBSERVATION_MS}:disconnectCommandId=${RF_DISCONNECT_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`);
      } else if (rssiMode) {
        report(`rf-rssi:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:rssiCommandId=${RF_RSSI_COMMAND}:rssiDbm=${rssiValue}:disconnectCommandId=${RF_DISCONNECT_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`);
      } else if (cancellationMode) {
        report(`rf-cancellation:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:gattCommandId=${RF_GATT_COMMAND}:activeCommandId=${RF_CANCELLATION_ACTIVE_COMMAND}:queuedCommandId=${RF_CANCELLATION_QUEUED_COMMAND}:recoveryCommandId=${RF_CANCELLATION_RECOVERY_COMMAND}:disconnectCommandId=${RF_DISCONNECT_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`);
      } else if (ioMode) {
        report(`rf-io:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:gattCommandId=${RF_GATT_COMMAND}:readCommandId=${RF_READ_INITIAL_COMMAND}:writeResponseCommandId=${RF_WRITE_RESPONSE_COMMAND}:writeWithoutResponseCommandId=${RF_WRITE_WITHOUT_RESPONSE_COMMAND}:disconnectCommandId=${RF_DISCONNECT_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`);
      } else if (gattMode) {
        report(`rf-gatt:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:gattCommandId=${RF_GATT_COMMAND}:disconnectCommandId=${RF_DISCONNECT_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`);
      } else if (connectMode) {
        report(`rf-connect:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:disconnectCommandId=${RF_DISCONNECT_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`);
      } else {
        report(
          `rf:passed:filterServiceUuid=${serviceUuid}:payloadAdvertisedServices=${payloadAdvertisedServices}:payloadContainsFilterUuid=${payloadContainsFilterUuid}:scanSessionId=${RF_SCAN_SESSION}:peripheralId=${matchedPeripheralId}:startCommandId=${RF_START_COMMAND}:stopCommandId=${RF_STOP_COMMAND}`,
        );
      }
    }
  }
}

export type RfLabMode =
  | 'rf'
  | 'rf-connect'
  | 'rf-gatt'
  | 'rf-io'
  | 'rf-rssi'
  | 'rf-notification'
  | 'rf-indication'
  | 'rf-passive-disconnect'
  | 'rf-cancellation'
  | 'rf-watchdog';

interface RfLabOption {
  mode: RfLabMode;
  label: string;
  description: string;
}

export const RF_LAB_MODES: readonly RfLabMode[] = [
  'rf',
  'rf-connect',
  'rf-gatt',
  'rf-io',
  'rf-rssi',
  'rf-notification',
  'rf-indication',
  'rf-passive-disconnect',
  'rf-cancellation',
  'rf-watchdog',
];

const RF_LAB_OPTIONS: readonly RfLabOption[] = [
  { mode: 'rf', label: 'Scan', description: 'Initialize, filter advertisements, stop, and verify silence.' },
  { mode: 'rf-connect', label: 'Connect', description: 'Scan, connect, disconnect, and verify lifecycle ordering.' },
  { mode: 'rf-gatt', label: 'GATT discovery', description: 'Discover and validate service, characteristic, and descriptor instances.' },
  { mode: 'rf-io', label: 'GATT I/O', description: 'Read, write with response, write without response, and read back.' },
  { mode: 'rf-rssi', label: 'RSSI', description: 'Read a connected peripheral RSSI and correlate its terminal.' },
  { mode: 'rf-notification', label: 'Notification', description: 'Enable, receive the exact value, disable, and verify silence.' },
  { mode: 'rf-indication', label: 'Strict Indication', description: 'Reject unsupported mode, enable Indication, receive, and disable.' },
  { mode: 'rf-passive-disconnect', label: 'Passive recovery', description: 'Observe passive loss, stale-route silence, and recovery connection.' },
  { mode: 'rf-cancellation', label: 'Cancellation', description: 'Reject active cancellation, cancel a queued read, prove terminal ordering, and recover the FIFO.' },
];

function rfLabSteps(mode: RfLabMode): RfStep[] {
  switch (mode) {
    case 'rf-connect': return INITIAL_RF_CONNECT_STEPS;
    case 'rf-gatt': return INITIAL_RF_GATT_STEPS;
    case 'rf-io': return INITIAL_RF_IO_STEPS;
    case 'rf-rssi': return INITIAL_RF_RSSI_STEPS;
    case 'rf-notification': return INITIAL_RF_NOTIFICATION_STEPS;
    case 'rf-indication': return INITIAL_RF_INDICATION_STEPS;
    case 'rf-passive-disconnect': return INITIAL_RF_PASSIVE_DISCONNECT_STEPS;
    case 'rf-cancellation': return INITIAL_RF_CANCELLATION_STEPS;
    default: return INITIAL_RF_STEPS;
  }
}

export async function runRfLabFlow(
  mode: RfLabMode,
  probe: Probe,
  rfServiceUuid: string | undefined,
  report: (line: string) => void,
  isCancelled: () => boolean,
  expectedLocalName?: string,
  rfCancellationControl?: string,
): Promise<void> {
  if (mode === 'rf-watchdog') {
    return runRfWatchdog(probe, rfServiceUuid, report, isCancelled, parseRfControl(rfCancellationControl, 'rf-watchdog'), expectedLocalName);
  }
  const ioMode = mode === 'rf-io';
  const rssiMode = mode === 'rf-rssi';
  const cancellationMode = mode === 'rf-cancellation';
  const passiveDisconnectMode = mode === 'rf-passive-disconnect';
  const subscriptionMode: RfSubscriptionMode | undefined = mode === 'rf-notification' || passiveDisconnectMode
    ? 'notification'
    : mode === 'rf-indication' ? 'indication' : undefined;
  const connectMode = mode !== 'rf';
  const gattMode = mode === 'rf-gatt' || ioMode || cancellationMode || subscriptionMode !== undefined;
  return runRfScan(
    probe,
    rfServiceUuid,
    report,
    isCancelled,
    connectMode,
    gattMode,
    ioMode,
    subscriptionMode,
    passiveDisconnectMode,
    rssiMode,
    cancellationMode,
    expectedLocalName,
    rfCancellationControl,
  );
}

export async function runRfLabSuite(
  rfServiceUuid: string | undefined,
  report: (line: string) => void,
  isCancelled: () => boolean,
  createProbe: (mode: RfLabMode) => Probe = () => createBleHubProbe(),
  onlyMode?: RfLabMode,
  setCurrentProbe?: (probe: Probe | undefined) => void,
  expectedLocalName?: string,
  rfCancellationControl?: string,
  sharedSignalGuard?: RfSuiteSignalGuard,
): Promise<boolean> {
  const modes = onlyMode ? [onlyMode] : RF_LAB_MODES;
  let failed = 0;
  const controls = onlyMode ? undefined : parseRfControls(rfCancellationControl);
  const signalGuard = sharedSignalGuard ?? (controls ? new RfSuiteSignalGuard(controls) : undefined);
  const output = report;
  report = (line): void => { signalGuard?.assertHealthy(); output(line); };
  const runtimeId = RF_BOOT_NONCE;
  try {
    if (controls) report(`rf-lab-suite:runtime:session=${controls[0].session}:nonce=${runtimeId}:phase=start`);
    for (const [index, mode] of modes.entries()) {
      if (isCancelled()) throw new Error('RF Lab suite cancelled');
      // CoreBluetooth/Android BLE teardown callbacks can arrive just after the
      // probe close promise resolves. Keep the same JS runtime, but give the
      // native route a bounded settle window before creating the next probe.
      if (index > 0) await delay(2000);
      report(`rf-lab-suite:flow-started:index=${index}:mode=${mode}`);
      if (Platform.OS === 'android' && (index === 5 || index === 9)) {
        // Android applies an app-level BLE scan-start throttle. The sixth scan
        // can otherwise receive no callbacks even though the Peripheral is live.
        report('rf-lab-suite:android-scan-throttle-wait:milliseconds=35000');
        await delay(35_000);
        if (isCancelled()) throw new Error('RF Lab suite cancelled during Android scan throttle wait');
      }
      const flowLines: string[] = [];
      const flowReport = (line: string): void => {
        flowLines.push(line);
        report(line);
      };
      let probe: Probe | undefined;
      let flowFailed = false;
      try {
        probe = createProbe(mode);
        setCurrentProbe?.(probe);
        await runRfLabFlow(mode, probe, rfServiceUuid, flowReport, isCancelled, expectedLocalName, rfCancellationControl);
      } catch (error: unknown) {
        flowFailed = true;
        flowReport(`${mode}:failed:${describeError(error)}`);
      } finally {
        try { if (probe) await probe.close(); }
        catch (error) { flowFailed = true; flowReport(`${mode}:failed:native-close:${describeError(error)}`); }
        finally { setCurrentProbe?.(undefined); }
      }
      const passed = !flowFailed && !flowLines.some((line) => /:failed(?::|$)|:close:rejected:/.test(line)) && flowLines.some((line) => mode === 'rf-watchdog' ? line === 'rf-watchdog:passed' : line.startsWith(`${mode}:passed:`));
      if (passed) {
        report(`rf-lab-suite:flow-passed:index=${index}:mode=${mode}`);
      } else {
        failed += 1;
        const detail = flowLines.find((line) => line.startsWith(`${mode}:failed:`)) ?? `${mode}:failed:missing-terminal-marker`;
        report(`rf-lab-suite:flow-failed:index=${index}:mode=${mode}:detail=${detail}`);
      }
    }

    const passed = modes.length - failed;
    if (controls) report(`rf-lab-suite:runtime:session=${controls[0].session}:nonce=${runtimeId}:phase=close`);
    report(`rf-lab-suite:${failed === 0 ? 'passed' : 'failed'}:flows=${modes.length}:passedCount=${passed}:failureCount=${failed}`);
    return failed === 0;
  } finally { if (!sharedSignalGuard) signalGuard?.remove(); }
}

function RfLab({ rfServiceUuid, rfExpectedLocalName }: AppProps): React.JSX.Element {
  const beginRun = useBeginRun();
  const platformLabel = Platform.OS === 'ios' ? 'iOS' : 'Android';
  const [selectedMode, setSelectedMode] = useState<RfLabMode>('rf');
  const [rfSteps, setRfSteps] = useState<RfStep[]>(INITIAL_RF_STEPS);
  const [statusLines, setStatusLines] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [closed, setClosed] = useState(false);
  const [runCount, setRunCount] = useState(0);
  const [cancelTargetCommandId, setCancelTargetCommandId] = useState(RF_START_COMMAND);
  const [cancelStatus, setCancelStatus] = useState('not requested');
  const activeRef = useRef(true);
  const probeRef = useRef<Probe | undefined>(undefined);

  const report = useCallback((line: string): void => {
    console.log(`BLEHUB_NITRO_RUNTIME ${line}`);
    if (!activeRef.current) return;
    setStatusLines((current) => [...current.slice(-11), line]);
    const cycleUpdate = /^rf-recovery(?:-ten)?:cycle:([0-9]):cycle:(passed|failed):/.exec(line);
    const update = cycleUpdate
      ? { id: `cycle-${cycleUpdate[1]}` as RfStepId, state: cycleUpdate[2] as StepState, detail: line }
      : rfStepState(line);
    if (update) {
      setRfSteps((current) => current.map((step) =>
        step.id === update.id ? { ...step, state: update.state, detail: update.detail } : step,
      ));
    }
  }, []);

  const start = useCallback((): void => {
    if (running) return;
    beginRun();
    const probe = createBleHubProbe();
    probeRef.current = probe;
    activeRef.current = true;
    setRunning(true);
    setClosed(false);
    setRunCount((current) => current + 1);
    setRfSteps(rfLabSteps(selectedMode));
    setStatusLines([]);
    setCancelStatus('ready');
    void runRfLabFlow(
      selectedMode,
      probe,
      rfServiceUuid,
      report,
      () => !activeRef.current,
      rfExpectedLocalName,
    )
      .catch((error: unknown) => report(`${rfMarkerPrefix(selectedMode)}:failed:${describeError(error)}`))
      .finally(() => {
        if (activeRef.current) {
          setRunning(false);
          setClosed(true);
        }
      });
  }, [beginRun, report, rfServiceUuid, rfExpectedLocalName, running, selectedMode]);

  const cancelOperation = useCallback((): void => {
    const probe = probeRef.current;
    const target = cancelTargetCommandId.trim();
    if (!probe || !target) return;
    if (!isCanonicalU64(target)) {
      const detail = `rf:cancel:rejected:targetCommandId=${target}:category=invalid_request`;
      setCancelStatus(detail);
      report(detail);
      return;
    }
    try {
      const error = probe.cancelOperation(target);
      const line = error
        ? `rf:cancel:rejected:targetCommandId=${target}:category=${error.category}:detail=${error.detail ?? ''}`
        : `rf:cancel:accepted:targetCommandId=${target}`;
      setCancelStatus(line);
      report(line);
    } catch (error: unknown) {
      const line = `rf:cancel:threw:targetCommandId=${target}:detail=${describeError(error)}`;
      setCancelStatus(line);
      report(line);
    }
  }, [cancelTargetCommandId, report]);

  const closeProbe = useCallback((): void => {
    const probe = probeRef.current;
    if (!probe || closed) return;
    activeRef.current = false;
    probe.removeRfScanListener();
    void probe.close().then(
      () => {
        setRunning(false);
        setClosed(true);
        console.log(`BLEHUB_NITRO_RUNTIME ${rfMarkerPrefix(selectedMode)}:close:resolved`);
      },
      (error: unknown) => {
        setRunning(false);
        setClosed(true);
        console.log(`BLEHUB_NITRO_RUNTIME ${rfMarkerPrefix(selectedMode)}:close:rejected:${describeError(error)}`);
      },
    );
  }, [closed, selectedMode]);

  useEffect(() => () => {
    activeRef.current = false;
    probeRef.current?.removeRfScanListener();
    void probeRef.current?.close().catch(() => undefined);
  }, []);

  const prefix = rfMarkerPrefix(selectedMode);
  const passed = statusLines.some((line) => line.startsWith(`${prefix}:passed:`));
  const failed = statusLines.some((line) => line.startsWith(`${prefix}:failed:`));
  const lastPassedStep = [...rfSteps].reverse().find((step) => step.state === 'passed');
  const runtimeStatus = running
    ? `running ${selectedMode}; waiting after ${lastPassedStep?.label ?? 'dispatch'}`
    : passed ? `${selectedMode} passed` : failed ? `${selectedMode} failed` : closed ? 'closed' : 'ready';
  useRunOutcome(failed ? 'failed' : passed ? 'passed' : running ? 'running' : 'ready', runtimeStatus);
  const selectedOption = RF_LAB_OPTIONS.find((option) => option.mode === selectedMode);

  return (
    <View style={styles.safeArea} testID="rf-lab-root">
      <ScrollView contentContainerStyle={styles.root} contentInsetAdjustmentBehavior="automatic">
        <View style={styles.header}>
          <View style={styles.headerCopy}>
            <Text style={styles.runTitle}>Interactive diagnostics</Text>
            <Text style={styles.subtitle}>{platformLabel} JS → Nitro → production BLE RF flow. The host recipe must provide the nRF fixture and final gate.</Text>
          </View>
          <View style={styles.statusCard}>
            <Text style={styles.statusCaption}>Runtime status</Text>
            <Text style={styles.statusCaption} testID="rf-lab-run-count">Run {runCount}</Text>
            <Text style={[styles.statusValue, passed && styles.successText]} testID="rf-lab-runtime-status">{runtimeStatus}</Text>
          </View>
        </View>

        <View style={styles.card}>
          <View style={styles.cardHeader}>
            <Text style={styles.cardTitle}>Select RF flow</Text>
            <Text style={styles.badge}>{selectedOption?.label}</Text>
          </View>
          {RF_LAB_OPTIONS.map((option) => (
            <Pressable
              key={option.mode}
              style={[styles.labMode, option.mode === selectedMode && styles.labModeSelected]}
              onPress={() => {
                if (running) return;
                setSelectedMode(option.mode);
                setRfSteps(rfLabSteps(option.mode));
                setStatusLines([]);
                setClosed(false);
              }}
              disabled={running}
              testID={`rf-lab-mode-${option.mode}`}
              accessibilityRole="button"
            >
              <Text style={styles.labModeLabel}>{option.label}</Text>
              <Text style={styles.stepDetail}>{option.description}</Text>
            </Pressable>
          ))}
        </View>

        <View style={styles.actions}>
          <Pressable
            style={[styles.button, styles.primaryButton]}
            onPress={start}
            disabled={running}
            testID="rf-lab-run-button"
            accessibilityRole="button"
          >
            <Text style={styles.primaryButtonText}>{running ? 'Running…' : `Run ${selectedOption?.label ?? 'RF flow'}`}</Text>
          </Pressable>
          <Pressable
            style={[styles.button, styles.secondaryButton]}
            onPress={closeProbe}
            disabled={closed || !probeRef.current}
            testID="rf-lab-close-button"
            accessibilityRole="button"
          >
            <Text style={styles.secondaryButtonText}>Close probe</Text>
          </Pressable>
        </View>

        <View style={styles.card}>
          <View style={styles.cardHeader}>
            <Text style={styles.cardTitle}>Native cancellation</Text>
            <Text style={styles.badge} testID="rf-lab-cancel-status">{cancelStatus}</Text>
          </View>
          <Text style={styles.stepDetail}>Cancels the selected command through production BleHub; completion/error evidence remains asynchronous.</Text>
          <TextInput
            style={styles.commandInput}
            value={cancelTargetCommandId}
            onChangeText={setCancelTargetCommandId}
            keyboardType="number-pad"
            autoCapitalize="none"
            testID="rf-lab-cancel-command-id"
            accessibilityLabel="Cancellation target command ID"
          />
          <Pressable
            style={[styles.button, styles.warningButton]}
            onPress={cancelOperation}
            disabled={closed || !probeRef.current}
            testID="rf-lab-cancel-button"
            accessibilityRole="button"
          >
            <Text style={styles.primaryButtonText}>Cancel native command</Text>
          </Pressable>
        </View>

        <View style={styles.card}>
          <View style={styles.cardHeader}>
            <Text style={styles.cardTitle}>RF gate coverage</Text>
            <Text style={styles.badge} testID="rf-lab-coverage">{rfSteps.filter((step) => step.state === 'passed').length}/{rfSteps.length}</Text>
          </View>
          <Text style={styles.stepDetail} testID="rf-lab-service-uuid">serviceUuid: {rfServiceUuid ?? 'missing (failure)'}</Text>
          {rfSteps.map((step) => <StepRow key={step.id} step={step} />)}
        </View>

        <View style={styles.card}>
          <View style={styles.cardHeader}>
            <Text style={styles.cardTitle}>Projected runtime events</Text>
            <Text style={styles.badge}>{statusLines.length}</Text>
          </View>
          {statusLines.length ? statusLines.map((line, index) => (
            <Text key={`${index}-${line}`} style={styles.eventLine}>{line}</Text>
          )) : <Text style={styles.emptyText}>Waiting for the RF flow to report.</Text>}
        </View>

        <Text style={styles.footer}>This page drives one RF flow at a time. Multi-DK, destructive fault, and engine-reload gates remain host-controlled recipes.</Text>
      </ScrollView>
    </View>
  );
}

function StepRow({ step, testID, stableHost = false }: {
  step: Pick<Step, 'label' | 'state' | 'detail'>;
  testID?: string;
  stableHost?: boolean;
}): React.JSX.Element {
  const indicator = step.state === 'passed' ? 'OK' : step.state === 'failed' ? 'FAIL' : '--';
  return (
    <View style={styles.stepRow} testID={testID} collapsable={stableHost ? false : undefined}>
      <Text style={[styles.stepIndicator, step.state === 'passed' && styles.successText, step.state === 'failed' && styles.errorText]}>
        {indicator}
      </Text>
      <View style={styles.stepCopy}>
        <Text style={styles.stepLabel}>{step.label}</Text>
        <Text style={styles.stepDetail}>{step.detail}</Text>
      </View>
    </View>
  );
}

function RfLabSuite(props: AppProps): React.JSX.Element {
  if (props.runtimeMode === 'rf-adapter-transition') return (
    <RfLabRunScreen title={props.rfAdapterMode === 'active-loss' ? 'Active-read radio loss' : 'Adapter OFF → ON'}
      description={props.rfAdapterMode === 'active-loss' ? 'Held read + queued read → radio OFF → retirement → fresh-Probe I/O. Separate from the full suite.' : 'Initialize with Bluetooth OFF, then enable it and recover I/O with a fresh Probe. Separate from the full suite.'}>
      <RfAdapterTransition {...props} runFlow={runRfLabFlow} />
    </RfLabRunScreen>
  );
  return <RfLabFlowSuite {...props} />;
}

function RfLabFlowSuite(props: AppProps): React.JSX.Element {
  const present = useRunReporter();
  const profile = useRef(Object.freeze({ ...props })).current;
  const { rfServiceUuid, rfExpectedLocalName, rfLabSuiteFlow, rfCancellationControl, rfRecoveryRunId } = profile;
  const full = rfRecoveryRunId !== undefined;
  const startedRef = useRef(false);
  const [fullStatus, setFullStatus] = useState({ latest: 'Waiting for the host-controlled run.', failure: '', summary: '' });
  const [fullFlows, setFullFlows] = useState(() => RF_LAB_MODES.map((mode) => ({ label: RF_LAB_OPTIONS.find((option) => option.mode === mode)?.label ?? mode, state: 'pending' as StepState, detail: 'waiting' })));
  const [fullCycles, setFullCycles] = useState(() => Array.from({ length: 10 }, (_, cycle) => ({ label: `Cycle ${cycle}`, state: 'pending' as StepState, detail: 'waiting' })));
  const [statusLines, setStatusLines] = useState<string[]>([]);
  const [cancelTargetCommandId, setCancelTargetCommandId] = useState(RF_START_COMMAND);
  const [cancelStatus, setCancelStatus] = useState('not requested');
  const activeRef = useRef(true);
  const probeRef = useRef<Probe | undefined>(undefined);

  useEffect(() => {
    let active = true;
    activeRef.current = true;
    const requested = rfLabSuiteFlow?.trim();
    const onlyMode = requested && (RF_LAB_MODES as readonly string[]).includes(requested)
      ? requested as RfLabMode
      : undefined;
    if (requested && !onlyMode && !full) {
      const error = `unsupported rfLabSuiteFlow: ${requested}`;
      console.log(`BLEHUB_NITRO_RUNTIME rf-lab-suite:failed:${error}`);
      present(`rf-lab-suite:failed:${error}`);
      setStatusLines([`rf-lab-suite:failed:${error}`]);
      return () => { activeRef.current = false; };
    }
    const report = (line: string): void => {
      console.log(`BLEHUB_NITRO_RUNTIME ${line}`);
      present(line);
      if (!activeRef.current) return;
      if (!full) { setStatusLines((current) => [...current, line]); return; }
      // Diagnostic payloads stay in console, never in a Fabric host-node list.
      if (/:event:|:resources:/.test(line)) return;
      const failed = /:failed(?::|$)|:flow-failed:|:close:rejected:/.test(line);
      const final = /^rf-lab-full:(passed|failed):/.test(line);
      setFullStatus((current) => ({ latest: line, failure: current.failure || (failed ? line : ''),
        summary: current.failure || (failed ? line : final ? line : current.summary) }));
      const flow = /^rf-lab-suite:flow-(started|passed|failed):index=(\d+):/.exec(line);
      if (flow) setFullFlows((current) => current.map((step, index) => index !== Number(flow[2]) || step.state === 'failed' ? step : {
        ...step, state: flow[1] === 'started' ? 'pending' : flow[1] as StepState, detail: flow[1],
      }));
      const cycle = /^rf-recovery-ten:cycle:([0-9]):([^:]+)(?::|$)/.exec(line);
      if (cycle) setFullCycles((current) => current.map((step, index) => index !== Number(cycle[1]) || step.state === 'failed' ? step : {
        ...step, state: cycle[2] === 'failed' ? 'failed' : cycle[2] === 'passed' ? 'passed' : step.state,
        detail: cycle[2] === 'passed' || cycle[2] === 'failed' ? cycle[2] : `In progress: ${cycle[2]}`,
      }));
    };
    void Promise.resolve().then(async () => {
      if (!active || startedRef.current) return;
      startedRef.current = true;
      if (full) {
        if (requested) throw new Error('full suite cannot select a single flow');
        await runRfLabFull(rfRecoveryRunId!, rfCancellationControl,
          (guard, output) => runRfLabSuite(rfServiceUuid, output, () => !active, undefined, undefined,
            (probe) => { probeRef.current = probe; }, rfExpectedLocalName, rfCancellationControl, guard),
          (guard, output) => runRfRecoveryTen(
            () => createBleHubProbe(),
            (probe, reject, connection) => createRfEventStream(probe, true, true, new Set([connection]), 1024, reject, true),
            profile, output, () => !active, guard),
          report, () => !active);
      } else {
        if (onlyMode === 'rf-cancellation') {
          parseRfControl(rfCancellationControl, 'rf-cancellation');
          if (!rfExpectedLocalName) throw new Error('cancellation requires the frozen fixture local name');
        }
        await runRfLabSuite(rfServiceUuid, report, () => !active, undefined, onlyMode,
          (probe) => { probeRef.current = probe; }, rfExpectedLocalName, rfCancellationControl);
      }
    }).catch((error: unknown) => report(`${full ? 'rf-lab-full' : 'rf-lab-suite'}:failed:${describeError(error)}`));
    return () => { active = false; activeRef.current = false; };
  }, [profile, full, rfRecoveryRunId, rfLabSuiteFlow, rfServiceUuid, rfExpectedLocalName, rfCancellationControl, present]);

  const publishCancellation = useCallback((line: string): void => {
    if (!activeRef.current) return;
    console.log(`BLEHUB_NITRO_RUNTIME ${line}`);
    setCancelStatus(line);
    setStatusLines((current) => [...current, line]);
  }, []);

  const cancelOperation = useCallback((): void => {
    const probe = probeRef.current;
    const target = cancelTargetCommandId.trim();
    if (!probe) {
      publishCancellation('rf-lab-suite:cancel:rejected:category=invalid_state:detail=no-active-flow');
      return;
    }
    if (!target) {
      publishCancellation('rf-lab-suite:cancel:rejected:targetCommandId=:category=invalid_request');
      return;
    }
    if (!isCanonicalU64(target)) {
      publishCancellation(`rf-lab-suite:cancel:rejected:targetCommandId=${target}:category=invalid_request`);
      return;
    }
    try {
      const error = probe.cancelOperation(target);
      publishCancellation(error
        ? `rf-lab-suite:cancel:rejected:targetCommandId=${target}:category=${error.category}`
        : `rf-lab-suite:cancel:accepted:targetCommandId=${target}`);
    } catch (error: unknown) {
      publishCancellation(`rf-lab-suite:cancel:threw:targetCommandId=${target}:detail=${describeError(error)}`);
    }
  }, [cancelTargetCommandId, publishCancellation]);

  if (full) return (
    <RfLabRunScreen title="Full suite"
      description="Phase 1 · 10 RF flows. Phase 2 · 10 recovery cycles. One App and JS runtime."
      status={fullStatus.failure ? 'RF Lab full suite failed' : fullStatus.summary ? 'RF Lab full suite passed' : 'running RF Lab full suite'}>
        <View style={styles.card} collapsable={false}>
          <Text style={styles.cardTitle}>1. RF capabilities</Text>
          <Text style={styles.badge} testID="rf-lab-suite-coverage">{fullFlows.filter((step) => step.state === 'passed').length}/10</Text>
          {fullFlows.map((step, index) => <StepRow key={RF_LAB_MODES[index]} step={step} stableHost testID={`rf-lab-full-flow-${index}`} />)}
        </View>
        <View style={styles.card} collapsable={false}>
          <Text style={styles.cardTitle}>2. Recovery lifecycles</Text>
          <Text style={styles.badge} testID="rf-lab-full-cycle-coverage">{fullCycles.filter((step) => step.state === 'passed').length}/10</Text>
          {fullCycles.map((step, index) => <StepRow key={`cycle-${index}`} step={step} stableHost testID={`rf-lab-full-cycle-${index}`} />)}
        </View>
        <View style={styles.card} collapsable={false} testID="rf-lab-full-report">
          <Text style={styles.eventLine} testID="rf-lab-full-latest">{fullStatus.latest}</Text>
          <Text style={styles.eventLine} testID="rf-lab-full-failure">{fullStatus.failure || 'No failure reported.'}</Text>
          <Text style={styles.eventLine} testID="rf-lab-full-summary">{fullStatus.summary || 'Awaiting final result.'}</Text>
        </View>
    </RfLabRunScreen>
  );

  const finalLine = statusLines.find((line) => line.startsWith('rf-lab-suite:passed:') || line.startsWith('rf-lab-suite:failed:'));
  const running = !finalLine && statusLines.length > 0;
  const status = finalLine?.startsWith('rf-lab-suite:passed:')
    ? 'RF Lab suite passed'
    : finalLine ? 'RF Lab suite failed' : running ? 'running RF Lab suite' : 'starting RF Lab suite';

  return (
    <View style={styles.safeArea} testID="rf-lab-suite-root">
      <ScrollView contentContainerStyle={styles.root} contentInsetAdjustmentBehavior="automatic">
        <View style={styles.header}>
          <View style={styles.headerCopy}>
            <Text style={styles.eyebrow}>BLEHUB / RN NITRO</Text>
            <Text style={styles.title}>RF Lab suite</Text>
            <Text style={styles.subtitle}>Host-controlled real-device execution of all nine single-Peripheral RF flows. The host owns fixture setup and waits for every flow marker.</Text>
          </View>
          <View style={styles.statusCard}>
            <Text style={styles.statusCaption}>Runtime status</Text>
            <Text style={[styles.statusValue, finalLine?.startsWith('rf-lab-suite:passed:') && styles.successText]} testID="rf-lab-suite-runtime-status">{status}</Text>
          </View>
        </View>
        <View style={styles.card}>
          <View style={styles.cardHeader}>
            <Text style={styles.cardTitle}>RF flows</Text>
            <Text style={styles.badge} testID="rf-lab-suite-coverage">{statusLines.filter((line) => line.startsWith('rf-lab-suite:flow-passed:')).length}/{rfLabSuiteFlow ? 1 : RF_LAB_MODES.length}</Text>
          </View>
          <Text style={styles.stepDetail}>serviceUuid: {rfServiceUuid ?? 'missing (failure)'}</Text>
          {RF_LAB_MODES.map((mode) => {
            const passed = statusLines.some((line) => line.startsWith('rf-lab-suite:flow-passed:') && line.endsWith(`:mode=${mode}`));
            const failed = statusLines.some((line) => line.startsWith('rf-lab-suite:flow-failed:') && line.includes(`:mode=${mode}:`));
            return <StepRow key={mode} step={{ label: mode, state: passed ? 'passed' : failed ? 'failed' : 'pending', detail: passed ? 'passed' : failed ? 'failed' : 'waiting' }} />;
          })}
        </View>
        <View style={styles.card}>
          <View style={styles.cardHeader}>
            <Text style={styles.cardTitle}>Native cancellation</Text>
            <Text style={styles.badge} testID="rf-lab-suite-cancel-status">{cancelStatus}</Text>
          </View>
          <Text style={styles.stepDetail}>Requests cancellation of the target command through production BleHub. Accepted is not completed: only the original command's cancelled terminal confirms cancellation. Rejected requests do not stop the flow; a cancelled terminal fails that flow and the suite continues.</Text>
          <TextInput
            style={styles.commandInput}
            value={cancelTargetCommandId}
            onChangeText={setCancelTargetCommandId}
            keyboardType="number-pad"
            autoCapitalize="none"
            testID="rf-lab-suite-cancel-command-id"
            accessibilityLabel="Suite cancellation target command ID"
          />
          <Pressable
            style={[styles.button, styles.warningButton]}
            onPress={cancelOperation}
            disabled={!probeRef.current || Boolean(finalLine)}
            testID="rf-lab-suite-cancel-button"
            accessibilityRole="button"
          >
            <Text style={styles.primaryButtonText}>Cancel current native command</Text>
          </Pressable>
        </View>
        <View style={styles.card}>
          <View style={styles.cardHeader}>
            <Text style={styles.cardTitle}>Projected runtime events</Text>
            <Text style={styles.badge}>{statusLines.length}</Text>
          </View>
          {statusLines.length ? statusLines.map((line, index) => <Text key={`${index}-${line}`} style={styles.eventLine}>{line}</Text>) : <Text style={styles.emptyText}>Waiting for the RF suite to start.</Text>}
        </View>
      </ScrollView>
    </View>
  );
}

function RuntimeApp({ runtimeMode = 'basic', rfServiceUuid, rfExpectedLocalName, rfMultiNotificationMode, rfMultiSubscriptionMode, rfMultiCycles, rfReloadNotificationName, rfPendingReadRunId, rfPendingReadGeneration, rfRecoveryRunId }: AppProps): React.JSX.Element {
  const present = useRunReporter();
  const beginRun = useBeginRun();
  const platformLabel = Platform.OS === 'ios' ? 'iOS' : 'Android';
  const [steps, setSteps] = useState<Step[]>(INITIAL_STEPS);
  const [rfSteps, setRfSteps] = useState<RfStep[]>(runtimeMode === 'rf-recovery-ten' ? INITIAL_RF_RECOVERY_TEN_STEPS : INITIAL_RF_STEPS);
  const [recoveryTenStatus, setRecoveryTenStatus] = useState({ latest: 'Waiting for the host-controlled run.', failure: '', final: '' });
  const [statusLines, setStatusLines] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [closed, setClosed] = useState(false);
  const [runCount, setRunCount] = useState(0);
  const activeRef = useRef(true);
  const probeRef = useRef<Probe | undefined>(undefined);
  const pendingReadProfile = useRef({ rfServiceUuid, rfExpectedLocalName, rfReloadNotificationName, rfPendingReadRunId }).current;
  const pendingReadStartedRef = useRef(false);
  const recoveryTenStartedRef = useRef(false);
  const recoveryTenProfile = useRef(Object.freeze({ rfServiceUuid, rfExpectedLocalName, rfRecoveryRunId })).current;
  const pendingReadGeneration = useRef(runtimeMode === 'rf-pending-read-reload'
    ? rfPendingReadGeneration : 1).current;
  const nativeOwnsPendingClose = runtimeMode === 'rf-pending-read-reload' && pendingReadGeneration === 1;

  const report = useCallback((line: string): void => {
    console.log(`BLEHUB_NITRO_RUNTIME ${line}`);
    present(line);
    if (!activeRef.current) return;
    if (runtimeMode === 'rf-recovery-ten') {
      // Preserve every audit record above, but never turn high-rate JSON into Fabric mutations.
      if (line.startsWith('rf-recovery-ten:event:') || line.startsWith('rf-recovery-ten:resources:')) return;
      const failed = line.includes(':failed:');
      const final = line.startsWith('rf-recovery-ten:passed:') || line.startsWith('rf-recovery-ten:failed:');
      setRecoveryTenStatus((current) => ({
        latest: line,
        failure: current.failure || (failed ? line : ''),
        final: failed ? line : final && !current.failure ? line : current.final,
      }));
      const cycle = /^rf-recovery-ten:cycle:([0-9]):(.+)$/.exec(line);
      if (cycle) {
        const id = `cycle-${cycle[1]}`;
        const passed = cycle[2] === 'passed' || cycle[2].startsWith('cycle:passed:');
        const phase = cycle[2].split(':')[0].replace(/-/g, ' ');
        setRfSteps((current) => current.map((step) => {
          if (step.id !== id) return step;
          const state = failed || step.state === 'failed' ? 'failed' : passed || step.state === 'passed' ? 'passed' : 'pending';
          const detail = state === 'failed' ? (step.state === 'failed' ? step.detail : line)
            : state === 'passed' ? 'Lifecycle complete: native close and all 17 resources zero.' : `In progress: ${phase}`;
          return step.state === state && step.detail === detail ? step : { ...step, state, detail };
        }));
      } else if (line.startsWith('rf-recovery-ten:close:passed:')) {
        setRfSteps((current) => current.map((step) => step.id === 'close'
          ? { ...step, state: 'passed', detail: 'All ten native Probe/Hub lifecycles closed.' } : step));
      }
      return;
    }
    setStatusLines((current) => [...current.slice(-7), line]);
    if (line === 'close:resolved' || line === 'rf:close:resolved') setClosed(true);
    const update = stepState(line);
    if (update) {
      setSteps((current) => current.map((step) =>
        step.id === update.id ? { ...step, state: update.state, detail: update.detail } : step,
      ));
    }
    const cycleUpdate = /^rf-recovery(?:-ten)?:cycle:([0-9]):cycle:(passed|failed):/.exec(line);
    const recoveryClose = line.startsWith('rf-recovery:close:passed:') || line.startsWith('rf-recovery-ten:close:passed:');
    const rfUpdate = cycleUpdate
      ? { id: `cycle-${cycleUpdate[1]}` as RfStepId, state: cycleUpdate[2] as StepState, detail: line }
      : recoveryClose ? { id: 'close' as RfStepId, state: 'passed' as StepState, detail: line } : rfStepState(line);
    if (rfUpdate) {
      setRfSteps((current) => current.map((step) =>
        step.id === rfUpdate.id ? { ...step, state: rfUpdate.state, detail: rfUpdate.detail } : step,
      ));
    }
  }, [runtimeMode, present]);

  const startSmoke = useCallback((): void => {
    if (running) return;
    beginRun();
    const probe = createBleHubProbe();
    probeRef.current = probe;
    setRunning(true);
    setClosed(false);
    setRunCount((current) => current + 1);
    setSteps(INITIAL_STEPS);
    setStatusLines([]);
    void runContractSmoke(probe, report)
      .catch((error: unknown) => report(`run:failed:${describeError(error)}`))
      .finally(() => setRunning(false));
  }, [beginRun, report, running]);

  const startRf = useCallback((): void => {
    if (running) return;
    if (runtimeMode === 'rf-recovery-ten') {
      if (recoveryTenStartedRef.current) return;
      recoveryTenStartedRef.current = true;
      setRunning(true);
      setClosed(false);
      setRunCount((current) => current + 1);
      setRfSteps(INITIAL_RF_RECOVERY_TEN_STEPS);
      setStatusLines([]);
      void runRfRecoveryTen(
        () => { const next = createBleHubProbe(); probeRef.current = next; return next; },
        (next, reject, connection) => createRfEventStream(next, true, true, new Set([connection]), 1024, reject, true),
        recoveryTenProfile, report, () => !activeRef.current,
      ).finally(() => { setRunning(false); setClosed(true); });
      return;
    }
    if (runtimeMode === 'rf-pending-read-reload') {
      if (pendingReadStartedRef.current) return;
      pendingReadStartedRef.current = true;
    }
    const probe = createBleHubProbe();
    if (runtimeMode === 'rf-pending-read-reload' && Platform.OS === 'ios') bindPendingReloadProbe(probe);
    probeRef.current = probe;
    setRunning(true);
    setClosed(false);
    setRunCount((current) => current + 1);
    if (runtimeMode === 'rf-pending-read-reload') {
      setStatusLines([]);
      void runRfPendingReadReload(
        probe, pendingReadProfile.rfServiceUuid, pendingReadProfile.rfReloadNotificationName,
        pendingReadGeneration, report, () => !activeRef.current,
        pendingReadProfile.rfPendingReadRunId, pendingReadProfile.rfExpectedLocalName,
      ).then(async (readPassed) => {
        if (nativeOwnsPendingClose) return;
        await probe.close();
        if (!activeRef.current) return;
        report(`rf-pending-read-reload:generation=${pendingReadGeneration}:close:passed`);
        setClosed(true);
        if (readPassed && pendingReadGeneration === 2) report(`rf-pending-read-reload:passed:runId=${pendingReadProfile.rfPendingReadRunId}`);
      })
        .catch((error: unknown) => report(`rf-pending-read-reload:generation=${pendingReadGeneration}:failed:${describeError(error)}`))
        .finally(() => setRunning(false));
      return;
    }
    if (runtimeMode === 'rf-reload') {
      setStatusLines([]);
      void runRfReload(probe, rfServiceUuid, report, () => !activeRef.current)
        .finally(() => setRunning(false));
      return;
    }
    if (runtimeMode === 'rf-recovery') {
      setRfSteps(INITIAL_RF_RECOVERY_STEPS);
      setStatusLines([]);
      void runRfRecovery(probe, rfServiceUuid, report, () => !activeRef.current)
        .finally(() => setRunning(false));
      return;
    }
    if (runtimeMode === 'rf-multi' || runtimeMode === 'rf-multi-soak') {
      setStatusLines([]);
      const cycles = Number.parseInt(rfMultiCycles ?? '10', 10);
      const run = runtimeMode === 'rf-multi-soak'
        ? runRfMultiSoak(probe, rfServiceUuid, report, () => !activeRef.current, rfMultiNotificationMode, rfMultiSubscriptionMode, cycles)
        : runRfMulti(probe, rfServiceUuid, report, () => !activeRef.current, rfMultiNotificationMode, rfMultiSubscriptionMode);
      void run.finally(() => setRunning(false));
      return;
    }
    if (runtimeMode === 'rf-multi-fault') {
      setStatusLines([]);
      void runRfMultiFault(probe, rfServiceUuid, report, () => !activeRef.current)
        .finally(() => setRunning(false));
      return;
    }
    const ioMode = runtimeMode === 'rf-io';
    const rssiMode = runtimeMode === 'rf-rssi';
    const cancellationMode = runtimeMode === 'rf-cancellation';
    const passiveDisconnectMode = runtimeMode === 'rf-passive-disconnect';
    const subscriptionMode: RfSubscriptionMode | undefined = runtimeMode === 'rf-notification' || passiveDisconnectMode
      ? 'notification'
      : runtimeMode === 'rf-indication' ? 'indication' : undefined;
    const connectMode = runtimeMode === 'rf-connect' || runtimeMode === 'rf-gatt' || ioMode || rssiMode || cancellationMode || subscriptionMode !== undefined;
    const gattMode = runtimeMode === 'rf-gatt' || ioMode || cancellationMode || subscriptionMode !== undefined;
    setRfSteps(cancellationMode ? INITIAL_RF_CANCELLATION_STEPS : passiveDisconnectMode ? INITIAL_RF_PASSIVE_DISCONNECT_STEPS : subscriptionMode === 'notification' ? INITIAL_RF_NOTIFICATION_STEPS : subscriptionMode === 'indication' ? INITIAL_RF_INDICATION_STEPS : ioMode ? INITIAL_RF_IO_STEPS : rssiMode ? INITIAL_RF_RSSI_STEPS : gattMode ? INITIAL_RF_GATT_STEPS : connectMode ? INITIAL_RF_CONNECT_STEPS : INITIAL_RF_STEPS);
    setStatusLines([]);
    void runRfScan(probe, rfServiceUuid, report, () => !activeRef.current, connectMode, gattMode, ioMode, subscriptionMode, passiveDisconnectMode, rssiMode, cancellationMode)
      .finally(() => setRunning(false));
  }, [report, rfServiceUuid, rfMultiNotificationMode, rfMultiSubscriptionMode, rfMultiCycles, rfReloadNotificationName, running, runtimeMode, pendingReadGeneration, pendingReadProfile, nativeOwnsPendingClose, recoveryTenProfile]);

  const closeProbe = useCallback((): void => {
    const probe = probeRef.current;
    if (!probe || closed) return;
    if (isRfMode(runtimeMode)) probe.removeRfScanListener();
    void probe.close().then(
      () => {
        report(isRfMode(runtimeMode) ? `${rfMarkerPrefix(runtimeMode)}:close:resolved` : 'close:resolved');
        setClosed(true);
      },
      (error: unknown) => report(`${isRfMode(runtimeMode) ? `${rfMarkerPrefix(runtimeMode)}:close:rejected` : 'close:rejected'}:${describeError(error)}`),
    );
  }, [closed, report, runtimeMode]);

  useEffect(() => {
    activeRef.current = true;
    if (isRfMode(runtimeMode)) {
      startRf();
    } else if (runtimeMode === 'stress') {
      setRunning(true);
      setStatusLines([]);
      void runLifecycleStress(
        report,
        () => !activeRef.current,
        (probe) => {
          probeRef.current = probe;
        },
      )
        .catch((error: unknown) => report(`stress:failed:${describeError(error)}`))
        .finally(() => setRunning(false));
    } else if (runtimeMode === 'reload') {
      const probe = createBleHubProbe();
      probeRef.current = probe;
      setRunning(true);
      setStatusLines([]);
      void runReloadProbe(probe, report)
        .catch((error: unknown) => report(`reload:failed:${describeError(error)}`))
        .finally(() => setRunning(false));
    } else {
      startSmoke();
    }
    return () => {
      if (runtimeMode === 'reload') report('reload:cleanup-started');
      if (runtimeMode === 'rf-reload') report('rf-reload:cleanup-started');
      if (runtimeMode === 'rf-pending-read-reload') report(`rf-pending-read-reload:generation=${pendingReadGeneration}:cleanup-started`);
      activeRef.current = false;
      if (runtimeMode === 'rf-recovery-ten') return; // The strict runner owns close and its armed callback guard.
      if (isRfMode(runtimeMode)) probeRef.current?.removeRfScanListener();
      else probeRef.current?.removeCompletionListener();
      // The native generation-1 engine-invalidation hook must own the first close.
      const closing = nativeOwnsPendingClose ? undefined : probeRef.current?.close();
      if (runtimeMode === 'rf-reload' || runtimeMode === 'rf-pending-read-reload') void closing?.catch(
        (error: unknown) => report(`${rfMarkerPrefix(runtimeMode)}:close-rejected:${describeError(error)}`),
      );
    };
  }, []);

  const passedCount = steps.filter((step) => step.state === 'passed').length;
  const rfPassedCount = rfSteps.filter((step) => step.state === 'passed').length;
  const rfPassed = statusLines.some((line) => line.startsWith('rf:passed:'));
  const rfConnectPassed = statusLines.some((line) => line.startsWith('rf-connect:passed:'));
  const rfGattPassed = statusLines.some((line) => line.startsWith('rf-gatt:passed:'));
  const rfIoPassed = statusLines.some((line) => line.startsWith('rf-io:passed:'));
  const rfNotificationPassed = statusLines.some((line) => line.startsWith('rf-notification:passed:'));
  const rfIndicationPassed = statusLines.some((line) => line.startsWith('rf-indication:passed:'));
  const rfPassiveDisconnectPassed = statusLines.some((line) => line.startsWith('rf-passive-disconnect:passed:'));
  const rfCancellationPassed = statusLines.some((line) => line.startsWith('rf-cancellation:passed:'));
  const rfFailed = statusLines.some((line) => line.startsWith('rf:failed:') || line.startsWith('rf:close:rejected:'));
  const rfConnectFailed = statusLines.some((line) => line.startsWith('rf-connect:failed:') || line.startsWith('rf-connect:close:rejected:'));
  const rfGattFailed = statusLines.some((line) => line.startsWith('rf-gatt:failed:') || line.startsWith('rf-gatt:close:rejected:'));
  const rfIoFailed = statusLines.some((line) => line.startsWith('rf-io:failed:') || line.startsWith('rf-io:close:rejected:'));
  const rfNotificationFailed = statusLines.some((line) => line.startsWith('rf-notification:failed:') || line.startsWith('rf-notification:close:rejected:'));
  const rfIndicationFailed = statusLines.some((line) => line.startsWith('rf-indication:failed:') || line.startsWith('rf-indication:close:rejected:'));
  const rfPassiveDisconnectFailed = statusLines.some((line) => line.startsWith('rf-passive-disconnect:failed:') || line.startsWith('rf-passive-disconnect:close:rejected:'));
  const rfCancellationFailed = statusLines.some((line) => line.startsWith('rf-cancellation:failed:') || line.startsWith('rf-cancellation:close:rejected:'));
  const rfMultiPassed = statusLines.some((line) => line.startsWith('rf-multi:passed:'));
  const rfMultiFailed = statusLines.some((line) => line.startsWith('rf-multi:failed:') || line.startsWith('rf-multi:close:rejected:'));
  const rfMultiFaultPassed = statusLines.some((line) => line.startsWith('rf-multi-fault:passed:'));
  const rfMultiFaultFailed = statusLines.some((line) => line.startsWith('rf-multi-fault:failed:') || line.startsWith('rf-multi-fault:close:rejected:'));
  const runtimeStatus = runtimeMode === 'rf-recovery-ten'
    ? recoveryTenStatus.failure ? 'RF recovery 10 cycles failed' : recoveryTenStatus.final.startsWith('rf-recovery-ten:passed:') ? 'RF recovery 10 cycles passed' : running ? 'running RF recovery (10 cycles)' : closed ? 'closed' : 'ready'
    : runtimeMode === 'rf-pending-read-reload'
    ? statusLines.some((line) => line.startsWith(`rf-pending-read-reload:generation=${pendingReadGeneration}:failed:`)) ? 'RF pending read reload failed' : statusLines.some((line) => line.startsWith(Platform.OS === 'android' ? 'rf-pending-read-reload:passed:runId=' : 'rf-pending-read-reload:generation=2:read:passed:')) ? 'RF pending read reload passed' : running ? `running RF pending read generation ${pendingReadGeneration}` : 'awaiting external reload'
    : runtimeMode === 'rf-reload'
    ? statusLines.some((line) => line.startsWith('rf-reload:failed:')) ? 'RF reload failed' : statusLines.some((line) => line.startsWith('rf-reload:active:')) ? 'RF active; awaiting host/fixture verdict' : running ? 'running RF reload gate' : 'waiting for native reload'
    : runtimeMode === 'rf-recovery'
    ? running ? 'running RF recovery (3 cycles)' : statusLines.some((line) => line.startsWith('rf-recovery:passed:')) ? 'RF recovery passed' : statusLines.some((line) => line.startsWith('rf-recovery:failed:')) ? 'RF recovery failed' : closed ? 'closed' : 'ready'
    : runtimeMode === 'rf-multi-soak'
      ? running ? 'running RF multi-DK continuous soak' : statusLines.some((line) => line.startsWith('rf-multi-soak:passed:')) ? 'RF multi-DK continuous soak passed' : statusLines.some((line) => line.startsWith('rf-multi-soak:failed:')) ? 'RF multi-DK continuous soak failed' : closed ? 'closed' : 'ready'
    : runtimeMode === 'rf-multi'
      ? running ? 'running RF multi-DK gate' : rfMultiPassed ? 'RF multi-DK passed' : rfMultiFailed ? 'RF multi-DK failed' : closed ? 'closed' : 'ready'
    : runtimeMode === 'rf-multi-fault'
      ? running ? 'running RF multi-DK fault/reconnect gate' : rfMultiFaultPassed ? 'RF multi-DK fault/reconnect passed' : rfMultiFaultFailed ? 'RF multi-DK fault/reconnect failed' : closed ? 'closed' : 'ready'
    : runtimeMode === 'rf'
    ? running ? 'running RF scan gate' : rfPassed ? 'RF scan passed' : rfFailed ? 'RF scan failed' : closed ? 'closed' : 'ready'
    : runtimeMode === 'rf-connect'
      ? running ? 'running RF connect gate' : rfConnectPassed ? 'RF connect passed' : rfConnectFailed ? 'RF connect failed' : closed ? 'closed' : 'ready'
      : runtimeMode === 'rf-notification'
        ? running ? 'running RF notification gate' : rfNotificationPassed ? 'RF notification passed' : rfNotificationFailed ? 'RF notification failed' : closed ? 'closed' : 'ready'
      : runtimeMode === 'rf-indication'
        ? running ? 'running RF strict Indication gate' : rfIndicationPassed ? 'RF strict Indication passed' : rfIndicationFailed ? 'RF strict Indication failed' : closed ? 'closed' : 'ready'
      : runtimeMode === 'rf-passive-disconnect'
        ? running ? 'running RF passive disconnect/recovery gate' : rfPassiveDisconnectPassed ? 'RF passive disconnect/recovery passed' : rfPassiveDisconnectFailed ? 'RF passive disconnect/recovery failed' : closed ? 'closed' : 'ready'
      : runtimeMode === 'rf-cancellation'
        ? running ? 'running RF native cancellation gate' : rfCancellationPassed ? 'RF native cancellation passed' : rfCancellationFailed ? 'RF native cancellation failed' : closed ? 'closed' : 'ready'
      : runtimeMode === 'rf-io'
        ? running ? 'running RF GATT I/O gate' : rfIoPassed ? 'RF GATT I/O passed' : rfIoFailed ? 'RF GATT I/O failed' : closed ? 'closed' : 'ready'
        : runtimeMode === 'rf-gatt'
          ? running ? 'running RF GATT gate' : rfGattPassed ? 'RF GATT passed' : rfGattFailed ? 'RF GATT failed' : closed ? 'closed' : 'ready'
    : runtimeMode === 'stress'
      ? running ? 'running lifecycle stress' : 'lifecycle stress complete'
      : runtimeMode === 'reload'
        ? running ? 'preparing runtime reload' : 'waiting for native reload'
        : running ? 'running contract smoke' : closed && passedCount === steps.length ? 'contract passed' : closed ? 'closed' : 'ready';
  useRunOutcome(runtimeStatus.includes('failed') ? 'failed' : runtimeStatus.includes('passed') ? 'passed' : running ? 'running' : 'ready', runtimeStatus);
  const scenario = findScenario(runtimeMode);

  return (
    <View style={styles.safeArea} testID={runtimeMode === 'rf' ? 'rf-scan-lab-root' : runtimeMode === 'rf-connect' ? 'rf-connect-lab-root' : runtimeMode === 'rf-gatt' ? 'rf-gatt-lab-root' : runtimeMode === 'rf-io' ? 'rf-io-lab-root' : runtimeMode === 'rf-notification' ? 'rf-notification-lab-root' : runtimeMode === 'rf-indication' ? 'rf-indication-lab-root' : runtimeMode === 'rf-passive-disconnect' ? 'rf-passive-disconnect-lab-root' : runtimeMode === 'rf-cancellation' ? 'rf-cancellation-lab-root' : runtimeMode === 'rf-recovery' ? 'rf-recovery-lab-root' : runtimeMode === 'rf-recovery-ten' ? 'rf-recovery-ten-lab-root' : runtimeMode === 'rf-multi' ? 'rf-multi-lab-root' : runtimeMode === 'rf-reload' ? 'rf-reload-lab-root' : 'contract-lab-root'}>
      <ScrollView
        contentContainerStyle={styles.root}
        contentInsetAdjustmentBehavior="automatic"
        // The bounded ten-cycle gate keeps its offscreen progress rows attached.
        removeClippedSubviews={runtimeMode === 'rf-recovery-ten' ? false : undefined}
        testID="runtime-scroll"
      >
        <View style={styles.header}>
          <View style={styles.headerCopy}>
            <Text style={styles.runTitle} testID={runtimeMode === 'rf' ? 'rf-scan-title' : runtimeMode === 'rf-connect' ? 'rf-connect-title' : runtimeMode === 'rf-gatt' ? 'rf-gatt-title' : runtimeMode === 'rf-io' ? 'rf-io-title' : runtimeMode === 'rf-notification' ? 'rf-notification-title' : runtimeMode === 'rf-indication' ? 'rf-indication-title' : runtimeMode === 'rf-passive-disconnect' ? 'rf-passive-disconnect-title' : runtimeMode === 'rf-cancellation' ? 'rf-cancellation-title' : runtimeMode === 'rf-recovery' ? 'rf-recovery-title' : runtimeMode === 'rf-multi' ? 'rf-multi-title' : 'contract-lab-title'}>
              {runtimeMode === 'basic' ? 'Contract lab' : scenario?.title ?? 'Runtime test'}
            </Text>
            <Text style={styles.subtitle}>
              {scenario?.description ?? 'A visible React Native surface for the Nitro and production BleHub assembly.'}
            </Text>
          </View>
          <View style={styles.statusCard}>
            <Text style={styles.statusCaption}>Runtime status</Text>
            <Text style={styles.statusCaption} testID="run-count">Run {runCount}</Text>
            <Text
              style={[styles.statusValue, (runtimeStatus === 'contract passed' || runtimeStatus === 'RF scan passed' || runtimeStatus === 'RF connect passed' || runtimeStatus === 'RF GATT passed' || runtimeStatus === 'RF GATT I/O passed' || runtimeStatus === 'RF notification passed' || runtimeStatus === 'RF strict Indication passed' || runtimeStatus === 'RF passive disconnect/recovery passed' || runtimeStatus === 'RF native cancellation passed' || runtimeStatus === 'RF recovery passed') && styles.successText]}
              testID="runtime-status"
            >
              {runtimeStatus}
            </Text>
          </View>
        </View>

        <View style={styles.actions}>
          <Pressable
            style={[styles.button, styles.primaryButton]}
            onPress={isRfMode(runtimeMode) ? startRf : startSmoke}
            disabled={running || runtimeMode === 'rf-pending-read-reload' || runtimeMode === 'rf-recovery-ten'}
            testID={runtimeMode === 'rf' ? 'rerun-rf-scan-button' : runtimeMode === 'rf-connect' ? 'rerun-rf-connect-button' : runtimeMode === 'rf-gatt' ? 'rerun-rf-gatt-button' : runtimeMode === 'rf-io' ? 'rerun-rf-io-button' : runtimeMode === 'rf-notification' ? 'rerun-rf-notification-button' : runtimeMode === 'rf-indication' ? 'rerun-rf-indication-button' : runtimeMode === 'rf-passive-disconnect' ? 'rerun-rf-passive-disconnect-button' : runtimeMode === 'rf-cancellation' ? 'rerun-rf-cancellation-button' : runtimeMode === 'rf-recovery' ? 'rerun-rf-recovery-button' : 'recreate-run-button'}
            accessibilityRole="button"
          >
            <Text style={styles.primaryButtonText}>
              {runtimeMode === 'rf-recovery-ten' ? 'Strict host-controlled run (no rerun)' : runtimeMode === 'rf-pending-read-reload' ? 'Host-controlled generation (no rerun)' : running ? 'Running…' : runtimeMode === 'rf' ? 'Run RF scan again' : runtimeMode === 'rf-connect' ? 'Run RF connect again' : runtimeMode === 'rf-gatt' ? 'Run RF GATT again' : runtimeMode === 'rf-io' ? 'Run RF GATT I/O again' : runtimeMode === 'rf-notification' ? 'Run RF notification again' : runtimeMode === 'rf-indication' ? 'Run RF strict Indication again' : runtimeMode === 'rf-passive-disconnect' ? 'Run RF passive disconnect/recovery again' : runtimeMode === 'rf-cancellation' ? 'Run RF native cancellation again' : runtimeMode === 'rf-recovery' ? 'Run RF recovery again' : closed ? 'Recreate and run' : 'Run contract smoke again'}
            </Text>
          </Pressable>
          <Pressable
            style={[styles.button, styles.secondaryButton]}
            onPress={closeProbe}
            disabled={closed || running || runtimeMode === 'rf-pending-read-reload' || runtimeMode === 'rf-recovery-ten'}
            testID="close-probe-button"
            accessibilityRole="button"
          >
            <Text style={styles.secondaryButtonText}>Close probe</Text>
          </Pressable>
        </View>

        {isRfMode(runtimeMode) ? (
          <View style={styles.card}>
            <View style={styles.cardHeader}>
              <Text style={styles.cardTitle}>RF gate coverage</Text>
              <Text style={styles.badge} testID={runtimeMode === 'rf' ? 'rf-scan-coverage' : runtimeMode === 'rf-connect' ? 'rf-connect-coverage' : runtimeMode === 'rf-gatt' ? 'rf-gatt-coverage' : runtimeMode === 'rf-io' ? 'rf-io-coverage' : runtimeMode === 'rf-notification' ? 'rf-notification-coverage' : runtimeMode === 'rf-indication' ? 'rf-indication-coverage' : runtimeMode === 'rf-passive-disconnect' ? 'rf-passive-disconnect-coverage' : runtimeMode === 'rf-recovery' ? 'rf-recovery-coverage' : 'rf-indication-coverage'}>{rfPassedCount}/{rfSteps.length}</Text>
            </View>
            <Text style={styles.stepDetail} testID="rf-service-uuid">serviceUuid: {rfServiceUuid ?? 'missing (failure)'}</Text>
            {rfSteps.map((step) => <StepRow
              key={step.id}
              step={step}
              stableHost={runtimeMode === 'rf-recovery-ten'}
              testID={runtimeMode === 'rf-recovery-ten' ? `rf-recovery-ten-${step.id}` : undefined}
            />)}
          </View>
        ) : (
          <View style={styles.card}>
            <View style={styles.cardHeader}>
              <Text style={styles.cardTitle}>Contract coverage</Text>
              <Text style={styles.badge} testID="contract-coverage">{passedCount}/{steps.length}</Text>
            </View>
            {steps.map((step) => <StepRow key={step.id} step={step} />)}
          </View>
        )}

        <View
          style={styles.card}
          collapsable={runtimeMode === 'rf-recovery-ten' ? false : undefined}
          testID="runtime-report-card"
        >
          <View style={styles.cardHeader}>
            <Text style={styles.cardTitle}>{runtimeMode === 'rf-recovery-ten' ? 'Recovery progress and result' : 'Projected runtime events'}</Text>
            <Text style={styles.badge}>{runtimeMode === 'rf-recovery-ten' ? 'audit → console' : statusLines.length}</Text>
          </View>
          {runtimeMode === 'rf-recovery-ten' ? (
            <>
              <Text style={styles.eventLine} testID="rf-recovery-ten-latest">{recoveryTenStatus.latest}</Text>
              <Text style={recoveryTenStatus.failure ? styles.errorText : styles.stepDetail} testID="rf-recovery-ten-failure">{recoveryTenStatus.failure || 'No failure reported.'}</Text>
              <Text style={styles.eventLine} testID="rf-recovery-ten-result">{recoveryTenStatus.final || 'Awaiting final result.'}</Text>
              <Text style={styles.stepDetail}>All native event and resource JSON records remain in the console audit; only cycle progress is rendered here.</Text>
            </>
          ) : (
            <Text style={styles.eventLine} testID="runtime-event-summary">{statusLines.join('\n') || 'Waiting for the Nitro object to report.'}</Text>
          )}
        </View>

        <Text style={styles.footer}>
          {isRfMode(runtimeMode)
            ? `${platformLabel} JS→Nitro RF gate; the host recipe and checker remain the final RF authority.`
            : 'This is a Nitro transport/lifecycle gate, not BLE RF or the public BLeng surface.'}
        </Text>
      </ScrollView>
    </View>
  );
}

export default function App(props: AppProps): React.JSX.Element {
  return (
    <RfLabWorkbench mode={props.runtimeMode} serviceUuid={props.rfServiceUuid}
      fullSuite={props.rfRecoveryRunId !== undefined} adapterMode={props.rfAdapterMode}
      runId={props.runtimeMode === 'rf-ccc-disable-reject' ? cccDisableRejectSession(props.rfCccDisableRejectControl) : props.runtimeMode === 'rf-ccc-enable-reject' ? cccEnableRejectSession(props.rfCccEnableRejectControl) : props.runtimeMode === 'rf-write-reject' ? writeRejectSession(props.rfWriteRejectControl) : props.rfPermissionRunId ?? props.rfAdapterRunId ?? props.rfRecoveryRunId ?? props.rfPendingReadRunId}
      interactive={<RfLab {...props} />}>
      {props.runtimeMode && props.runtimeMode !== 'rf-workbench' ? <ScenarioRuntime {...props} /> : null}
    </RfLabWorkbench>
  );
}

function ScenarioRuntime(props: AppProps): React.JSX.Element {
  if (props.runtimeMode === 'rf-ccc-disable-reject') return <RfCccDisableRejectLab {...props} />;
  if (props.runtimeMode === 'rf-ccc-enable-reject') return <RfCccEnableRejectLab {...props} />;
    if (props.runtimeMode === 'rf-write-reject') return <RfWriteRejectLab {...props} />;
  if (props.runtimeMode === 'rf-permission-transition' || props.runtimeMode === 'permission-authorization') return (
    <RfLabRunScreen title={props.runtimeMode === 'permission-authorization' ? 'iOS authorization (non-RF)' : 'Android permission transition'}
      description={props.runtimeMode === 'permission-authorization'
        ? 'Inspect → authorized baseline → denied start → fresh authorized initialization. XCUITest owns restoration; no RF recovery is claimed.'
        : 'Denied start → active-read permission revocation / OS process death → fresh-App recovery. The host owns the full-run verdict.'}>
      <RfPermissionTransition {...props} runFlow={runRfLabFlow} />
    </RfLabRunScreen>
  );
  if (props.runtimeMode === 'rf-adapter-transition') return <RfLabSuite {...props} />;
  if (props.runtimeMode === 'rf-cancellation') return <RfLabSuite {...props} rfLabSuiteFlow="rf-cancellation" />;
  if (props.runtimeMode === 'rf-watchdog') return <RfWatchdogLab rfServiceUuid={props.rfServiceUuid} rfCancellationControl={props.rfCancellationControl} rfExpectedLocalName={props.rfExpectedLocalName} />;
  if (props.runtimeMode === 'rf-lab') return <RfLab {...props} />;
  if (props.runtimeMode === 'rf-lab-suite') return <RfLabSuite {...props} />;
  return <RuntimeApp {...props} />;
}
