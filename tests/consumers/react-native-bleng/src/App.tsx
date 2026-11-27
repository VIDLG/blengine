import React, { useEffect, useRef, useState } from 'react'
import { Ids } from './ids'
import { Button, Keyboard, ScrollView, StyleSheet, Text, View } from 'react-native'
import { initialWindowMetrics, SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context'
import { createBlengCentral, requireOutcome } from '@blehub/react-native-bleng'
import type { BlengAdapterSnapshot, BlengCentral } from '@blehub/react-native-bleng'
import {
  GattPanel, ManualScanDetails, NotifyPanel, ScanConfiguration, ScanSummary,
} from './ManualScan'
import { CommandConsole } from './CommandConsole'
import { ProbeConsole } from './ProbeConsole'
import { encodeActionLedger } from './actionLedger'
import { useManualScan } from './useManualScan'
import type { ScanAction } from './useManualScan'
import { encodeRunStatus, runStatusFacts } from './runStatus'

type SessionState = 'idle' | 'starting' | 'started' | 'closing' | 'closed' | 'closedWithError' | 'failed'
type WorkbenchPage = 'Tests' | 'Current run' | 'Probe' | 'Evidence'
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
  ['tests', 'Tests'], ['current', 'Current run'], ['probe', 'Probe'], ['evidence', 'Evidence'],
] as const

// Fixed phase labels only: diagnostics must not become a result/identity API.
function traceClosePhase(phase: string): void {
  try {
    console.info(`BLEHUB_FORMAL_CLOSE phase=${phase} monotonicMs=${performance.now()}`)
  } catch {
    // A diagnostic sink must not change operation submission or settlement.
  }
}

