import React, { useRef, useState } from 'react'
import { Button, ScrollView, StyleSheet, Text, View } from 'react-native'
import { initialWindowMetrics, SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context'
import { createBlengSessionOwner } from '@blehub/react-native-bleng'
import type { BlengAdapterSnapshot, BlengSessionOwner } from '@blehub/react-native-bleng'
import { ManualScan, ScanSummary } from './ManualScan'
import { useManualScan } from './useManualScan'
import type { ScanAction } from './useManualScan'

type SessionState = 'idle' | 'starting' | 'started' | 'closing' | 'closed' | 'closedWithError' | 'failed'
type WorkbenchPage = 'Tests' | 'Current run' | 'Evidence'
type ActionResult = 'pending' | 'succeeded' | 'failed'
interface Observation {
  sequence: number
  action: 'start' | 'close' | ScanAction
  result: ActionResult
  error?: string
}
interface RunEvidence {
  run: number
  entries: Observation[]
  omitted: number
  snapshot?: BlengAdapterSnapshot
}

// This is a bounded UI summary, not the host's durable evidence bundle.
const EVIDENCE_LIMIT = 32
const pages = [
  ['tests', 'Tests'], ['current', 'Current run'], ['evidence', 'Evidence'],
] as const

// Fixed phase labels only: diagnostics must not become a result/identity API.
function traceClosePhase(phase: string): void {
  try {
    console.info(`BLEHUB_FORMAL_CLOSE phase=${phase} monotonicMs=${performance.now()}`)
  } catch {
    // A diagnostic sink must not change operation submission or settlement.
  }
}

export default function App(): React.JSX.Element {
  return (
    <SafeAreaProvider initialMetrics={initialWindowMetrics}>
      <Workbench />
    </SafeAreaProvider>
  )
}

function Workbench(): React.JSX.Element {
  // Run ownership lives above the view switch. Browsing never creates, retires or replaces it.
  const ownerRef = useRef<BlengSessionOwner | undefined>(undefined)
  const actionPending = useRef(false)
  const runCounter = useRef(0)
  const actionCounter = useRef(0)
  const [page, setPage] = useState<WorkbenchPage>('Tests')
  const [state, setState] = useState<SessionState>('idle')
  const [snapshot, setSnapshot] = useState<BlengAdapterSnapshot>()
  const [error, setError] = useState<string>()
  const [evidence, setEvidence] = useState<RunEvidence>()
  const busy = state === 'starting' || state === 'closing'

  const record = (action: Observation['action'], newRun = false): number => {
    const sequence = ++actionCounter.current
    if (newRun) ++runCounter.current
    const run = runCounter.current
    setEvidence(previous => {
      const current = newRun || !previous ? { run, entries: [], omitted: 0 } : previous
      const entries: Observation[] = [...current.entries, { sequence, action, result: 'pending' }]
      const omitted = Math.max(0, entries.length - EVIDENCE_LIMIT)
      return { ...current, entries: entries.slice(-EVIDENCE_LIMIT), omitted: current.omitted + omitted }
    })
    return sequence
  }

  const settle = (sequence: number, result: ActionResult, detail?: string): void => {
    setEvidence(current => current && {
      ...current,
      entries: current.entries.map(entry => entry.sequence === sequence
        ? { ...entry, result, error: detail } : entry),
    })
  }

  const scan = useManualScan({ ownerRef, ownerReady: state === 'started' && !!snapshot,
    controlsEnabled: !busy && !!snapshot, record, settle })

  const start = async (): Promise<void> => {
    if (actionPending.current || snapshot) return
    actionPending.current = true
    const sequence = record('start', !ownerRef.current)
    if (!ownerRef.current) scan.reset()
    setPage('Current run')
    setState('starting')
    setError(undefined)
    try {
      const owner = ownerRef.current ?? createBlengSessionOwner()
      ownerRef.current = owner
      const observed = await owner.start()
      setSnapshot(observed)
      setEvidence(current => current && { ...current, snapshot: { ...observed } })
      settle(sequence, 'succeeded')
      setState('started')
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : String(cause)
      settle(sequence, 'failed', detail)
      setError(detail)
      setState('failed')
    } finally {
      actionPending.current = false
    }
  }

  const close = async (): Promise<void> => {
    if (actionPending.current) return
    const owner = ownerRef.current
    if (!owner) {
      setState(previous => previous === 'closedWithError' ? previous : 'closed')
      return
    }
    actionPending.current = true
    const sequence = record('close')
    scan.ownerClosing()
    setState('closing')
    setError(undefined)
    try {
      traceClosePhase('js.close.call')
      const pendingClose = owner.close()
      traceClosePhase('js.close.promise')
      const result = await pendingClose
      traceClosePhase('js.close.receipt')
      const detail = result.failure && `${result.failure.code}: ${result.failure.message}`
      if (result.ownerClosed) {
        // Logical closure retires controls even when cleanup failed; keep that failure visible.
        scan.ownerClosed()
        ownerRef.current = undefined
        setSnapshot(undefined)
      }
      if (detail || !result.ownerClosed) {
        const failure = detail ?? 'Close did not confirm owner closure.'
        settle(sequence, 'failed', failure)
        setError(failure)
        setState(result.ownerClosed ? 'closedWithError' : 'failed')
        return
      }
      settle(sequence, 'succeeded')
      setState('closed')
    } catch (cause) {
      traceClosePhase('js.close.rejected')
      const detail = cause instanceof Error ? cause.message : String(cause)
      settle(sequence, 'failed', detail)
      setError(detail)
      setState('failed')
    } finally {
      actionPending.current = false
      traceClosePhase('js.close.finally')
    }
  }

  return (
    <SafeAreaView style={styles.safeArea} mode="padding" edges={['top', 'right', 'bottom', 'left']}>
      <View style={styles.container}>
        <Text testID="formal-host-title" style={styles.eyebrow}>BLEHUB FORMAL RN CONSUMER</Text>
        <View style={styles.navigation}>
          {pages.map(([identifier, title]) => (
            <Button key={identifier} testID={`workbench-${identifier}`} title={title}
              accessibilityState={{ selected: page === title }} onPress={() => setPage(title)} />
          ))}
        </View>
        <View style={styles.statusCard}>
          <Text style={styles.statusCaption}>SESSION STATE</Text>
          <Text testID="session-state" style={styles.status}>{state}</Text>
          {error ? <Text testID="session-error" style={styles.error}>{error}</Text> : null}
          {state === 'closedWithError' ? (
            <Text testID="session-retirement-warning" style={styles.error}>
              Owner is closed, but cleanup failed. This is not resource-zero or a passed host gate.
              Only an explicit Start session creates another owner; no cleanup retry is implied.
            </Text>
          ) : null}
        </View>
        <ScrollView contentContainerStyle={styles.content}>
          <Text testID="workbench-page" style={styles.title}>{page}</Text>
          {page === 'Tests' ? (
            <>
              <Text style={styles.description}>
                Session lifecycle observes initialization, then explicit close. Starting a new session
                replaces the previous UI evidence; browsing does not run BLE commands.
              </Text>
              <Button testID="start-session" title="Start session" onPress={start} disabled={busy || !!snapshot} />
              <Text style={styles.description}>
                Scan is available only as an explicit manual typed flow in Current run. Connection,
                GATT, notifications and RF full-suite are not migrated. These UI observations do not
                replace the legacy RF suite or host verdict.
              </Text>
            </>
          ) : null}
          {page === 'Current run' ? (
            <>
              <Text style={styles.description}>
                Initialization is not a guarantee that Bluetooth is powered on or access is granted.
                Switching views leaves this run and pending operations intact.
              </Text>
              {snapshot ? <SnapshotCard snapshot={snapshot} /> : null}
              <View style={styles.actions}>
                <Button testID="start-session" title="Start session" onPress={start} disabled={busy || !!snapshot} />
                <Button testID="close-session" title="Close session" onPress={close} disabled={busy} />
              </View>
              <ManualScan scan={scan} />
            </>
          ) : null}
          {page === 'Evidence' ? (
            <>
              <Text style={styles.description}>
                UI observations only — not a host verdict, RF result or proof of native resource-zero.
                Kept in this App runtime until the next explicit session; not persisted across reload.
              </Text>
              {evidence ? (
                <>
                  <Text testID="evidence-run">Run {evidence.run}</Text>
                  {evidence.omitted > 0 ? (
                    <Text testID="evidence-omitted">{evidence.omitted} earlier observations omitted from this bounded summary.</Text>
                  ) : null}
                  <Text testID="lifecycle-evidence">{evidence.entries.map(entry =>
                    `${entry.sequence}. ${entry.action}: ${entry.result}${entry.error === undefined ? '' : ` — ${entry.error}`}`
                  ).join('\n')}</Text>
                  {evidence.snapshot ? <SnapshotCard snapshot={evidence.snapshot} prefix="evidence-" /> : null}
                  <ScanSummary scan={scan} />
                </>
              ) : <Text testID="lifecycle-evidence">No run has been requested.</Text>}
            </>
          ) : null}
        </ScrollView>
      </View>
    </SafeAreaView>
  )
}

function SnapshotCard({ snapshot, prefix = '' }: {
  snapshot: BlengAdapterSnapshot
  prefix?: string
}): React.JSX.Element {
  return (
    <View style={styles.statusCard}>
      <Text style={styles.statusCaption}>INITIALIZATION SNAPSHOT — NOT LIVE</Text>
      <Text>Adapter state</Text>
      <Text testID={`${prefix}adapter-state`}>{snapshot.state}</Text>
      <Text>Scan authorization</Text>
      <Text testID={`${prefix}scan-authorization`}>{snapshot.scanAuthorization}</Text>
      <Text>Connection authorization</Text>
      <Text testID={`${prefix}connection-authorization`}>{snapshot.connectionAuthorization}</Text>
      <Text>Adapter name</Text>
      <Text testID={`${prefix}adapter-name`}>{snapshot.name ?? 'not reported'}</Text>
    </View>
  )
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: '#f7f8fa' },
  container: { flex: 1, padding: 16, gap: 12 },
  content: { gap: 12, paddingBottom: 24 },
  navigation: { flexDirection: 'row', justifyContent: 'space-between' },
  eyebrow: { color: '#5865f2', fontSize: 12, fontWeight: '700', letterSpacing: 1 },
  title: { color: '#15171a', fontSize: 24, fontWeight: '700' },
  description: { color: '#50545a', fontSize: 14, lineHeight: 20 },
  statusCard: { backgroundColor: '#ffffff', borderRadius: 12, padding: 12, gap: 4 },
  statusCaption: { color: '#777d85', fontSize: 12, fontWeight: '700' },
  status: { color: '#15171a', fontSize: 22, fontWeight: '600' },
  error: { color: '#b42318', fontSize: 14 },
  actions: { gap: 8 },
})
