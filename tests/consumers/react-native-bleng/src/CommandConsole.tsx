import React from 'react'
import { Ids } from './ids'
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native'
import type { useManualScan } from './useManualScan'

type Controller = ReturnType<typeof useManualScan>

/**
 * A compact command button.
 *
 * React Native's `Button` has a fixed, tall platform height. Every row of the pinned
 * console costs the scrolled content a row, and the console is read by a machine, so the
 * control surface is deliberately small: its job is to be reachable and pressable, not
 * comfortable. `accessibilityState` is set explicitly because a disabled command must be
 * observable as disabled by the platform, not merely styled that way.
 */
export function CommandButton({ identifier, title, onPress, disabled }: {
  identifier: string
  title: string
  onPress: () => void
  disabled?: boolean
}): React.JSX.Element {
  return (
    <Pressable
      testID={identifier}
      accessibilityRole="button"
      accessibilityLabel={title}
      accessibilityState={{ disabled: disabled === true }}
      disabled={disabled}
      onPress={onPress}
      style={[styles.command, disabled === true ? styles.commandDisabled : null]}
    >
      <Text numberOfLines={1} style={styles.commandText}>{title}</Text>
    </Pressable>
  )
}

/** One labelled input in the console; the label is the machine-visible name. */
function CommandInput({ identifier, label, value, onChangeText, keyboardType }: {
  identifier: string
  label: string
  value: string
  onChangeText: (value: string) => void
  keyboardType?: 'number-pad'
}): React.JSX.Element {
  return (
    <View style={styles.inputRow}>
      <Text style={styles.inputLabel}>{label}</Text>
      <TextInput
        testID={identifier}
        accessibilityLabel={label}
        keyboardType={keyboardType}
        autoCapitalize="none"
        autoCorrect={false}
        value={value}
        onChangeText={onChangeText}
        style={styles.input}
      />
    </View>
  )
}

/**
 * The run's entire command surface, pinned.
 *
 * A UI scene must never scroll to reach an action. Scroll position is a rendering detail:
 * a drag is only as accurate as the automation server's gesture injection, and every
 * panel a scene has to walk past is another chance to land short. So the commands live in
 * one bounded block that is never inside a scroll container, and the narration, topology
 * index and sample lists — which only a human reads — are the scrollable content.
 *
 * The layout is therefore load-bearing, not cosmetic: `assertNoneOf` in the host suite
 * requires this scene to reach its commands with zero gestures.
 */
