import React from 'react'
import { Ids } from './ids'
import { Button, ScrollView, StyleSheet, Text, View } from 'react-native'
import type { useManualScan } from './useManualScan'
import { gattCharacteristicText } from './gattProjection'

type Controller = ReturnType<typeof useManualScan>

/**
 * Everything below is *content*: narration, the topology index, sample lists and history.
 * A human reads it; a UI scene never needs to reach it, because every command and every
 * polled value lives in the pinned console and the pinned status projection.
 *
 * This split is the design, not a preference. Android omits off-viewport `ScrollView`
 * children from the accessibility tree, so anything a scene must touch would have to be
 * scrolled to — and a gesture is only as accurate as the automation server's injection.
 * Content is allowed to be arbitrarily long precisely because nothing machine-relevant
 * lives in it.
 */

export function ScanConfiguration({ scan }: { scan: Controller }): React.JSX.Element {
  return (
    <View testID={Ids.scan.configuration} style={styles.group}>
      <Text testID={Ids.scan.filter}>{scan.filterSummary}</Text>
      {scan.filterError ? <Text testID={Ids.scan.filterError}>{scan.filterError}</Text> : null}
      <Text style={styles.note}>
        Manual scan — UI observations, not unique devices or an RF verdict. Bounded delivery:
        an unfiltered scan in a dense environment fails closed instead of dropping
        advertisements. The scan request is the filter above; the inputs are pinned with the
        commands.
      </Text>
    </View>
  )
}

/** The run's phases and bounded counts, as a human reads them. Evidence shows this too. */
export function ScanSummary({ scan }: { scan: Controller }): React.JSX.Element {
  const { view } = scan
  return (
    <View testID={Ids.scan.summary} style={styles.group}>
      <Text testID={Ids.scan.state}>{view.phase}</Text>
      <Text testID={Ids.link.state}>{view.link}</Text>
      <Text testID={Ids.gatt.state}>{view.gattPhase}</Text>
      <Text testID={Ids.notify.state}>{view.notifyPhase}</Text>
      <Text testID={Ids.scan.count}>{`${view.total} advertisements observed; ${Math.max(0, view.total - view.samples.length)} samples omitted (latest 8 only).`}</Text>
      {view.accepted ? <Text testID={Ids.scan.accepted}>Submission accepted; only the original start wait observes startup.</Text> : null}
      {view.error ? <Text testID={Ids.scan.error}>{view.error}</Text> : null}
      {view.ended ? <Text testID={Ids.scan.ended}>ended: {view.ended}</Text> : null}
      <Text testID={Ids.scan.resource}>{view.retained
        ? 'Scan/control retained; native stop/resource-zero is not proven by an observation failure.'
        : 'No scan retained for active controls; not proof of owner resource-zero.'}</Text>
      <ScrollView testID={Ids.scan.sampleViewport} nestedScrollEnabled style={styles.sampleViewport}>
        <Text testID={Ids.scan.samples}>{view.samples.join('\n\n')}</Text>
      </ScrollView>
    </View>
  )
}

/** The peer's discovered topology, as a human reads it: full rows, properties, descriptors. */
export function GattPanel({ scan }: { scan: Controller }): React.JSX.Element {
  const { view } = scan
  return (
    <View testID={Ids.gatt.panel} style={styles.group}>
      <Text testID={Ids.gatt.summary}>{view.gatt
        ? `${view.gatt.services.length} services, ${view.gatt.characteristics} characteristics; ${view.gatt.omitted} not addressable from this screen.`
        : 'No topology discovered for the current link.'}</Text>
      <Text testID={Ids.gatt.rows}>{view.gatt === undefined || view.gatt.addressable.length === 0 ? 'none'
        : view.gatt.addressable.map(entry => `${entry.key} ${gattCharacteristicText(entry)}`).join('\n')}</Text>
      <Text testID={Ids.gatt.targetStatus}>{resolveText(scan)}</Text>
      {/* The outcome of a read, write or RSSI request is pinned in the command console:
          it must be observable without scrolling, so it is never rendered here. */}
      <Text style={styles.note}>
        Rows are keyed by discovery position for reading only. The action strip targets the
        characteristic UUID you declare in the console, so acting never depends on a position.
      </Text>
    </View>
  )
}

function resolveText(scan: Controller): string {
  const declared = scan.gattTarget.trim()
  if (declared === '') return 'No characteristic declared'
  if (scan.gattTargetStatus === 'invalid') return `Not a full 128-bit UUID: ${declared.slice(0, 40)}`
  if (scan.gattTargetStatus === 'none') return `No discovered characteristic matches ${declared}`
  if (scan.gattTargetStatus === 'ambiguous') {
    return `${scan.gattTargetMatches} characteristics share ${declared}; refusing to guess`
  }
  return `${declared} [${scan.gattTargetProperties}]`
}

/** The subscription stream, as a human reads it; the commands and their outcome are pinned. */
export function NotifyPanel({ scan }: { scan: Controller }): React.JSX.Element {
  const { view } = scan
  return (
    <View testID={Ids.notify.panel} style={styles.group}>
      <Text testID={Ids.notify.mode}>{view.notifyMode === undefined
        ? 'Actual mode not reported'
        : `Actual mode: ${view.notifyMode}`}</Text>
      <Text testID={Ids.notify.count}>{`${view.notifyTotal} values received; ${Math.max(0, view.notifyTotal - view.notifySamples.length)} omitted (latest 8 only).`}</Text>
      <Text testID={Ids.notify.samples}>{view.notifySamples.join('\n') || 'none'}</Text>
      {/* The sentence a human reads. A scene asserts the pinned `failure` identity instead:
          an error line here cannot push a command out of the screen, and prose of unbounded
          length is not something a machine should have to match. */}
      {view.notifyError ? <Text testID={Ids.notify.error}>{view.notifyError}</Text> : null}
      <Text style={styles.note}>
        A subscription is a stream: a value backlog that overflows fails closed instead of
        dropping values. An observation timeout is not a stopped subscription.
      </Text>
    </View>
  )
}

/** The run's narration and its retire-cancel controls, which stay human-only. */
export function ManualScanDetails({ scan }: { scan: Controller }): React.JSX.Element {
  return (
    <View style={styles.group}>
      <Text testID={Ids.link.detail}>{scan.view.link === 'connected'
        ? 'Connection retained; native teardown is only proven by a successful disconnect receipt.'
        : 'Connect uses the newest observed capability; a capability outlives its scan session.'}</Text>
      <Button testID={Ids.scan.cancelStart} title="Cancel start" onPress={() => scan.cancel('start')} disabled={!scan.canCancelStart} />
      <Button testID={Ids.scan.cancelStop} title="Cancel stop" onPress={() => scan.cancel('stop')} disabled={!scan.canCancelStop} />
      <Text style={styles.note}>
        Observation timeout (ms); a timeout does not stop scanning. Rust validates the actual
        timeout limit; an enabled button is not native acceptance.
      </Text>
      <Text style={styles.note}>
        Cancel is a receipt, not rollback. A consumed stop wait is never repeated. Rejected
        submission keeps the previous cancel control. Rust input/invalid_timeout leaves the
        original wait available after correcting the timeout. No automatic retry or stop.
        Close remains independent.
      </Text>
    </View>
  )
}

const styles = StyleSheet.create({
  group: { gap: 4 },
  note: { color: '#50545a', fontSize: 12, lineHeight: 16 },
  sampleViewport: { maxHeight: 160, flexGrow: 0 },
})