function traceNavigationPhase(phase: 'request' | 'commit', page: WorkbenchPage): void {
  try {
    console.info(`BLEHUB_FORMAL_NAV phase=${phase} page=${page} monotonicMs=${performance.now()}`)
  } catch {
    // Logging must not prevent navigation or interrupt a pending run.
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
  const centralRef = useRef<BlengCentral | undefined>(undefined)
  const actionPending = useRef(false)
  const runCounter = useRef(0)
  const actionCounter = useRef(0)
  const [page, setPage] = useState<WorkbenchPage>('Tests')
  const [state, setState] = useState<SessionState>('idle')
  const [snapshot, setSnapshot] = useState<BlengAdapterSnapshot>()
  const [error, setError] = useState<string>()
  const [evidence, setEvidence] = useState<RunEvidence>()
  const busy = state === 'starting' || state === 'closing'

  // Post-commit JS observation, not a native frame/accessibility acknowledgement.
  useEffect(() => { traceNavigationPhase('commit', page) }, [page])

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

  const scan = useManualScan({ centralRef, centralReady: state === 'started' && !!snapshot,
    controlsEnabled: !busy && !!snapshot, record, settle })
  // Write payload is a screen input, not run state. The declared characteristic lives in
  // the scan controller, because the projection resolves it against its own topology.
  const [writeText, setWriteText] = useState('01')
  // One pinned machine-readable snapshot of every scalar fact a scene polls for. It is
  // derived in this render pass from the same state the labelled panels read, so the two
  // cannot disagree; see `runStatus.ts` for why a scene needs it as one node.
  const runStatus = encodeRunStatus(runStatusFacts({
    state, page, view: scan.view, timeoutText: scan.timeoutText,
    filterText: scan.filterText, gattTarget: scan.gattTarget,
    gattMatch: scan.gattTargetStatus, gattProps: scan.gattTargetProperties,
    gattReads: scan.gattReads,
    gattReadTotal: scan.gattReadTotal,
    // A monotonic settled-read count for the CCCD lane: the scene advances it so a read is
    // proven fresh even when the expected bytes equal the value already on screen.
    descriptorReadTotal: scan.descriptorReadTotal,
    // The run's newest failure sentence, whichever lane published it; the projection
    // reduces it to an identity and the sentence itself stays in the panels a human reads.
    // `view.error` is the scan/link lane — the run's own level — so it leads: a failed connect
    // is published only there, and leaving it out made the machine surface say `failure=none`
    // while the panel and the App's own ledger both carried the failure.
    failureText: scan.view.error ?? scan.view.notifyError ?? scan.view.gattError ?? '',
  }))

  const start = async (): Promise<void> => {
    if (actionPending.current || snapshot) return
    actionPending.current = true
    const sequence = record('start', !centralRef.current)
    if (!centralRef.current) scan.reset()
    setPage('Current run')
    setState('starting')
    setError(undefined)
    try {
      const central = centralRef.current ?? createBlengCentral()
      centralRef.current = central
      const observed = await central.initialize()
      const initialized = requireOutcome(observed).snapshot
      setSnapshot(initialized)
      setEvidence(current => current && { ...current, snapshot: { ...initialized } })
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
    const central = centralRef.current
    if (!central) {
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
      const pendingClose = central.close()
      traceClosePhase('js.close.promise')
      const result = await pendingClose
      traceClosePhase('js.close.receipt')
      const detail = result.failure && `${result.failure.code}: ${result.failure.message}`
      if (result.closed) {
        // Logical closure retires controls even when cleanup failed; keep that failure visible.
        scan.ownerClosed()
        centralRef.current = undefined
        setSnapshot(undefined)
      }
      if (detail || !result.closed) {
        const failure = detail ?? 'Close did not confirm owner closure.'
        settle(sequence, 'failed', failure)
        setError(failure)
        setState(result.closed ? 'closedWithError' : 'failed')
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
        {/* ── Pinned: the machine surface ───────────────────────────────────────────
            Everything a UI scene reads or acts on lives here, outside every scroll
            container. Android omits off-viewport ScrollView children from the
            accessibility tree, so content a scene had to reach would have to be
            scrolled to — and a gesture is only as accurate as the automation server's
            injection. Keeping this block small enough to fit is therefore a hard
            requirement of the design, not a layout preference. */}
        {/* The title is also the App's tap-to-dismiss surface. A scene sets text fields,
            and the software keyboard it raises covers the lower half of the screen — where
            pinned controls live, so those controls are present, enabled and unreachable. The
            value is already applied on every keystroke, so the keyboard has no further job;
            the native scene driver retires it by tapping this title. Plans never tap it. */}
        <Text testID={Ids.workbench.title} onPress={() => { Keyboard.dismiss() }} style={styles.eyebrow}>BLEHUB FORMAL RN CONSUMER</Text>
        <View style={styles.navigation}>
          {pages.map(([identifier, title]) => (
            <Button key={identifier} testID={Ids.nav.page(identifier)} title={title}
              accessibilityState={{ selected: page === title }} onPress={() => {
                traceNavigationPhase('request', title)
                setPage(title)
              }} />
          ))}
        </View>
        <View style={styles.statusLine}>
          <Text testID={Ids.workbench.page} style={styles.pageTitle}>{page}</Text>
          <Text testID={Ids.workbench.state} style={styles.state}>{state}</Text>
        </View>
        {error ? <Text testID={Ids.workbench.error} style={styles.error}>{error}</Text> : null}
        {state === 'closedWithError' ? (
          <Text testID={Ids.workbench.retirementWarning} style={styles.error}>
            Owner is closed, but cleanup failed. This is not resource-zero or a passed host gate.
            Only an explicit Start session creates another owner; no cleanup retry is implied.
          </Text>
        ) : null}
        {/* One request, one consistent snapshot: every value a scene polls for. */}
        <Text testID={Ids.workbench.runStatus} style={styles.projection}>{runStatus}</Text>
        {/* The newest observation only, because that is what a scene matches against after
            each explicit pull. It belongs to the run, not to a page: browsing is never
            allowed to change what the run observed. */}
        {scan.view.total > 0 ? (
          <Text testID={Ids.workbench.scanLatest} style={styles.projection}>
            {scan.view.samples[scan.view.samples.length - 1]}
          </Text>
        ) : null}
        {scan.view.notifyTotal > 0 ? (
          <Text testID={Ids.workbench.notifyLatest} style={styles.projection}>
            {scan.view.notifySamples[scan.view.notifySamples.length - 1]}
          </Text>
        ) : null}
        {/* Session control is pinned on every page. A scene's first command is
            `start-session`, issued before any navigation, so this button must never live
            inside the scroll container: Android omits off-viewport ScrollView children, and
            the old placement only worked because it happened to be the first content node. */}
        <View testID={Ids.workbench.sessionControls} style={styles.sessionControls}>
          <View style={styles.action}>
            <Button testID={Ids.workbench.startSession} title="Start session" onPress={start} disabled={busy || !!snapshot} />
          </View>
          <View style={styles.action}>
            <Button testID={Ids.workbench.closeSession} title="Close session" onPress={close} disabled={busy} />
          </View>
        </View>
        {page === 'Current run' && snapshot ? (
          // The initialization snapshot is a run-level fact, not content: a scene reads
          // all of it as one block and cannot proceed without it.
          <SnapshotCard snapshot={snapshot} compact />
        ) : null}
        {page === 'Evidence' && evidence?.snapshot ? (
          // The same facts, pinned on Evidence too: a scene re-verifies the snapshot after
          // closing the run, and content would make that re-verification a gesture.
          <SnapshotCard snapshot={evidence.snapshot} prefix="evidence-" compact />
        ) : null}
        {page === 'Current run' ? (
          <CommandConsole scan={scan} writeText={writeText} setWriteText={setWriteText} />
        ) : null}
        {page === 'Probe' ? <ProbeConsole scan={scan} /> : null}
        {/* ── Machine ledger, last in the pinned block ─────────────────────────────
            A scene has to establish that the App recorded the actions it dispatched, but
            `lifecycle-evidence` is prose inside the scroll container, which a scene may
            not read. The same bounded list is therefore published here as machine data.
            It is the last pinned node on purpose: it grows with the run, and everything a
            scene taps must stay above it.
            ─────────────────────────────────────────────────────────────────────── */}
        <Text testID={Ids.workbench.actionLedger} style={styles.ledger}>
          {encodeActionLedger(evidence?.entries ?? [], evidence?.omitted ?? 0)}
        </Text>
        {/* ── Content: everything a human reads ─────────────────────────────────────
            Narration, the topology index, bounded sample lists and history. It may be
            arbitrarily long because nothing machine-relevant lives in it. */}
        <ScrollView testID={Ids.workbench.content} contentContainerStyle={styles.content}>
          {page === 'Tests' ? (
            <>
              <Text style={styles.description}>
                Session lifecycle observes initialization, then explicit close. Starting a new session
                replaces the previous UI evidence; browsing does not run BLE commands.
              </Text>
              <Text style={styles.description}>
                Scan, connection, GATT discovery, characteristic read/write and notification
                subscription are explicit manual typed flows in Current run. The RF full-suite is
                not migrated. These UI observations do not replace the legacy RF suite or host
                verdict.
              </Text>
            </>
          ) : null}
          {page === 'Current run' ? (
            <>
              <Text style={styles.description}>
                Initialization is not a guarantee that Bluetooth is powered on or access is granted.
                Switching views leaves this run and pending operations intact.
              </Text>
              <ScanConfiguration scan={scan} />
              <GattPanel scan={scan} />
              <NotifyPanel scan={scan} />
              <ManualScanDetails scan={scan} />
              <ScanSummary scan={scan} />
            </>
          ) : null}
          {page === 'Probe' ? (
            <>
              <Text style={styles.description}>
                Capabilities that are not part of the observation main line. They are pinned on
                their own page so the Current run console keeps its fixed height, and because a
                radio change or a pairing prompt is a different risk from an observation step.
              </Text>
              <Text style={styles.description}>
                Each probe states the platform's own answer: doing it, or refusing it as a
                capability this OS does not have — never a timeout in place of either.
              </Text>
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
                  <Text testID={Ids.workbench.evidenceRun}>Run {evidence.run}</Text>
                  {evidence.omitted > 0 ? (
                    <Text testID={Ids.workbench.evidenceOmitted}>{evidence.omitted} earlier observations omitted from this bounded summary.</Text>
                  ) : null}
                  <Text testID={Ids.workbench.lifecycleEvidence}>{evidence.entries.map(entry =>
                    `${entry.sequence}. ${entry.action}: ${entry.result}${entry.error === undefined ? '' : ` — ${entry.error}`}`
                  ).join('\n')}</Text>
                  <ScanSummary scan={scan} />
                </>
              ) : <Text testID={Ids.workbench.lifecycleEvidence}>No run has been requested.</Text>}
            </>
          ) : null}
        </ScrollView>
      </View>
    </SafeAreaView>
  )
}

function SnapshotCard({ snapshot, prefix = '', compact = false }: {
  snapshot: BlengAdapterSnapshot
  prefix?: string
  compact?: boolean
}): React.JSX.Element {
  return (
    <View style={compact ? styles.snapshotCardCompact : styles.statusCard}>
      {compact ? (
        <View style={styles.snapshotRowCompact}>
          <Text style={styles.snapshotCaptionCompact}>INIT — NOT LIVE</Text>
          <Text testID={Ids.snapshot.adapterState(prefix)} style={styles.snapshotFact}>{snapshot.state}</Text>
          <Text testID={Ids.snapshot.scanAuthorization(prefix)} style={styles.snapshotFact}>{snapshot.scanAuthorization}</Text>
          <Text testID={Ids.snapshot.connectionAuthorization(prefix)} style={styles.snapshotFact}>{snapshot.connectionAuthorization}</Text>
          <Text testID={Ids.snapshot.adapterName(prefix)} style={styles.snapshotFact}>{snapshot.name ?? 'not reported'}</Text>
        </View>
      ) : (
        <>
          <Text style={styles.statusCaption}>INITIALIZATION SNAPSHOT — NOT LIVE</Text>
          <View style={styles.snapshotRow}>
            <Text style={styles.snapshotLabel}>Adapter state</Text>
            <Text testID={Ids.snapshot.adapterState(prefix)} style={styles.snapshotValue}>{snapshot.state}</Text>
          </View>
          <View style={styles.snapshotRow}>
            <Text style={styles.snapshotLabel}>Scan authorization</Text>
            <Text testID={Ids.snapshot.scanAuthorization(prefix)} style={styles.snapshotValue}>{snapshot.scanAuthorization}</Text>
          </View>
          <View style={styles.snapshotRow}>
            <Text style={styles.snapshotLabel}>Connection authorization</Text>
            <Text testID={Ids.snapshot.connectionAuthorization(prefix)} style={styles.snapshotValue}>{snapshot.connectionAuthorization}</Text>
          </View>
          <View style={styles.snapshotRow}>
            <Text style={styles.snapshotLabel}>Adapter name</Text>
            <Text testID={Ids.snapshot.adapterName(prefix)} style={styles.snapshotValue}>{snapshot.name ?? 'not reported'}</Text>
          </View>
        </>
      )}
    </View>
  )
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: '#f7f8fa' },
  container: { flex: 1, padding: 12, gap: 6 },
  content: { gap: 12, paddingBottom: 24 },
  navigation: { flexDirection: 'row', justifyContent: 'space-between' },
  sessionControls: { flexDirection: 'row', gap: 8 },
  action: { flexGrow: 1 },
  eyebrow: { color: '#5865f2', fontSize: 10, fontWeight: '700', letterSpacing: 1 },
  statusLine: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  pageTitle: { color: '#15171a', fontSize: 14, fontWeight: '700' },
  state: { color: '#15171a', fontSize: 12, fontWeight: '600' },
  description: { color: '#50545a', fontSize: 14, lineHeight: 20 },
  statusCard: { backgroundColor: '#ffffff', borderRadius: 12, padding: 12, gap: 4 },
  snapshotCardCompact: { backgroundColor: '#ffffff', borderRadius: 8, paddingHorizontal: 8, paddingVertical: 4, gap: 0 },
  snapshotRowCompact: { flexDirection: 'row', flexWrap: 'wrap', columnGap: 10 },
  snapshotCaptionCompact: { color: '#777d85', fontSize: 10, fontWeight: '700' },
  snapshotFact: { color: '#15171a', fontSize: 12, lineHeight: 16 },
  statusCaption: { color: '#777d85', fontSize: 12, fontWeight: '700' },
  snapshotRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', minHeight: 32, gap: 8 },
  snapshotLabel: { color: '#50545a', flexShrink: 1 },
  snapshotValue: { color: '#15171a', flexShrink: 1, textAlign: 'right' },
  status: { color: '#15171a', fontSize: 22, fontWeight: '600' },
  // Machine-read: small on purpose. It is a projection, not a headline.
  projection: { color: '#383d45', fontSize: 10, lineHeight: 13 },
  // Machine surface: small, monospaced-feeling, one line per recorded action.
  ledger: { color: '#383d45', fontSize: 9, lineHeight: 11 },
  error: { color: '#b42318', fontSize: 14 },
  actions: { gap: 8 },
})
