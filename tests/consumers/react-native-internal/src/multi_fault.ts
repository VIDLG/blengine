import type { RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro'
import { createBleHubProbe } from '@blehub/react-native-internal'
import {
  RF_CONNECTION_TIMEOUT_MS,
  RF_INITIALIZATION_TIMEOUT_MS,
  RF_POST_STOP_OBSERVATION_MS,
  isCanonicalU64,
  validateRfGattDatabase,
} from './rf_contract'
import {
  createMultiEventStream,
  sameBytes,
  takeCompletion,
  takeConnectionState,
  type MultiEventStream,
} from './multi_rf'

type Probe = ReturnType<typeof createBleHubProbe>
type Report = (line: string) => void

type Identity = {
  peripheralId: string
  connectionId: string
  gattSessionId: string
  characteristicId: string
}

async function event(
  stream: MultiEventStream,
  predicate: (candidate: RfScanEvent) => boolean,
  label: string,
  isCancelled: () => boolean,
  timeout = RF_CONNECTION_TIMEOUT_MS,
): Promise<RfScanEvent> {
  return stream.take(predicate, timeout, label, isCancelled)
}

async function connectAndDiscover(
  stream: MultiEventStream,
  probe: Probe,
  serviceUuid: string,
  peripheralId: string,
  commandBase: number,
  connectionId: string,
  gattSessionId: string,
  isCancelled: () => boolean,
): Promise<Identity> {
  const connectCommand = String(commandBase)
  if (probe.connectRfPeripheral(connectCommand, connectionId, peripheralId)) {
    throw new Error(`connect dispatch rejected:${serviceUuid}`)
  }
  await takeConnectionState(stream, connectCommand, connectionId, 'Connecting', isCancelled)
  await takeConnectionState(stream, connectCommand, connectionId, 'Connected', isCancelled)
  await takeCompletion(stream, connectCommand, isCancelled)

  const gattCommand = String(commandBase + 2)
  if (probe.discoverRfGatt(gattCommand, connectionId, gattSessionId)) {
    throw new Error(`GATT dispatch rejected:${serviceUuid}`)
  }
  const discovered = await event(
    stream,
    (candidate) => candidate.kind === 'gattDiscovered' &&
      candidate.commandId === gattCommand && candidate.connectionId === connectionId,
    `GATT discovery for ${serviceUuid}`,
    isCancelled,
  )
  const database = validateRfGattDatabase(discovered, serviceUuid, connectionId)
  if (database.gattSessionId !== gattSessionId) throw new Error(`GATT session mismatch:${serviceUuid}`)
  await takeCompletion(stream, gattCommand, isCancelled)
  return {
    peripheralId,
    connectionId,
    gattSessionId,
    characteristicId: database.readWriteCharacteristicId,
  }
}

async function readValue(
  stream: MultiEventStream,
  probe: Probe,
  identity: Identity,
  commandId: string,
  expected: number,
  isCancelled: () => boolean,
): Promise<void> {
  if (probe.readRfCharacteristic(
    commandId,
    identity.connectionId,
    identity.gattSessionId,
    identity.characteristicId,
  )) throw new Error(`read dispatch rejected:${identity.connectionId}`)
  const read = await event(
    stream,
    (candidate) => candidate.kind === 'characteristicRead' &&
      candidate.commandId === commandId && candidate.connectionId === identity.connectionId &&
      candidate.gattSessionId === identity.gattSessionId &&
      candidate.characteristicId === identity.characteristicId,
    `read for ${identity.connectionId}`,
    isCancelled,
  )
  if (read.error || !sameBytes(read.value, new Uint8Array([expected]))) {
    throw new Error(`readback mismatch:${identity.connectionId}:expected=${expected.toString(16)}`)
  }
  await takeCompletion(stream, commandId, isCancelled)
}

async function scanOne(
  stream: MultiEventStream,
  probe: Probe,
  serviceUuid: string,
  commandBase: number,
  isCancelled: () => boolean,
): Promise<string> {
  const startCommand = String(commandBase)
  const scanSession = String(commandBase + 1)
  if (probe.startRfScan(startCommand, scanSession, serviceUuid)) throw new Error('fault scan dispatch rejected')
  await event(
    stream,
    (candidate) => candidate.kind === 'scanStarted' && candidate.commandId === startCommand && candidate.scanSessionId === scanSession,
    'fault scan started',
    isCancelled,
  )
  await takeCompletion(stream, startCommand, isCancelled)
  const advertisement = await event(
    stream,
    (candidate) => candidate.kind === 'advertisementObserved' && candidate.scanSessionId === scanSession && candidate.advertisedServices?.includes(serviceUuid) === true,
    `fault advertisement for ${serviceUuid}`,
    isCancelled,
  )
  if (!isCanonicalU64(advertisement.peripheralId)) throw new Error(`invalid fault peripheral ID:${serviceUuid}`)
  const stopCommand = String(commandBase + 2)
  if (probe.stopRfScan(stopCommand, scanSession)) throw new Error('fault scan stop dispatch rejected')
  await event(
    stream,
    (candidate) => candidate.kind === 'scanStopped' && candidate.commandId === stopCommand && candidate.scanSessionId === scanSession && !candidate.error,
    'fault scan stopped',
    isCancelled,
  )
  await takeCompletion(stream, stopCommand, isCancelled)
  await new Promise((resolve) => setTimeout(resolve, RF_POST_STOP_OBSERVATION_MS))
  return advertisement.peripheralId
}

export async function runRfMultiFault(
  probe: Probe,
  rfServiceUuid: string | undefined,
  report: Report,
  isCancelled: () => boolean,
): Promise<void> {
  const serviceUuids = (rfServiceUuid ?? '').split(',').map((value) => value.trim()).filter(Boolean)
  let stream: MultiEventStream | undefined
  let failed = false
  try {
    if (serviceUuids.length !== 2 || new Set(serviceUuids).size !== 2) {
      throw new Error('rf-multi-fault requires exactly two distinct service UUIDs')
    }
    stream = createMultiEventStream(probe)
    report('rf-multi-fault:listener:registered-before-initialize')
    if (probe.initialize('9400')) throw new Error('initialize dispatch rejected')
    await event(stream, (candidate) => candidate.kind === 'adapterSnapshotChanged' && candidate.adapterState === 'PoweredOn' && candidate.scanAuthorization === 'Authorized', 'fault adapter ready', isCancelled, RF_INITIALIZATION_TIMEOUT_MS)
    await takeCompletion(stream, '9400', isCancelled)

    if (probe.startRfScan('9401', '9402', serviceUuids.join(','))) throw new Error('fault scan dispatch rejected')
    await event(stream, (candidate) => candidate.kind === 'scanStarted' && candidate.commandId === '9401' && candidate.scanSessionId === '9402', 'fault scan started', isCancelled)
    await takeCompletion(stream, '9401', isCancelled)
    const peripherals = new Map<string, string>()
    for (const serviceUuid of serviceUuids) {
      const advertisement = await event(stream, (candidate) => candidate.kind === 'advertisementObserved' && candidate.scanSessionId === '9402' && candidate.advertisedServices?.includes(serviceUuid) === true, `fault advertisement for ${serviceUuid}`, isCancelled)
      if (!isCanonicalU64(advertisement.peripheralId)) throw new Error(`invalid peripheral ID:${serviceUuid}`)
      peripherals.set(serviceUuid, advertisement.peripheralId)
    }
    if (probe.stopRfScan('9403', '9402')) throw new Error('fault scan stop dispatch rejected')
    await event(stream, (candidate) => candidate.kind === 'scanStopped' && candidate.commandId === '9403' && candidate.scanSessionId === '9402' && !candidate.error, 'fault scan stopped', isCancelled)
    await takeCompletion(stream, '9403', isCancelled)
    await new Promise((resolve) => setTimeout(resolve, RF_POST_STOP_OBSERVATION_MS))

    const a = await connectAndDiscover(stream, probe, serviceUuids[0], peripherals.get(serviceUuids[0])!, 9410, '9420', '9430', isCancelled)
    const b = await connectAndDiscover(stream, probe, serviceUuids[1], peripherals.get(serviceUuids[1])!, 9440, '9450', '9460', isCancelled)
    report(`rf-multi-fault:ready-for-a-fault:connectionA=${a.connectionId}:connectionB=${b.connectionId}:gattA=${a.gattSessionId}:gattB=${b.gattSessionId}`)

    const disconnectedA = await event(
      stream,
      (candidate) => candidate.kind === 'connectionStateChanged' && candidate.commandId === undefined && candidate.connectionId === a.connectionId && ((candidate.connectionState === 'Disconnected' && candidate.error === undefined) || (candidate.connectionState === undefined && candidate.error?.category === 'connection')),
      'passive disconnect of A',
      isCancelled,
      RF_CONNECTION_TIMEOUT_MS,
    )
    if (!disconnectedA) throw new Error('A passive disconnect event missing')
    report(`rf-multi-fault:a-passive-disconnected:connectionId=${a.connectionId}:peripheralId=${a.peripheralId}:error=${disconnectedA.error?.category ?? 'none'}`)

    await readValue(stream, probe, b, '9470', 0x00, isCancelled)
    report(`rf-multi-fault:b-survived-a-fault:connectionId=${b.connectionId}:gattSessionId=${b.gattSessionId}:value=00`)
    report('rf-multi-fault:awaiting-a-restart')

    const restartedPeripheral = await scanOne(stream, probe, serviceUuids[0], 9480, isCancelled)
    const reconnected = await connectAndDiscover(stream, probe, serviceUuids[0], restartedPeripheral, 9490, '9500', '9510', isCancelled)
    await readValue(stream, probe, reconnected, '9520', 0x00, isCancelled)
    report(`rf-multi-fault:a-reconnected:connectionId=${reconnected.connectionId}:gattSessionId=${reconnected.gattSessionId}:value=00`)

    if (probe.disconnectRfPeripheral('9530', b.connectionId)) throw new Error('B disconnect dispatch rejected')
    await takeConnectionState(stream, '9530', b.connectionId, 'Disconnected', isCancelled)
    await takeCompletion(stream, '9530', isCancelled)
    if (probe.disconnectRfPeripheral('9540', reconnected.connectionId)) throw new Error('A reconnect disconnect dispatch rejected')
    await takeConnectionState(stream, '9540', reconnected.connectionId, 'Disconnected', isCancelled)
    await takeCompletion(stream, '9540', isCancelled)
    stream.remove()
    await probe.close()
    report('rf-multi-fault:close:passed:listener-removed=true:probe-closed=true')
    report(`rf-multi-fault:passed:passiveFault=dk_a:bSurvived=true:aReconnected=true:connectionCount=3:gattSessionCount=3`)
  } catch (error: unknown) {
    failed = true
    stream?.remove()
    report(`rf-multi-fault:failed:${error instanceof Error ? error.message : String(error)}`)
    throw error
  } finally {
    if (failed) await probe.close().catch(() => undefined)
  }
}
