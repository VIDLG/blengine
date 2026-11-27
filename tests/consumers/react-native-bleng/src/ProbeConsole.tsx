import React from 'react'
import { Ids } from './ids'
import { StyleSheet, Text, View } from 'react-native'
import { CommandButton } from './CommandConsole'
import type { useManualScan } from './useManualScan'

type Controller = ReturnType<typeof useManualScan>

/**
 * Capabilities that are not part of the observation main line.
 *
 * They live on their own page for a structural reason: `CommandConsole` is a fixed-height
 * machine surface, and every family added to it costs a wrapped row that can push a later
 * control out of the viewport — where Android renders nothing at all. Probe families also
 * carry a different risk: a radio change or a pairing prompt is not an observation step, so
 * it should not share the surface that seventeen observation scenes already depend on.
 */
export function ProbeConsole({ scan }: { scan: Controller }): React.JSX.Element {
  return (
    <View testID={Ids.probe.console} accessible={false} style={styles.console}>
      <View testID={Ids.probe.autoStrip} style={styles.row}>
        <CommandButton identifier={Ids.controls.autoConnect} title="Auto-connect"
          onPress={() => { void scan.autoConnect() }} disabled={!scan.canAutoConnect} />
      </View>
      <View testID={Ids.probe.inventoryStrip} style={styles.row}>
        <CommandButton identifier={Ids.controls.devicesBonded} title="Bonded inventory"
          onPress={() => { void scan.queryDevices('bonded') }} disabled={!scan.canQueryInventory} />
        <CommandButton identifier={Ids.controls.devicesCompanion} title="Companion inventory"
          onPress={() => { void scan.queryDevices('companion') }} disabled={!scan.canQueryInventory} />
      </View>
      {/* Radio power. Both platforms refuse this on the devices we test (Android: any app
          targeting API 33+ that is not DO/PO/system; Apple: CoreBluetooth exposes no radio
          control), so the scene proves the App reports the platform's own refusal as itself
          instead of pretending the radio changed. */}
      <View testID={Ids.probe.radioStrip} style={styles.row}>
        <CommandButton identifier={Ids.controls.radioOff} title="Radio off"
          onPress={() => { void scan.setRadio(false) }} disabled={!scan.canSetRadio} />
        <CommandButton identifier={Ids.controls.radioOn} title="Radio on"
          onPress={() => { void scan.setRadio(true) }} disabled={!scan.canSetRadio} />
      </View>
      <Text style={styles.note}>
        Auto-connect is a standing request: the scene asks while the peer is absent, then proves
        the link appears only after the peer returns. Unsupported is a platform answer.
      </Text>
    </View>
  )
}

const styles = StyleSheet.create({
  console: { gap: 3 },
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: 4 },
  note: { color: '#50545a', fontSize: 10, lineHeight: 13 },
})
