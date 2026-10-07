import type { RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';
import { createBleHubProbe } from '@blehub/react-native-internal';
import {
  RF_CONNECTION_TIMEOUT_MS,
  RF_INITIALIZATION_TIMEOUT_MS,
  RF_POST_NOTIFICATION_DISABLE_OBSERVATION_MS,
  RF_POST_STOP_OBSERVATION_MS,
  isCanonicalU64,
  validateRfGattDatabase,
} from './rf_contract';

type Probe = ReturnType<typeof createBleHubProbe>;

type Report = (line: string) => void;

export interface MultiRfOptions {
  initialize?: boolean;
  closeProbe?: boolean;
  commandOffset?: number;
}

export interface MultiEventStream {
  events: RfScanEvent[];
  take(predicate: (event: RfScanEvent) => boolean, timeout: number, label: string, isCancelled: () => boolean): Promise<RfScanEvent>;
  remove(): void;
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export function sameBytes(left: ArrayBuffer | undefined, right: Uint8Array): boolean {
  const actual = new Uint8Array(left ?? new ArrayBuffer(0));
  return actual.length === right.length && actual.every((value, index) => value === right[index]);
}

export function createMultiEventStream(probe: Probe): MultiEventStream {
  const events: RfScanEvent[] = [];
  const consumed = new Set<number>();
  let wake: (() => void) | undefined;
  let removed = false;
  let terminalError: Error | undefined;
  const listener = (event: RfScanEvent): void => {
    if (removed || terminalError) return;
    if (event.kind === 'backendError' || (event.kind === 'scanStopped' && event.error)) {
      terminalError = new Error(`multi RF event error:${event.kind}:${event.error?.message ?? 'backend error'}`);
      wake?.();
      wake = undefined;
      return;
    }
    events.push(event);
    wake?.();
    wake = undefined;
  };
  probe.setRfScanListener(listener);
  return {
    events,
    async take(predicate, timeout, label, isCancelled): Promise<RfScanEvent> {
      const deadline = Date.now() + timeout;
      while (true) {
        if (terminalError) throw terminalError;
        for (let index = 0; index < events.length; index += 1) {
          if (consumed.has(index) || !predicate(events[index])) continue;
          consumed.add(index);
          return events[index];
        }
        if (isCancelled()) throw new Error(`${label} cancelled`);
        const remaining = deadline - Date.now();
        if (remaining <= 0) throw new Error(`${label} timed out after ${timeout}ms`);
        await Promise.race([
          delay(Math.min(remaining, 100)),
          new Promise<void>((resolve) => { wake = resolve; }),
        ]);
      }
    },
    remove(): void {
      if (removed) return;
      removed = true;
      probe.removeRfScanListener();
    },
  };
}

export async function takeCompletion(
  stream: MultiEventStream,
  commandId: string,
  isCancelled: () => boolean,
): Promise<void> {
  const event = await stream.take(
    (candidate) => candidate.kind === 'commandCompleted' && candidate.commandId === commandId,
    RF_CONNECTION_TIMEOUT_MS,
    `command ${commandId} completion`,
    isCancelled,
  );
  if (event.error) throw new Error(`command ${commandId} failed:${event.error.category}:${event.error.message}`);
}

export async function takeConnectionState(
  stream: MultiEventStream,
  commandId: string,
  connectionId: string,
  state: string,
  isCancelled: () => boolean,
): Promise<void> {
  const event = await stream.take(
    (candidate) => candidate.kind === 'connectionStateChanged' &&
      candidate.commandId === commandId && candidate.connectionId === connectionId && candidate.connectionState === state,
    RF_CONNECTION_TIMEOUT_MS,
    `connection ${connectionId} state ${state ?? 'disconnected'}`,
    isCancelled,
  );
  if (event.error) throw new Error(`connection ${connectionId} failed:${event.error.category}:${event.error.message}`);
}

export async function runRfMulti(
  probe: Probe,
  rfServiceUuid: string | undefined,
  report: Report,
  isCancelled: () => boolean,
  notificationMode = 'both',
  subscriptionMode = 'notification',
  options: MultiRfOptions = {},
): Promise<void> {
  const serviceUuids = (rfServiceUuid ?? '').split(',').map((value) => value.trim()).filter(Boolean);
  if (!['both', 'only-dk-a', 'only-dk-b', 'reverse'].includes(notificationMode)) {
    throw new Error(`unsupported rf-multi route mode:${notificationMode}`);
  }
  if (subscriptionMode !== 'notification' && subscriptionMode !== 'indication') {
    throw new Error(`unsupported rf-multi subscription mode:${subscriptionMode}`);
  }
  const subscriptionLabel = subscriptionMode === 'indication' ? 'indication' : 'notification';
  const requestedMode = subscriptionMode === 'indication' ? 'require_indication' : 'platform_default';
  const expectedMode = subscriptionMode === 'indication' ? 'Indication' : 'Notification';
  const initialize = options.initialize ?? true;
  const closeProbe = options.closeProbe ?? true;
  const commandOffset = options.commandOffset ?? 0;
  const numericId = (base: number): string => String(base + commandOffset);
  let stream: MultiEventStream | undefined;
  let failure: unknown;
  const peripherals = new Map<string, string>();
  const connections = new Map<string, string>();
  const sessions = new Map<string, string>();
  const databases = new Map<string, ReturnType<typeof validateRfGattDatabase>>();
  try {
    if (serviceUuids.length !== 2 || new Set(serviceUuids).size !== 2) {
      throw new Error('rf-multi requires exactly two distinct service UUIDs');
    }
    stream = createMultiEventStream(probe);
    report('rf-multi:listener:registered-before-initialize');
    if (initialize) {
      const initializeCommand = numericId(9200);
      if (probe.initialize(initializeCommand)) throw new Error('initialize dispatch rejected');
      const adapter = await stream.take(
        (event) => event.kind === 'adapterSnapshotChanged' && event.adapterState === 'PoweredOn' && event.scanAuthorization === 'Authorized',
        RF_INITIALIZATION_TIMEOUT_MS,
        'multi adapter PoweredOn + Authorized',
        isCancelled,
      );
      await takeCompletion(stream, initializeCommand, isCancelled);
      report(`rf-multi:step:initialize:passed:commandId=${initializeCommand}:adapterState=${adapter.adapterState}:scanAuthorization=${adapter.scanAuthorization}`);
    } else {
      report(`rf-multi:step:initialize:passed:reused=true:commandId=${numericId(9200)}`);
    }

    if (probe.startRfScan(numericId(9201), numericId(9202), serviceUuids.join(','))) throw new Error('multi scan dispatch rejected');
    await stream.take((event) => event.kind === 'scanStarted' && event.commandId === numericId(9201) && event.scanSessionId === numericId(9202), RF_CONNECTION_TIMEOUT_MS, 'multi scan started', isCancelled);
    await takeCompletion(stream, numericId(9201), isCancelled);
    report(`rf-multi:step:scan-start:passed:commandId=${numericId(9201)}:scanSessionId=${numericId(9202)}:serviceUuids=${serviceUuids.join(',')}`);

    for (const [index, serviceUuid] of serviceUuids.entries()) {
      const advertisement = await stream.take(
        (event) => event.kind === 'advertisementObserved' && event.scanSessionId === numericId(9202) && event.advertisedServices?.includes(serviceUuid) === true,
        RF_CONNECTION_TIMEOUT_MS,
        `advertisement for ${serviceUuid}`,
        isCancelled,
      );
      if (!isCanonicalU64(advertisement.peripheralId)) throw new Error(`non-canonical multi peripheral ID:${advertisement.peripheralId ?? 'missing'}`);
      const peripheralId = advertisement.peripheralId;
      if (peripherals.has(peripheralId)) throw new Error(`duplicate multi peripheral ID:${peripheralId}`);
      peripherals.set(serviceUuid, peripheralId);
      report(`rf-multi:step:advertisement:passed:index=${index}:serviceUuid=${serviceUuid}:peripheralId=${peripheralId}:localName=${advertisement.localName ?? 'missing'}:payloadAdvertisedServices=${advertisement.advertisedServices?.join(',') ?? 'none'}`);
    }
    if (probe.stopRfScan(numericId(9203), numericId(9202))) throw new Error('multi scan stop dispatch rejected');
    await stream.take((event) => event.kind === 'scanStopped' && event.commandId === numericId(9203) && event.scanSessionId === numericId(9202) && !event.error, RF_CONNECTION_TIMEOUT_MS, 'multi scan stopped', isCancelled);
    await takeCompletion(stream, numericId(9203), isCancelled);
    report(`rf-multi:step:scan-stop:passed:commandId=${numericId(9203)}:scanSessionId=${numericId(9202)}:requested=true`);
    const scanEventCount = stream.events.length;
    await delay(RF_POST_STOP_OBSERVATION_MS);
    if (stream.events.slice(scanEventCount).some((event) => event.kind === 'advertisementObserved' && event.scanSessionId === numericId(9202))) throw new Error('multi advertisement arrived after scan stop');
    report(`rf-multi:step:silence:passed:scanSessionId=${numericId(9202)}:observationMs=${RF_POST_STOP_OBSERVATION_MS}`);

    for (const [index, serviceUuid] of serviceUuids.entries()) {
      const peripheralId = peripherals.get(serviceUuid);
      if (!peripheralId) throw new Error(`missing peripheral for ${serviceUuid}`);
      const commandId = numericId(9210 + index * 10);
      const connectionId = numericId(9220 + index * 10);
      connections.set(serviceUuid, connectionId);
      if (probe.connectRfPeripheral(commandId, connectionId, peripheralId)) throw new Error(`connect dispatch rejected:${serviceUuid}`);
      await takeConnectionState(stream, commandId, connectionId, 'Connecting', isCancelled);
      await takeConnectionState(stream, commandId, connectionId, 'Connected', isCancelled);
      await takeCompletion(stream, commandId, isCancelled);
      report(`rf-multi:step:connected:passed:index=${index}:commandId=${commandId}:connectionId=${connectionId}:peripheralId=${peripheralId}:serviceUuid=${serviceUuid}`);
    }

    for (const [index, serviceUuid] of serviceUuids.entries()) {
      const connectionId = connections.get(serviceUuid);
      const peripheralId = peripherals.get(serviceUuid);
      if (!connectionId || !peripheralId) throw new Error(`missing connection identity for ${serviceUuid}`);
      const commandId = numericId(9230 + index * 10);
      const gattSessionId = numericId(9240 + index * 10);
      sessions.set(serviceUuid, gattSessionId);
      if (probe.discoverRfGatt(commandId, connectionId, gattSessionId)) throw new Error(`GATT dispatch rejected:${serviceUuid}`);
      const discovered = await stream.take(
        (event) => event.kind === 'gattDiscovered' && event.commandId === commandId && event.connectionId === connectionId,
        RF_CONNECTION_TIMEOUT_MS,
        `GATT discovery for ${serviceUuid}`,
        isCancelled,
      );
      const database = validateRfGattDatabase(discovered, serviceUuid, connectionId);
      if (database.gattSessionId !== gattSessionId) throw new Error(`GATT session mismatch:${serviceUuid}`);
      databases.set(serviceUuid, database);
      await takeCompletion(stream, commandId, isCancelled);
      report(`rf-multi:step:gatt-discovery:passed:index=${index}:commandId=${commandId}:connectionId=${connectionId}:gattSessionId=${gattSessionId}:peripheralId=${peripheralId}:serviceUuid=${serviceUuid}:serviceCount=${database.serviceCount}:characteristicCount=${database.characteristicCount}:descriptorCount=${database.descriptorCount}`);
    }

    // Keep both native connections active while operations are interleaved. This
    // is the first multi-device boundary beyond identity discovery: each event
    // must remain correlated to its own connection, GATT session and characteristic.
    for (const operation of ['read-initial', 'write-response', 'readback'] as const) {
      for (const [index, serviceUuid] of serviceUuids.entries()) {
        const connectionId = connections.get(serviceUuid);
        const gattSessionId = sessions.get(serviceUuid);
        const database = databases.get(serviceUuid);
        const peripheralId = peripherals.get(serviceUuid);
        if (!connectionId || !gattSessionId || !database || !peripheralId) {
          throw new Error(`missing I/O identity:${serviceUuid}`);
        }
        const characteristicId = database.readWriteCharacteristicId;
        const baseCommandId = 9300 + index * 20 + commandOffset;
        const commandId = operation === 'read-initial'
          ? String(baseCommandId)
          : operation === 'write-response'
            ? String(baseCommandId + 1)
            : String(baseCommandId + 2);
        if (operation === 'write-response') {
          const value = new Uint8Array([0x10 + index * 0x10]);
          if (probe.writeRfCharacteristic(commandId, connectionId, gattSessionId, characteristicId, value.buffer, 'with_response')) {
            throw new Error(`write dispatch rejected:${serviceUuid}`);
          }
          const written = await stream.take(
            (event) => event.kind === 'characteristicWritten' && event.commandId === commandId && event.connectionId === connectionId && event.gattSessionId === gattSessionId && event.characteristicId === characteristicId,
            RF_CONNECTION_TIMEOUT_MS,
            `write response for ${serviceUuid}`,
            isCancelled,
          );
          if (written.error) throw new Error(`write failed:${serviceUuid}:${written.error.category}:${written.error.message}`);
          await takeCompletion(stream, commandId, isCancelled);
          report(`rf-multi:step:io:passed:index=${index}:operation=write-response:commandId=${commandId}:connectionId=${connectionId}:gattSessionId=${gattSessionId}:characteristicId=${characteristicId}:peripheralId=${peripheralId}:serviceUuid=${serviceUuid}:value=${(0x10 + index * 0x10).toString(16).padStart(2, '0')}`);
          continue;
        }
        if (probe.readRfCharacteristic(commandId, connectionId, gattSessionId, characteristicId)) {
          throw new Error(`read dispatch rejected:${serviceUuid}`);
        }
        const read = await stream.take(
          (event) => event.kind === 'characteristicRead' && event.commandId === commandId && event.connectionId === connectionId && event.gattSessionId === gattSessionId && event.characteristicId === characteristicId,
          RF_CONNECTION_TIMEOUT_MS,
          `${operation} for ${serviceUuid}`,
          isCancelled,
        );
        const expected = operation === 'read-initial' ? new Uint8Array([0x00]) : new Uint8Array([0x10 + index * 0x10]);
        if (read.error || !sameBytes(read.value, expected)) {
          throw new Error(`readback mismatch:${serviceUuid}:operation=${operation}`);
        }
        await takeCompletion(stream, commandId, isCancelled);
        report(`rf-multi:step:io:passed:index=${index}:operation=${operation}:commandId=${commandId}:connectionId=${connectionId}:gattSessionId=${gattSessionId}:characteristicId=${characteristicId}:peripheralId=${peripheralId}:serviceUuid=${serviceUuid}:value=${expected[0].toString(16).padStart(2, '0')}`);
      }
    }

    for (const [index, serviceUuid] of serviceUuids.entries()) {
      const connectionId = connections.get(serviceUuid);
      const peripheralId = peripherals.get(serviceUuid);
      if (!connectionId || !peripheralId) throw new Error(`missing RSSI identity:${serviceUuid}`);
      const commandId = numericId(9270 + index * 10);
      if (probe.readRfRssi(commandId, connectionId)) {
        throw new Error(`RSSI dispatch rejected:${serviceUuid}`);
      }
      const reading = await stream.take(
        (event) => event.kind === 'rssiRead' &&
          event.commandId === commandId && event.connectionId === connectionId,
        RF_CONNECTION_TIMEOUT_MS,
        `RSSI for ${serviceUuid}`,
        isCancelled,
      );
      if (reading.error || reading.rssi === undefined || reading.rssi < -127 || reading.rssi > 127) {
        throw new Error(`RSSI result invalid:${serviceUuid}:${reading.rssi ?? 'missing'}`);
      }
      await takeCompletion(stream, commandId, isCancelled);
      report(`rf-multi:step:rssi:passed:index=${index}:commandId=${commandId}:connectionId=${connectionId}:peripheralId=${peripheralId}:serviceUuid=${serviceUuid}:rssiDbm=${reading.rssi}`);
    }

    const subscriptions = new Map<string, { subscriptionId: string; characteristicId: string }>();
    const subscriptionIndexes = notificationMode === 'only-dk-a'
      ? [0]
      : notificationMode === 'only-dk-b'
        ? [1]
        : notificationMode === 'reverse'
          ? [1, 0]
          : [0, 1];
    for (const index of subscriptionIndexes) {
      const serviceUuid = serviceUuids[index];
      const connectionId = connections.get(serviceUuid);
      const gattSessionId = sessions.get(serviceUuid);
      const database = databases.get(serviceUuid);
      const peripheralId = peripherals.get(serviceUuid);
      if (!connectionId || !gattSessionId || !database || !peripheralId) {
        throw new Error(`missing notification identity:${serviceUuid}`);
      }
      const characteristicId = subscriptionMode === 'indication'
        ? database.indicationCharacteristicId
        : database.notificationCharacteristicId;
      const commandId = numericId(9560 + index * 10);
      const subscriptionId = numericId(9561 + index * 10);
      if (probe.setRfNotifications(commandId, connectionId, gattSessionId, subscriptionId, characteristicId, true, requestedMode)) {
        throw new Error(`${subscriptionLabel} enable dispatch rejected:${serviceUuid}`);
      }
      const enabled = await stream.take(
        (event) => event.kind === 'notificationStateChanged' &&
          event.commandId === commandId && event.connectionId === connectionId &&
          event.gattSessionId === gattSessionId && event.subscriptionId === subscriptionId &&
          event.characteristicId === characteristicId && event.notificationState === 'Enabled',
        RF_CONNECTION_TIMEOUT_MS,
        `${subscriptionLabel} enable for ${serviceUuid}`,
        isCancelled,
      );
      if (enabled.error || enabled.actualNotificationMode !== expectedMode) {
        throw new Error(`unexpected ${subscriptionLabel} mode:${serviceUuid}:${enabled.actualNotificationMode ?? 'missing'}`);
      }
      await takeCompletion(stream, commandId, isCancelled);
      subscriptions.set(serviceUuid, { subscriptionId, characteristicId });
      report(`rf-multi:step:${subscriptionLabel}-enable:passed:index=${index}:commandId=${commandId}:connectionId=${connectionId}:gattSessionId=${gattSessionId}:subscriptionId=${subscriptionId}:characteristicId=${characteristicId}:peripheralId=${peripheralId}:serviceUuid=${serviceUuid}:actualMode=${expectedMode}`);
      const value = await stream.take(
        (event) => event.kind === 'notificationReceived' &&
          event.connectionId === connectionId && event.gattSessionId === gattSessionId &&
          event.subscriptionId === subscriptionId && event.characteristicId === characteristicId,
        RF_CONNECTION_TIMEOUT_MS,
        `${subscriptionLabel} value for ${serviceUuid}`,
        isCancelled,
      );
      if (value.error || !sameBytes(value.value, new Uint8Array([0xef]))) {
        throw new Error(`${subscriptionLabel} value mismatch:${serviceUuid}`);
      }
      report(`rf-multi:step:${subscriptionLabel}-value:passed:index=${index}:connectionId=${connectionId}:gattSessionId=${gattSessionId}:subscriptionId=${subscriptionId}:characteristicId=${characteristicId}:peripheralId=${peripheralId}:serviceUuid=${serviceUuid}:value=ef`);
    }

    for (const index of subscriptionIndexes) {
      const serviceUuid = serviceUuids[index];
      const connectionId = connections.get(serviceUuid);
      const gattSessionId = sessions.get(serviceUuid);
      const peripheralId = peripherals.get(serviceUuid);
      const subscription = subscriptions.get(serviceUuid);
      if (!connectionId || !gattSessionId || !peripheralId || !subscription) {
        throw new Error(`missing notification disable identity:${serviceUuid}`);
      }
      const commandId = numericId(9580 + index * 10);
      if (probe.setRfNotifications(commandId, connectionId, gattSessionId, subscription.subscriptionId, subscription.characteristicId, false, requestedMode)) {
        throw new Error(`${subscriptionLabel} disable dispatch rejected:${serviceUuid}`);
      }
      await stream.take(
        (event) => event.kind === 'notificationStateChanged' &&
          event.commandId === commandId && event.connectionId === connectionId &&
          event.gattSessionId === gattSessionId && event.subscriptionId === subscription.subscriptionId &&
          event.characteristicId === subscription.characteristicId && event.notificationState === 'Disabled',
        RF_CONNECTION_TIMEOUT_MS,
        `${subscriptionLabel} disable for ${serviceUuid}`,
        isCancelled,
      );
      await takeCompletion(stream, commandId, isCancelled);
      const postDisableEventCount = stream.events.length;
      await delay(RF_POST_NOTIFICATION_DISABLE_OBSERVATION_MS);
      if (stream.events.slice(postDisableEventCount).some((event) =>
        event.kind === 'notificationReceived' && event.connectionId === connectionId &&
        event.gattSessionId === gattSessionId && event.subscriptionId === subscription.subscriptionId &&
        event.characteristicId === subscription.characteristicId)) {
        throw new Error(`stale ${subscriptionLabel} route after disable:${serviceUuid}`);
      }
      report(`rf-multi:step:${subscriptionLabel}-disable:passed:index=${index}:commandId=${commandId}:connectionId=${connectionId}:gattSessionId=${gattSessionId}:subscriptionId=${subscription.subscriptionId}:characteristicId=${subscription.characteristicId}:peripheralId=${peripheralId}:serviceUuid=${serviceUuid}:postDisableSilenceMs=${RF_POST_NOTIFICATION_DISABLE_OBSERVATION_MS}`);
    }

    for (const [index, serviceUuid] of serviceUuids.entries()) {
      const connectionId = connections.get(serviceUuid);
      const peripheralId = peripherals.get(serviceUuid);
      if (!connectionId || !peripheralId) throw new Error(`missing disconnect identity:${serviceUuid}`);
      const commandId = numericId(9250 + index * 10);
      if (probe.disconnectRfPeripheral(commandId, connectionId)) throw new Error(`disconnect dispatch rejected:${serviceUuid}`);
      await takeConnectionState(stream, commandId, connectionId, 'Disconnected', isCancelled);
      await takeCompletion(stream, commandId, isCancelled);
      report(`rf-multi:step:disconnected:passed:index=${index}:commandId=${commandId}:connectionId=${connectionId}:peripheralId=${peripheralId}:serviceUuid=${serviceUuid}`);
    }
    stream.remove();
    if (closeProbe) {
      await probe.close();
      report('rf-multi:step:close:passed:listener-removed=true:probe-closed=true');
    } else {
      report('rf-multi:step:close:passed:listener-removed=true:probe-closed=false:deferred=true');
    }
    report(`rf-multi:passed:peripheralCount=${peripherals.size}:connectionCount=${connections.size}:gattSessionCount=${sessions.size}:routeMode=${notificationMode}:subscriptionMode=${subscriptionMode}`);
  } catch (error: unknown) {
    failure = error;
    stream?.remove();
    report(`rf-multi:failed:${error instanceof Error ? error.message : String(error)}`);
    throw error;
  } finally {
    if (failure && closeProbe) await probe.close().catch(() => undefined);
  }
}

export async function runRfMultiSoak(
  probe: Probe,
  rfServiceUuid: string | undefined,
  report: Report,
  isCancelled: () => boolean,
  notificationMode = 'both',
  subscriptionMode = 'notification',
  cycles = 10,
): Promise<void> {
  if (!Number.isInteger(cycles) || cycles < 1 || cycles > 20) {
    throw new Error('rf-multi-soak cycles must be between 1 and 20');
  }
  let failed = false;
  report(`rf-multi-soak:listener:registered-before-initialize:cycles=${cycles}`);
  try {
    for (let cycle = 1; cycle <= cycles; cycle += 1) {
      report(`rf-multi-soak:cycle:${cycle}:started`);
      await runRfMulti(
        probe,
        rfServiceUuid,
        report,
        isCancelled,
        notificationMode,
        subscriptionMode,
        {
          initialize: cycle === 1,
          closeProbe: false,
          commandOffset: (cycle - 1) * 1000,
        },
      );
      report(`rf-multi-soak:cycle:${cycle}:passed`);
    }
    await probe.close();
    report('rf-multi-soak:close:passed:probe-closed=true');
    report(`rf-multi-soak:passed:cycles=${cycles}:routeMode=${notificationMode}:subscriptionMode=${subscriptionMode}`);
  } catch (error: unknown) {
    failed = true;
    report(`rf-multi-soak:failed:${error instanceof Error ? error.message : String(error)}`);
    throw error;
  } finally {
    if (failed) await probe.close().catch(() => undefined);
  }
}