export function CommandConsole({ scan, writeText, setWriteText }: {
  scan: Controller
  writeText: string
  setWriteText: (value: string) => void
}): React.JSX.Element {
  return (
    <View testID={Ids.command.console} accessible={false} style={styles.console}>
      <View testID={Ids.scan.primaryControls} style={styles.row}>
        <CommandButton identifier={Ids.controls.startScan} title="Start scan" onPress={scan.start}
          disabled={!scan.canStart} />
        <CommandButton identifier={Ids.controls.nextScanEvent} title="Pull next" onPress={scan.pull}
          disabled={!scan.canPull} />
        <CommandButton identifier={Ids.controls.stopScan} title={scan.stopTitle} onPress={scan.stop}
          disabled={!scan.canStop} />
        {/* The inventory query is an owner command, not a scan command; it rides here because
            this row has width to spare and the console is a fixed height budget — a new row
            would push the controls below it out of the viewport, where Android renders nothing. */}
        <CommandButton identifier={Ids.controls.devicesQuery} title="Devices"
          onPress={() => { void scan.queryDevices() }} disabled={!scan.canQueryDevices} />
      </View>
      <View testID={Ids.link.controls} style={styles.row}>
        <CommandButton identifier={Ids.controls.connectPeripheral} title="Connect" onPress={scan.connect}
          disabled={!scan.canConnect} />
        <CommandButton identifier={Ids.controls.disconnectPeripheral} title="Disconnect"
          onPress={scan.disconnect} disabled={!scan.canDisconnect} />
        <CommandButton identifier={Ids.controls.discoverGatt} title="Discover" onPress={scan.discover}
          disabled={!scan.canDiscoverGatt} />
        <CommandButton identifier={Ids.controls.readRssi} title="RSSI" onPress={scan.readRssi}
          disabled={!scan.canReadRssi} />
        {/* Link facts ride in the same wrapping row instead of a new one: the console is a
            pinned machine surface, and every row it grows can push a later control out of the
            viewport, where Android renders nothing at all. Their outcomes are the
            `mtu`/`mtuRequest`/`priority`/`refresh` scalars in `run-status`, not new text. */}
        <CommandButton identifier={Ids.controls.mtuQuery} title="MTU"
          onPress={() => { void scan.queryMtu() }} disabled={!scan.canLinkFact} />
        <CommandButton identifier={Ids.controls.mtuRequest} title="MTU req"
          onPress={() => { void scan.requestMtu() }} disabled={!scan.canLinkFact} />
        <CommandButton identifier={Ids.controls.priorityHigh} title="Priority"
          onPress={() => { void scan.requestHighPriority() }} disabled={!scan.canLinkFact} />
        <CommandButton identifier={Ids.controls.refreshGatt} title="Refresh"
          onPress={() => { void scan.refreshGattCache() }} disabled={!scan.canLinkFact} />
      </View>
      <CommandInput identifier={Ids.controls.gattTarget} label="characteristic UUID"
        value={scan.gattTarget} onChangeText={scan.setGattTarget} />
      <View testID={Ids.gatt.actionStrip} style={styles.row}>
        <CommandButton identifier={Ids.controls.gattRead} title="Read"
          onPress={() => scan.read(scan.gattTarget)}
          disabled={!scan.canReadGatt} />
        <CommandButton identifier={Ids.controls.gattWrite} title="Write"
          onPress={() => scan.write(scan.gattTarget, writeText)}
          disabled={!scan.canWriteGatt} />
        <CommandButton identifier={Ids.controls.gattSubscribe} title="Subscribe"
          onPress={() => { void scan.enableNotifications(scan.gattTarget) }}
          disabled={!scan.canEnableNotifications} />
        <CommandButton identifier={Ids.controls.cancelGattRead} title="Cancel read"
          onPress={() => { void scan.cancelRead() }} disabled={!scan.canCancelGattRead} />
      </View>
      {/* What the commands above reported, pinned for the same reason they are: a scene has
          to read the outcome it just caused, and content is not readable without a
          gesture. A value that exists but is scrolled out of view is not an observation a
          UI scene can make, so the outcome lives here and not in the GATT panel. */}
      <View testID={Ids.command.outcome} style={styles.outcome}>
        <Text testID={Ids.rssi.value} style={styles.outcomeText}>
          {scan.view.rssi === undefined ? 'RSSI not read' : `${scan.view.rssi} dBm`}
        </Text>
        {scan.view.rssiError ? (
          <Text testID={Ids.rssi.error} style={styles.outcomeError}>{scan.view.rssiError}</Text>
        ) : null}
        {scan.view.gattError ? (
          <Text testID={Ids.gatt.error} style={styles.outcomeError}>{scan.view.gattError}</Text>
        ) : null}
        {scan.view.gattResult ? (
          <Text testID={Ids.gatt.result} style={styles.outcomeText}>{scan.view.gattResult}</Text>
        ) : null}
        {scan.view.gattCancel ? (
          <Text testID={Ids.gatt.cancel} style={styles.outcomeText}>{scan.view.gattCancel}</Text>
        ) : null}
      </View>
      <CommandInput identifier={Ids.controls.gattWriteValue} label="write payload"
        value={writeText} onChangeText={setWriteText} />
      <View testID={Ids.notify.actionStrip} style={styles.row}>
        <CommandButton identifier={Ids.controls.pullNotification} title="Pull value"
          onPress={scan.pullNotification} disabled={!scan.canPullNotification} />
        <CommandButton identifier={Ids.controls.disableNotifications} title="Disable"
          onPress={scan.disableNotifications} disabled={!scan.canDisableNotifications} />
        {/* The CCCD rides in this row: it is the peer's own answer about this subscription, and
            the row has width left. A new row would cost the *last* control its visibility, not
            this one — the console is a fixed budget. */}
        <CommandButton identifier={Ids.controls.descriptorRead} title="Read CCCD"
          onPress={() => { void scan.readDescriptor() }} disabled={!scan.canReadDescriptor} />
        <CommandButton identifier={Ids.controls.descriptorWrite} title="Write CCCD"
          onPress={() => { void scan.writeDescriptor() }} disabled={!scan.canWriteDescriptor} />
      </View>
      <CommandInput identifier={Ids.controls.scanObservationTimeout} label="timeout ms"
        keyboardType="number-pad" value={scan.timeoutText}
        onChangeText={scan.setTimeoutText} />
      <CommandInput identifier={Ids.controls.scanServiceFilter} label="service filter"
        value={scan.filterText} onChangeText={scan.setFilterText} />
    </View>
  )
}

const styles = StyleSheet.create({
  console: { gap: 3 },
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: 4 },
  // The outcome is machine surface too: small, one line per fact, never a scrolling list.
  outcome: { gap: 2 },
  outcomeText: { color: '#15171a', fontSize: 11, lineHeight: 14 },
  outcomeError: { color: '#a3231f', fontSize: 11, lineHeight: 14 },
  command: {
    flexGrow: 1, flexShrink: 1, flexBasis: 72, minHeight: 28, borderRadius: 6,
    paddingHorizontal: 4, paddingVertical: 3, backgroundColor: '#d9e2f7',
    alignItems: 'center', justifyContent: 'center',
  },
  commandDisabled: { backgroundColor: '#eceef2' },
  commandText: { color: '#15171a', fontSize: 10, lineHeight: 13 },
  inputRow: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  inputLabel: { color: '#50545a', fontSize: 10, lineHeight: 13, flexBasis: 96 },
  input: {
    flexGrow: 1, minHeight: 30, paddingHorizontal: 6, paddingVertical: 2,
    fontSize: 11, color: '#15171a', backgroundColor: '#ffffff', borderRadius: 6,
  },
})
