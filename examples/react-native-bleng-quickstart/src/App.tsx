/**
 * A minimal, complete BLE Central over `@blengine/react-native-bleng`.
 *
 * What it shows, in the order a real screen needs it:
 *
 * 1. create the central and read the adapter snapshot (no scan yet);
 * 2. scan with one bounded pull loop and an explicit stop;
 * 3. connect to one advertisement, then observe the link's own end;
 * 4. discover GATT, read the first readable characteristic;
 * 5. subscribe with the mode the peer advertises, pull values, disable before disconnecting;
 * 6. disconnect and close.
 *
 * The component itself only renders and wires; the commands live in `screen.ts`. That split is
 * what keeps this component React Compiler clean, so the compiler's memoization actually applies
 * here instead of bailing out on imperative code.
 */
import React, { useEffect, useState } from 'react'
import { Button, ScrollView, StyleSheet, Text, View } from 'react-native'

import { createScreenController, initial } from './screen'
import type { Screen } from './screen'

export default function App(): React.JSX.Element {
  const [screen, setScreen] = useState<Screen>(initial)
  // `setScreen` is referentially stable, so this initializer runs once per mounted screen: the
  // controller owns every capability, and the component owns only what the engine reported.
  const [controller] = useState(() => createScreenController(setScreen))

  useEffect(() => {
    void controller.initialize()
    // Leaving the screen must not leave observation running.
    return () => void controller.close()
  }, [controller])

  return (
    <ScrollView contentContainerStyle={styles.page}>
      <Text style={styles.title}>BLeng quickstart</Text>
      <Text style={styles.line}>adapter: {screen.adapter}</Text>
      {screen.error ? <Text style={styles.error}>{screen.error}</Text> : null}

      <View style={styles.row}>
        <Button
          title={screen.scanning ? 'Stop scan' : 'Scan'}
          onPress={() => void (screen.scanning ? controller.stopScan() : controller.startScan())}
        />
        <Button
          title="Disconnect"
          onPress={() => void controller.disconnect()}
          disabled={screen.link !== 'connected'}
        />
      </View>

      <Text style={styles.line}>link: {screen.link}</Text>
      {screen.devices.map((advertisement, sighting) => (
        <Button
          key={`sighting-${sighting}`}
          title={`Connect ${advertisement.localName ?? '(unnamed)'} `
            + `rssi=${advertisement.rssi ?? 'not reported'}`}
          onPress={() => void controller.connect(advertisement)}
          disabled={screen.link === 'connecting' || screen.link === 'connected'}
        />
      ))}

      {screen.link === 'connected' ? (
        <View style={styles.row}>
          <Button title="Discover + read" onPress={() => void controller.discoverAndRead()} />
          <Button title="Subscribe" onPress={() => void controller.subscribe()} />
        </View>
      ) : null}

      {screen.value ? <Text style={styles.mono}>{screen.value}</Text> : null}
      {screen.services.map(service => (
        <Text key={service} style={styles.mono}>{service}</Text>
      ))}
      {screen.values > 0 ? (
        <Text style={styles.line}>values received: {screen.values}</Text>
      ) : null}
    </ScrollView>
  )
}

const styles = StyleSheet.create({
  page: { padding: 16, gap: 8 },
  title: { fontSize: 20, fontWeight: '600' },
  row: { flexDirection: 'row', gap: 12, marginVertical: 8 },
  line: { fontSize: 14 },
  mono: { fontFamily: 'Menlo', fontSize: 12 },
  error: { fontSize: 14, color: '#b00020' },
})
