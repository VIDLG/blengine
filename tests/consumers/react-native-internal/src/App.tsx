import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Pressable,
  ScrollView,
  Linking,
  Settings,
  StyleSheet,
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
  PENDING_READ_GENERATION_KEY,
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
  RF_PENDING_READ_EXPECTED_LOCAL_NAME,
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

interface AppProps {
  runtimeMode?: RuntimeMode;
  rfServiceUuid?: string;
  rfMultiNotificationMode?: string;
  rfMultiSubscriptionMode?: string;
  rfMultiCycles?: string;
  rfReloadNotificationName?: string;
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
  const match = /^rf:step:(initialize|scan-start|advertisement|scan-stop|silence|connecting|connected|gatt-discovery|rssi|read-initial|write-response|read-response|write-without-response|read-without-response|indication-rejection|notification-enable|notification-value|notification-disable|indication-enable|indication-value|indication-disable|disconnected|connection-silence|recovery-connecting|recovery-connected|recovery-disconnected|recovery-connection-silence|close):(passed|failed):/.exec(line);
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

function createRfEventStream(
  probe: Probe,
  connectMode = false,
  passiveDisconnectMode = false,
  passiveConnectionIds: ReadonlySet<string> = new Set([RF_CONNECTION_ID]),
  maxEvents = MAX_RF_EVENT_COUNT,
  rejectEvent?: (event: RfScanEvent) => boolean,
): RfEventStream {
  const events: RfScanEvent[] = [];
  const expectedCommandFailures = new Map<string, string>();
  let wake: (() => void) | undefined;
  let removed = false;
  let terminalError: Error | undefined;
  let cursor = 0;
  const failClosed = (error: Error): void => {
    if (terminalError) return;
    terminalError = error;
    wake?.();
    wake = undefined;
  };
  const listener = (event: RfScanEvent): void => {
    if (removed || terminalError) return;
    if (events.length >= maxEvents) {
      failClosed(new Error(`RF event stream exceeded ${maxEvents} events`));
      return;
    }
    if (rejectEvent?.(event)) {
      failClosed(new Error(`previous generation event leaked:kind=${event.kind}:commandId=${event.commandId ?? 'none'}:connectionId=${event.connectionId ?? 'none'}`));
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
        while (cursor < events.length) {
          const event = events[cursor];
          cursor += 1;
          if (predicate(event)) return event;
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
  generation: number,
  report: (line: string) => void,
  isCancelled: () => boolean,
): Promise<void> {
  const prefix = `rf-pending-read-reload:generation=${generation}`;
  let stream: RfEventStream | undefined;
  let armSubscription: ReturnType<typeof Linking.addEventListener> | undefined;
  let armAwaiting = false;
  let armReceived = false;
  let armFailure: Error | undefined;
  try {
    if (generation !== 1 && generation !== 2) throw new Error(`invalid generation ${generation}`);
    const serviceUuid = rfServiceUuid?.trim();
    if (!serviceUuid) throw new Error('rfServiceUuid is required');
    const notificationToken = /^dev\.blehub\.rn\.reload\.([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$/.exec(rfReloadNotificationName ?? '')?.[1];
    if (!notificationToken) throw new Error('rfReloadNotificationName must contain a canonical UUID');
    const expectedArmURL = `blehub-rn-spike://arm/${notificationToken.toLowerCase()}`;
    if (generation === 2) {
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
      report(`${prefix}:arm-linking-listener:registered-before-initialize`);
    }
    const base = generation === 1 ? 12000 : 13000;
    const id = (offset: number): string => String(base + offset);
    const stale = (value: string | undefined): boolean => value !== undefined &&
      /^1200[0-9]$/.test(value);
    stream = createRfEventStream(probe, true, false, undefined, MAX_RF_EVENT_COUNT,
      generation === 2 ? (event) => stale(event.commandId) || stale(event.scanSessionId) ||
        stale(event.connectionId) || stale(event.gattSessionId) || stale(event.subscriptionId) : undefined);
    report(`${prefix}:listener:registered-before-initialize`);
    if (probe.initialize(id(0))) throw new Error('initialize dispatch rejected');
    const initDeadline = Date.now() + RF_INITIALIZATION_TIMEOUT_MS;
    await stream.waitFor((e) => e.kind === 'adapterSnapshotChanged' && e.adapterState === 'PoweredOn' && e.scanAuthorization === 'Authorized', RF_INITIALIZATION_TIMEOUT_MS, 'authorized adapter', isCancelled);
    await expectRfConnectionEvent(stream, 'commandCompleted', id(0), undefined, initDeadline, isCancelled);
    report(`${prefix}:initialize:passed:commandId=${id(0)}`);

    if (probe.startRfScan(id(2), id(1), serviceUuid)) throw new Error('scan dispatch rejected');
    const scanDeadline = Date.now() + RF_SCAN_TIMEOUT_MS;
    await stream.waitFor((e) => e.kind === 'scanStarted' && e.commandId === id(2) && e.scanSessionId === id(1), RF_SCAN_TIMEOUT_MS, 'scan started', isCancelled);
    await expectRfConnectionEvent(stream, 'commandCompleted', id(2), undefined, scanDeadline, isCancelled);
    const advertisement = await stream.waitFor((e) => e.kind === 'advertisementObserved' && e.scanSessionId === id(1) && (e.localName == null || e.localName === RF_PENDING_READ_EXPECTED_LOCAL_NAME), Math.max(1, scanDeadline - Date.now()), 'advertisement', isCancelled);
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
    if (probe.discoverRfGatt(id(7), id(4), id(6))) throw new Error('GATT dispatch rejected');
    const discovered = await stream.waitFor((e) => e.kind === 'gattDiscovered' && e.commandId === id(7) && e.connectionId === id(4), RF_CONNECTION_TIMEOUT_MS, 'GATT database', isCancelled);
    const database = validateRfGattDatabase(discovered, serviceUuid, id(4));
    if (database.gattSessionId !== id(6)) throw new Error('GATT session ID mismatch');
    await expectRfConnectionEvent(stream, 'commandCompleted', id(7), undefined, Date.now() + RF_CONNECTION_TIMEOUT_MS, isCancelled);
    report(`${prefix}:gatt:passed:commandId=${id(7)}:connectionId=${id(4)}:gattSessionId=${id(6)}`);

    const characteristic = database.readWriteCharacteristicId;
    if (generation === 2) {
      if (armFailure) throw armFailure;
      armAwaiting = true;
      report(`${prefix}:awaiting-arm:commandId=${id(8)}:connectionId=${id(4)}:gattSessionId=${id(6)}:characteristicId=${characteristic}`);
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
    const dispatchError = probe.readRfCharacteristic(id(8), id(4), id(6), characteristic);
    if (dispatchError) throw new Error(`read dispatch rejected: ${dispatchError.category}:${dispatchError.detail ?? ''}`);
    if (generation === 1) {
      // The host must receive this marker before the external reload notification.
      // Do not consume the read callback or command terminal in this generation.
      report(`${prefix}:read-dispatched:commandId=${id(8)}:connectionId=${id(4)}:gattSessionId=${id(6)}:characteristicId=${characteristic}:peripheralId=${peripheral}`);
      while (!isCancelled()) {
        await delay(100);
        stream.assertHealthy();
      }
    } else {
      report(`${prefix}:read-dispatched:commandId=${id(8)}:connectionId=${id(4)}:gattSessionId=${id(6)}:characteristicId=${characteristic}`);
      const readDeadline = Date.now() + RF_CONNECTION_TIMEOUT_MS;
      const read = await stream.waitForNext(Math.max(1, readDeadline - Date.now()), 'characteristic read', isCancelled);
      if (read.kind !== 'characteristicRead' || read.commandId !== id(8) || read.connectionId !== id(4) ||
          read.gattSessionId !== id(6) || read.characteristicId !== characteristic || read.error ||
          !sameBytes(read.value ?? new ArrayBuffer(0), new Uint8Array([0x00]))) {
        throw new Error(`unexpected generation 2 read:kind=${read.kind}:commandId=${read.commandId ?? 'none'}`);
      }
      await expectRfConnectionEvent(stream, 'commandCompleted', id(8), undefined, readDeadline, isCancelled);
      // Keep the second listener alive briefly to catch a late first-generation callback.
      await delay(RF_POST_DISCONNECT_OBSERVATION_MS);
      stream.assertHealthy();
      if (armFailure) throw armFailure;
      report(`${prefix}:read:passed:commandId=${id(8)}:connectionId=${id(4)}:gattSessionId=${id(6)}:characteristicId=${characteristic}:value=00`);
    }
  } catch (error: unknown) {
    if (!isCancelled()) report(`${prefix}:failed:${describeError(error)}`);
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

  report(
    `rf:start:serviceUuid=${serviceUuid ?? 'missing'}:initCommandId=${RF_INITIALIZE_COMMAND}:startCommandId=${RF_START_COMMAND}:stopCommandId=${RF_STOP_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`,
  );

  try {
    if (!serviceUuid) throw new Error('rfServiceUuid is required for RF mode');
    stream = createRfEventStream(probe, connectMode, passiveDisconnectMode);
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
        (event.localName === undefined || event.localName === RF_EXPECTED_LOCAL_NAME),
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

        if (ioMode) {
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
          // Android's no-response terminal confirms only local/native submission;
          // the later readback is the separate fixture-side evidence.
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
      stream?.remove();
      if (stream) report('rf:listener:removed');
    } catch (error: unknown) {
      cleanupFailure = error;
      if (!isCancelled()) report(`rf:listener-removal:failed:${describeError(error)}`);
    }

    let closeSucceeded = false;
    try {
      await probe.close();
      closeSucceeded = true;
      if (!isCancelled()) report('rf:close:resolved');
    } catch (error: unknown) {
      cleanupFailure = cleanupFailure ?? error;
      if (!isCancelled()) report(`rf:close:rejected:${describeError(error)}`);
    }
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
        (subscriptionMode && !subscriptionEvidencePassed ? new Error(`missing verified ${subscriptionMode} evidence`) : undefined);
      if (finalFailure) {
        report(
          `${passiveDisconnectMode ? 'rf-passive-disconnect' : subscriptionMode === 'indication' ? 'rf-indication' : subscriptionMode === 'notification' ? 'rf-notification' : ioMode ? 'rf-io' : rssiMode ? 'rf-rssi' : gattMode ? 'rf-gatt' : connectMode ? 'rf-connect' : 'rf'}:failed:${describeError(finalFailure)}:serviceUuid=${serviceUuid ?? 'missing'}:scanSessionId=${RF_SCAN_SESSION}:peripheralId=${matchedPeripheralId}${connectMode ? `:connectionId=${RF_CONNECTION_ID}` : ''}`,
        );
      } else if (passiveDisconnectMode) {
        report(`rf-passive-disconnect:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:gattCommandId=${RF_GATT_COMMAND}:subscriptionId=${RF_NOTIFICATION_SUBSCRIPTION}:recoveryConnectCommandId=${RF_RECOVERY_CONNECT_COMMAND}:recoveryConnectionId=${RF_RECOVERY_CONNECTION_ID}:recoveryDisconnectCommandId=${RF_RECOVERY_DISCONNECT_COMMAND}:notificationValue=ef:passiveDisconnectCommandId=none:scanSessionId=${RF_SCAN_SESSION}`);
      } else if (subscriptionMode === 'indication') {
        report(`rf-indication:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:gattCommandId=${RF_GATT_COMMAND}:rejectionCommandId=${RF_INDICATION_REJECTION_COMMAND}:enableCommandId=${RF_INDICATION_ENABLE_COMMAND}:disableCommandId=${RF_INDICATION_DISABLE_COMMAND}:subscriptionId=${RF_INDICATION_SUBSCRIPTION}:indicationValue=ef:postDisableSilenceMs=${RF_POST_NOTIFICATION_DISABLE_OBSERVATION_MS}:disconnectCommandId=${RF_DISCONNECT_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`);
      } else if (subscriptionMode === 'notification') {
        report(`rf-notification:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:gattCommandId=${RF_GATT_COMMAND}:enableCommandId=${RF_NOTIFICATION_ENABLE_COMMAND}:disableCommandId=${RF_NOTIFICATION_DISABLE_COMMAND}:subscriptionId=${RF_NOTIFICATION_SUBSCRIPTION}:notificationValue=ef:postDisableSilenceMs=${RF_POST_NOTIFICATION_DISABLE_OBSERVATION_MS}:disconnectCommandId=${RF_DISCONNECT_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`);
      } else if (rssiMode) {
        report(`rf-rssi:passed:serviceUuid=${serviceUuid}:peripheralId=${matchedPeripheralId}:connectionId=${RF_CONNECTION_ID}:connectCommandId=${RF_CONNECT_COMMAND}:rssiCommandId=${RF_RSSI_COMMAND}:rssiDbm=${rssiValue}:disconnectCommandId=${RF_DISCONNECT_COMMAND}:scanSessionId=${RF_SCAN_SESSION}`);
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

function StepRow({ step }: { step: Pick<Step, 'label' | 'state' | 'detail'> }): React.JSX.Element {
  const indicator = step.state === 'passed' ? 'OK' : step.state === 'failed' ? 'FAIL' : '--';
  return (
    <View style={styles.stepRow}>
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

export default function App({ runtimeMode = 'basic', rfServiceUuid, rfMultiNotificationMode, rfMultiSubscriptionMode, rfMultiCycles, rfReloadNotificationName }: AppProps): React.JSX.Element {
  const [steps, setSteps] = useState<Step[]>(INITIAL_STEPS);
  const [rfSteps, setRfSteps] = useState<RfStep[]>(INITIAL_RF_STEPS);
  const [statusLines, setStatusLines] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [closed, setClosed] = useState(false);
  const [runCount, setRunCount] = useState(0);
  const activeRef = useRef(true);
  const probeRef = useRef<Probe | undefined>(undefined);
  const pendingReadGeneration = useRef(runtimeMode === 'rf-pending-read-reload'
    ? Settings.get(PENDING_READ_GENERATION_KEY) ?? 1 : 1).current;

  const report = useCallback((line: string): void => {
    console.log(`BLEHUB_NITRO_RUNTIME ${line}`);
    if (!activeRef.current) return;
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
  }, []);

  const startSmoke = useCallback((): void => {
    if (running) return;
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
  }, [report, running]);

  const startRf = useCallback((): void => {
    if (running) return;
    const probe = createBleHubProbe();
    probeRef.current = probe;
    setRunning(true);
    setClosed(false);
    setRunCount((current) => current + 1);
    if (runtimeMode === 'rf-pending-read-reload') {
      setStatusLines([]);
      void runRfPendingReadReload(probe, rfServiceUuid, rfReloadNotificationName, pendingReadGeneration, report, () => !activeRef.current)
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
    if (runtimeMode === 'rf-recovery' || runtimeMode === 'rf-recovery-ten') {
      setRfSteps(runtimeMode === 'rf-recovery-ten' ? INITIAL_RF_RECOVERY_TEN_STEPS : INITIAL_RF_RECOVERY_STEPS);
      setStatusLines([]);
      void runRfRecovery(probe, rfServiceUuid, report, () => !activeRef.current, runtimeMode === 'rf-recovery-ten' ? 10 : 3)
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
    const passiveDisconnectMode = runtimeMode === 'rf-passive-disconnect';
    const subscriptionMode: RfSubscriptionMode | undefined = runtimeMode === 'rf-notification' || passiveDisconnectMode
      ? 'notification'
      : runtimeMode === 'rf-indication' ? 'indication' : undefined;
    const connectMode = runtimeMode === 'rf-connect' || runtimeMode === 'rf-gatt' || ioMode || rssiMode || subscriptionMode !== undefined;
    const gattMode = runtimeMode === 'rf-gatt' || ioMode || subscriptionMode !== undefined;
    setRfSteps(passiveDisconnectMode ? INITIAL_RF_PASSIVE_DISCONNECT_STEPS : subscriptionMode === 'notification' ? INITIAL_RF_NOTIFICATION_STEPS : subscriptionMode === 'indication' ? INITIAL_RF_INDICATION_STEPS : ioMode ? INITIAL_RF_IO_STEPS : rssiMode ? INITIAL_RF_RSSI_STEPS : gattMode ? INITIAL_RF_GATT_STEPS : connectMode ? INITIAL_RF_CONNECT_STEPS : INITIAL_RF_STEPS);
    setStatusLines([]);
    void runRfScan(probe, rfServiceUuid, report, () => !activeRef.current, connectMode, gattMode, ioMode, subscriptionMode, passiveDisconnectMode, rssiMode)
      .finally(() => setRunning(false));
  }, [report, rfServiceUuid, rfMultiNotificationMode, rfMultiSubscriptionMode, rfMultiCycles, rfReloadNotificationName, running, runtimeMode, pendingReadGeneration]);

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
      if (isRfMode(runtimeMode)) probeRef.current?.removeRfScanListener();
      else probeRef.current?.removeCompletionListener();
      const closing = probeRef.current?.close();
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
  const rfFailed = statusLines.some((line) => line.startsWith('rf:failed:') || line.startsWith('rf:close:rejected:'));
  const rfConnectFailed = statusLines.some((line) => line.startsWith('rf-connect:failed:') || line.startsWith('rf-connect:close:rejected:'));
  const rfGattFailed = statusLines.some((line) => line.startsWith('rf-gatt:failed:') || line.startsWith('rf-gatt:close:rejected:'));
  const rfIoFailed = statusLines.some((line) => line.startsWith('rf-io:failed:') || line.startsWith('rf-io:close:rejected:'));
  const rfNotificationFailed = statusLines.some((line) => line.startsWith('rf-notification:failed:') || line.startsWith('rf-notification:close:rejected:'));
  const rfIndicationFailed = statusLines.some((line) => line.startsWith('rf-indication:failed:') || line.startsWith('rf-indication:close:rejected:'));
  const rfPassiveDisconnectFailed = statusLines.some((line) => line.startsWith('rf-passive-disconnect:failed:') || line.startsWith('rf-passive-disconnect:close:rejected:'));
  const rfMultiPassed = statusLines.some((line) => line.startsWith('rf-multi:passed:'));
  const rfMultiFailed = statusLines.some((line) => line.startsWith('rf-multi:failed:') || line.startsWith('rf-multi:close:rejected:'));
  const rfMultiFaultPassed = statusLines.some((line) => line.startsWith('rf-multi-fault:passed:'));
  const rfMultiFaultFailed = statusLines.some((line) => line.startsWith('rf-multi-fault:failed:') || line.startsWith('rf-multi-fault:close:rejected:'));
  const runtimeStatus = runtimeMode === 'rf-recovery-ten'
    ? running ? 'running RF recovery (10 cycles)' : statusLines.some((line) => line.startsWith('rf-recovery-ten:passed:')) ? 'RF recovery 10 cycles passed' : statusLines.some((line) => line.startsWith('rf-recovery-ten:failed:')) ? 'RF recovery 10 cycles failed' : closed ? 'closed' : 'ready'
    : runtimeMode === 'rf-pending-read-reload'
    ? statusLines.some((line) => line.startsWith(`rf-pending-read-reload:generation=${pendingReadGeneration}:failed:`)) ? 'RF pending read reload failed' : statusLines.some((line) => line.startsWith('rf-pending-read-reload:generation=2:read:passed:')) ? 'RF pending read reload passed' : running ? `running RF pending read generation ${pendingReadGeneration}` : 'awaiting external reload'
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
      : runtimeMode === 'rf-io'
        ? running ? 'running RF GATT I/O gate' : rfIoPassed ? 'RF GATT I/O passed' : rfIoFailed ? 'RF GATT I/O failed' : closed ? 'closed' : 'ready'
        : runtimeMode === 'rf-gatt'
          ? running ? 'running RF GATT gate' : rfGattPassed ? 'RF GATT passed' : rfGattFailed ? 'RF GATT failed' : closed ? 'closed' : 'ready'
    : runtimeMode === 'stress'
      ? running ? 'running lifecycle stress' : 'lifecycle stress complete'
      : runtimeMode === 'reload'
        ? running ? 'preparing runtime reload' : 'waiting for native reload'
        : running ? 'running contract smoke' : closed && passedCount === steps.length ? 'contract passed' : closed ? 'closed' : 'ready';

  return (
    <View style={styles.safeArea} testID={runtimeMode === 'rf' ? 'rf-scan-lab-root' : runtimeMode === 'rf-connect' ? 'rf-connect-lab-root' : runtimeMode === 'rf-gatt' ? 'rf-gatt-lab-root' : runtimeMode === 'rf-io' ? 'rf-io-lab-root' : runtimeMode === 'rf-notification' ? 'rf-notification-lab-root' : runtimeMode === 'rf-indication' ? 'rf-indication-lab-root' : runtimeMode === 'rf-passive-disconnect' ? 'rf-passive-disconnect-lab-root' : runtimeMode === 'rf-recovery' ? 'rf-recovery-lab-root' : runtimeMode === 'rf-recovery-ten' ? 'rf-recovery-ten-lab-root' : runtimeMode === 'rf-multi' ? 'rf-multi-lab-root' : runtimeMode === 'rf-reload' ? 'rf-reload-lab-root' : 'contract-lab-root'}>
      <ScrollView contentContainerStyle={styles.root} contentInsetAdjustmentBehavior="automatic">
        <View style={styles.header}>
          <View style={styles.headerCopy}>
            <Text style={styles.eyebrow}>BLEHUB / RN NITRO</Text>
            <Text style={styles.title} testID={runtimeMode === 'rf' ? 'rf-scan-title' : runtimeMode === 'rf-connect' ? 'rf-connect-title' : runtimeMode === 'rf-gatt' ? 'rf-gatt-title' : runtimeMode === 'rf-io' ? 'rf-io-title' : runtimeMode === 'rf-notification' ? 'rf-notification-title' : runtimeMode === 'rf-indication' ? 'rf-indication-title' : runtimeMode === 'rf-passive-disconnect' ? 'rf-passive-disconnect-title' : runtimeMode === 'rf-recovery' ? 'rf-recovery-title' : runtimeMode === 'rf-multi' ? 'rf-multi-title' : 'contract-lab-title'}>
              {runtimeMode === 'rf' ? 'iPhone RF scan gate' : runtimeMode === 'rf-connect' ? 'iPhone RF connect gate' : runtimeMode === 'rf-gatt' ? 'iPhone RF GATT gate' : runtimeMode === 'rf-io' ? 'iPhone RF GATT I/O gate' : runtimeMode === 'rf-notification' ? 'iPhone RF notification gate' : runtimeMode === 'rf-indication' ? 'iPhone RF strict Indication gate' : runtimeMode === 'rf-passive-disconnect' ? 'iPhone RF passive disconnect/recovery gate' : runtimeMode === 'rf-recovery' ? 'iPhone RF three-cycle recovery gate' : runtimeMode === 'rf-multi' ? 'RN RF multi-DK gate' : 'Contract lab'}
            </Text>
            <Text style={styles.subtitle}>
              {runtimeMode === 'rf'
                ? 'JS → Nitro → production Apple BleHub scan evidence; this mode does not run the default contract smoke.'
                : runtimeMode === 'rf-connect'
                  ? 'JS → Nitro → production Apple BleHub scan, connect, and disconnect evidence.'
                  : runtimeMode === 'rf-gatt'
                    ? 'JS → Nitro → production Apple BleHub scan, connect, aggregate GATT discovery, and disconnect evidence.'
                    : runtimeMode === 'rf-io'
                      ? 'JS → Nitro → production Apple BleHub scan, connect, GATT discovery, read/write, and disconnect evidence.'
                      : runtimeMode === 'rf-notification'
                        ? 'JS → Nitro → production Apple BleHub notification enable, value, disable, and disconnect evidence.'
                        : runtimeMode === 'rf-indication'
                          ? 'JS → Nitro → production Apple BleHub strict Indication rejection, enable, value, disable, and disconnect evidence.'
                          : runtimeMode === 'rf-passive-disconnect'
                            ? 'JS → Nitro → production Apple BleHub passive disconnect, stale-route silence, recovery connect, and cleanup evidence.'
                          : runtimeMode === 'rf-recovery'
                            ? 'One Nitro probe and event listener across three nRF passive-disconnect/recovery cycles.'
                          : runtimeMode === 'rf-multi'
                            ? 'One shared RN/Nitro probe keeps two independent nRF52840 DK connections active while checking identity, GATT, and interleaved I/O correlation.'
                          : 'A visible React Native surface for the Nitro and production BleHub assembly.'}
            </Text>
          </View>
          <View style={styles.statusCard}>
            <Text style={styles.statusCaption}>Runtime status</Text>
            <Text style={styles.statusCaption} testID="run-count">Run {runCount}</Text>
            <Text
              style={[styles.statusValue, (runtimeStatus === 'contract passed' || runtimeStatus === 'RF scan passed' || runtimeStatus === 'RF connect passed' || runtimeStatus === 'RF GATT passed' || runtimeStatus === 'RF GATT I/O passed' || runtimeStatus === 'RF notification passed' || runtimeStatus === 'RF strict Indication passed' || runtimeStatus === 'RF passive disconnect/recovery passed' || runtimeStatus === 'RF recovery passed') && styles.successText]}
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
            disabled={running}
            testID={runtimeMode === 'rf' ? 'rerun-rf-scan-button' : runtimeMode === 'rf-connect' ? 'rerun-rf-connect-button' : runtimeMode === 'rf-gatt' ? 'rerun-rf-gatt-button' : runtimeMode === 'rf-io' ? 'rerun-rf-io-button' : runtimeMode === 'rf-notification' ? 'rerun-rf-notification-button' : runtimeMode === 'rf-indication' ? 'rerun-rf-indication-button' : runtimeMode === 'rf-passive-disconnect' ? 'rerun-rf-passive-disconnect-button' : runtimeMode === 'rf-recovery' ? 'rerun-rf-recovery-button' : 'recreate-run-button'}
            accessibilityRole="button"
          >
            <Text style={styles.primaryButtonText}>
              {running ? 'Running…' : runtimeMode === 'rf' ? 'Run RF scan again' : runtimeMode === 'rf-connect' ? 'Run RF connect again' : runtimeMode === 'rf-gatt' ? 'Run RF GATT again' : runtimeMode === 'rf-io' ? 'Run RF GATT I/O again' : runtimeMode === 'rf-notification' ? 'Run RF notification again' : runtimeMode === 'rf-indication' ? 'Run RF strict Indication again' : runtimeMode === 'rf-passive-disconnect' ? 'Run RF passive disconnect/recovery again' : runtimeMode === 'rf-recovery' ? 'Run RF recovery again' : closed ? 'Recreate and run' : 'Run contract smoke again'}
            </Text>
          </Pressable>
          <Pressable
            style={[styles.button, styles.secondaryButton]}
            onPress={closeProbe}
            disabled={closed || running}
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
            {rfSteps.map((step) => <StepRow key={step.id} step={step} />)}
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

        <View style={styles.card}>
          <View style={styles.cardHeader}>
            <Text style={styles.cardTitle}>Projected runtime events</Text>
            <Text style={styles.badge}>{statusLines.length}</Text>
          </View>
          {statusLines.length ? statusLines.map((line, index) => (
            <Text key={`${index}-${line}`} style={styles.eventLine}>{line}</Text>
          )) : <Text style={styles.emptyText}>Waiting for the Nitro object to report.</Text>}
        </View>

        <Text style={styles.footer}>
          {isRfMode(runtimeMode)
            ? 'RF mode is an iPhone JS→Nitro gate; Android production backend RF projection is not enabled in this private probe yet.'
            : 'This is a Nitro transport/lifecycle gate, not BLE RF or the public BLeng surface.'}
        </Text>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: '#f5f7fa' },
  root: { padding: 20, gap: 16 },
  header: { gap: 16 },
  headerCopy: { gap: 6 },
  eyebrow: { color: '#526173', fontSize: 12, fontWeight: '700', letterSpacing: 1.2 },
  title: { color: '#122033', fontSize: 30, fontWeight: '800' },
  subtitle: { color: '#526173', fontSize: 15, lineHeight: 22 },
  statusCard: { borderLeftWidth: 3, borderLeftColor: '#218739', paddingLeft: 12, gap: 4 },
  statusCaption: { color: '#526173', fontSize: 12, fontWeight: '600' },
  statusValue: { color: '#122033', fontSize: 16, fontWeight: '700' },
  actions: { flexDirection: 'row', flexWrap: 'wrap', gap: 10 },
  button: { borderRadius: 8, paddingHorizontal: 14, paddingVertical: 11 },
  primaryButton: { backgroundColor: '#1769aa' },
  secondaryButton: { backgroundColor: '#e7edf3' },
  primaryButtonText: { color: '#ffffff', fontSize: 14, fontWeight: '700' },
  secondaryButtonText: { color: '#17324d', fontSize: 14, fontWeight: '700' },
  card: { backgroundColor: '#ffffff', borderRadius: 10, padding: 16, gap: 8 },
  cardHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 },
  cardTitle: { color: '#122033', fontSize: 18, fontWeight: '800' },
  badge: { color: '#1769aa', fontSize: 14, fontWeight: '800' },
  stepRow: { flexDirection: 'row', alignItems: 'center', gap: 10, borderBottomWidth: 1, borderBottomColor: '#e4e9ef', paddingVertical: 10 },
  stepIndicator: { color: '#8a98a8', fontSize: 11, fontWeight: '800', width: 32 },
  stepCopy: { flex: 1, gap: 2 },
  stepLabel: { color: '#243447', fontSize: 14, fontWeight: '600' },
  stepDetail: { color: '#718096', fontSize: 12 },
  successText: { color: '#218739' },
  errorText: { color: '#b42318' },
  eventLine: { color: '#526173', fontFamily: 'Menlo', fontSize: 12, paddingVertical: 4 },
  emptyText: { color: '#718096', fontSize: 14 },
  footer: { color: '#718096', fontSize: 12, lineHeight: 18, paddingBottom: 20 },
});
