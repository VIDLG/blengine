import React, { useEffect, useState } from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import type { EventError } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import { createBleHubProbe } from '@blehub/react-native-internal';

const MAX_U64 = '18446744073709551615';
const LISTENER_REMOVAL_COMMAND = '1';
const LISTENER_REMOVAL_WAIT_MS = 100;
const STRESS_CYCLES = 8;
const STRESS_WAIT_MS = 150;
const COMPLETION_TIMEOUT_MS = 5_000;
const SAMPLE_BYTES = new Uint8Array([0x01, 0x02, 0x7f, 0x00]);
const INVALID_COMMAND_IDS = ['', '00', '01', '-1', '1.0', '18446744073709551616'];

type Probe = ReturnType<typeof createBleHubProbe>;
type RuntimeMode = 'basic' | 'stress';

interface AppProps {
  runtimeMode?: RuntimeMode;
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

async function runBasicProbe(probe: Probe, report: (line: string) => void): Promise<void> {
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
    const roundTrip = probe.roundTrip({ id: 'bytes', bytes: SAMPLE_BYTES.buffer, error: sampleError });
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

async function runStressProbe(
  report: (line: string) => void,
  isCancelled: () => boolean,
  setCurrentProbe: (probe: Probe | undefined) => void,
): Promise<void> {
  report(`stress:start:${STRESS_CYCLES}`);
  let recreateClosePassed = true;
  let lateCallbackPassed = true;
  let postCloseDispatchPassed = true;

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
      // The production Apple backend accepts only one initialize transition.
      // A post-completion invalid_state is expected; either way, no callback
      // may arrive after the listener route has been removed.
      if (removedListenerDispatchError && removedListenerDispatchError.category !== 'invalid_state') {
        throw new Error(`post-removal dispatch rejected: ${removedListenerDispatchError.category}`);
      }
      await delay(STRESS_WAIT_MS);
      if (completionCount !== countAfterListenerRemoval) {
        throw new Error('completion delivered after listener removal');
      }

      const firstClose = probe.close();
      const secondClose = probe.close();
      await Promise.all([firstClose, secondClose]);
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
      // close() is idempotent; this also drains a failed cycle before recreation.
      await probe.close().catch(() => undefined);
    }
  }

  if (isCancelled()) return;
  report(`stress:recreate-close:${recreateClosePassed ? 'passed' : 'failed'}`);
  report(`stress:late-callback:${lateCallbackPassed ? 'passed' : 'failed'}`);
  report(`stress:post-close-dispatch:${postCloseDispatchPassed ? 'passed' : 'failed'}`);
  report(
    `stress:complete:${recreateClosePassed && lateCallbackPassed && postCloseDispatchPassed ? 'passed' : 'failed'}`,
  );
}

export default function App({ runtimeMode = 'basic' }: AppProps): React.JSX.Element {
  const [status, setStatus] = useState<string[]>(['runtime:waiting']);

  useEffect(() => {
    let active = true;

    let currentProbe: Probe | undefined;
    let cancelled = false;

    const report = (line: string): void => {
      console.log(`BLEHUB_NITRO_RUNTIME ${line}`);
      if (active) setStatus((current) => [...current, line]);
    };

    const run = async (): Promise<void> => {
      if (runtimeMode === 'stress') {
        await runStressProbe(report, () => cancelled, (probe) => {
          currentProbe = probe;
        });
        return;
      }

      const probe = createBleHubProbe();
      currentProbe = probe;
      await runBasicProbe(probe, report);
    };

    void run().catch((error: unknown) => report(`run:failed:${describeError(error)}`));

    return () => {
      active = false;
      cancelled = true;

      currentProbe?.removeCompletionListener();
      void currentProbe?.close();
    };
  }, [runtimeMode]);

  return (
    <View style={styles.root} testID="macos-contract-lab-root">
      <ScrollView contentContainerStyle={styles.content}>
        <Text style={styles.eyebrow}>BLEHUB / RN MACOS</Text>
        <View accessible accessibilityLabel="Contract lab" testID="macos-contract-lab-title">
          <Text style={styles.title}>Contract lab</Text>
        </View>
        <Text style={styles.subtitle}>Nitro and production BleHub · {runtimeMode} probe</Text>
        <View
          accessible
          accessibilityLabel={`Runtime events\n${status.join('\n')}`}
          style={styles.card}
          testID="macos-runtime-events"
        >
          <Text style={styles.sectionTitle}>Runtime events</Text>
          {status.map((line, index) => (
            <Text key={`${index}-${line}`} style={styles.eventLine}>{line}</Text>
          ))}
        </View>
        <Text style={styles.footer}>Internal transport/lifecycle validation; no BLE RF or public BLeng surface.</Text>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: '#f5f7fa' },
  content: { padding: 24, gap: 16 },
  eyebrow: { color: '#526173', fontSize: 12, fontWeight: '700' },
  title: { color: '#122033', fontSize: 30, fontWeight: '800' },
  subtitle: { color: '#526173', fontSize: 15 },
  card: { backgroundColor: '#ffffff', borderRadius: 10, padding: 16, gap: 8 },
  sectionTitle: { color: '#122033', fontSize: 18, fontWeight: '700' },
  eventLine: { color: '#243447', fontSize: 14 },
  footer: { color: '#718096', fontSize: 12 },
});
