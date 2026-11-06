import React from 'react'
import { Button, Text, TextInput, View } from 'react-native'
import type { useManualScan } from './useManualScan'

type Controller = ReturnType<typeof useManualScan>
export function ScanSummary({ scan }: { scan: Controller }): React.JSX.Element {
  const { view } = scan
  return (
    <View>
      <Text>MANUAL SCAN — UI observations, not unique devices or an RF verdict</Text>
      <Text testID="scan-state">{view.phase}</Text>
      {view.accepted ? <Text testID="scan-accepted">Submission accepted; only the original start wait observes startup.</Text> : null}
      <Text testID="scan-resource">{view.retained
        ? 'Scan/control retained; native stop/resource-zero is not proven by an observation failure.'
        : 'No scan retained for active controls; not proof of owner resource-zero.'}</Text>
      {view.error ? <Text testID="scan-error">{view.error}</Text> : null}
      {view.ended ? <Text testID="scan-ended">ended: {view.ended}</Text> : null}
      <Text testID="scan-count">{view.total} advertisements observed; {Math.max(0, view.total - view.samples.length)} samples omitted (latest 8 only).</Text>
      <Text testID="scan-samples">{view.samples.join('\n\n')}</Text>
    </View>
  )
}
export function ManualScan({ scan }: { scan: Controller }): React.JSX.Element {
  return (
    <View style={{ gap: 8 }}>
      <ScanSummary scan={scan} />
      <Text>Unfiltered scan, duplicates disabled. No commands run until explicitly pressed.</Text>
      <Text>Observation timeout (ms); timeout does not stop scanning. Rust validates the actual timeout limit; an enabled button is not native acceptance.</Text>
      <TextInput testID="scan-observation-timeout" accessibilityLabel="Scan observation timeout in milliseconds"
        keyboardType="number-pad" value={scan.timeoutText} onChangeText={scan.setTimeoutText} />
      {!scan.validTimeout ? <Text>Enter a safe positive integer in ms.</Text> : null}
      <Button testID="start-scan" title="Start scan" onPress={scan.start} disabled={!scan.canStart} />
      <Button testID="next-scan-event" title="Pull next" onPress={scan.pull} disabled={!scan.canPull} />
      <Button testID="stop-scan" title={scan.stopTitle} onPress={scan.stop} disabled={!scan.canStop} />
      <Button testID="cancel-scan-start" title="Cancel start" onPress={() => scan.cancel('start')} disabled={!scan.canCancelStart} />
      <Button testID="cancel-scan-stop" title="Cancel stop" onPress={() => scan.cancel('stop')} disabled={!scan.canCancelStop} />
      <Text>Cancel is a receipt, not rollback. A consumed stop wait is never repeated; Submit another stop explicitly sends a new command. Rejected submission keeps the previous cancel control. Rust input/invalid_timeout leaves the original wait available after correcting the timeout. No automatic retry or stop. Close remains independent.</Text>
    </View>
  )
}
