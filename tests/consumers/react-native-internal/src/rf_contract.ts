import type { RfScanEvent } from '@blehub/react-native-internal/specs/BleHubProbe.nitro';

export const MAX_U64 = '18446744073709551615';
export const LISTENER_REMOVAL_COMMAND = '1';
export const LISTENER_REMOVAL_WAIT_MS = 100;
export const COMPLETION_TIMEOUT_MS = 5_000;
export const RF_INITIALIZE_COMMAND = '9001';
export const RF_START_COMMAND = '9002';
export const RF_STOP_COMMAND = '9003';
export const RF_SCAN_SESSION = '9004';
export const RF_CONNECT_COMMAND = '9005';
export const RF_DISCONNECT_COMMAND = '9006';
export const RF_CONNECTION_ID = '9007';
export const RF_GATT_COMMAND = '9008';
export const RF_GATT_SESSION = '9009';
export const RF_READ_INITIAL_COMMAND = '9010';
export const RF_WRITE_RESPONSE_COMMAND = '9011';
export const RF_READ_RESPONSE_COMMAND = '9012';
export const RF_WRITE_WITHOUT_RESPONSE_COMMAND = '9013';
export const RF_READ_WITHOUT_RESPONSE_COMMAND = '9014';
export const RF_NOTIFICATION_ENABLE_COMMAND = '9015';
export const RF_NOTIFICATION_DISABLE_COMMAND = '9016';
export const RF_NOTIFICATION_SUBSCRIPTION = '9017';
export const RF_INDICATION_REJECTION_COMMAND = '9018';
export const RF_INDICATION_ENABLE_COMMAND = '9019';
export const RF_INDICATION_DISABLE_COMMAND = '9020';
export const RF_INDICATION_SUBSCRIPTION = '9021';
export const RF_INDICATION_REJECTED_SUBSCRIPTION = '9022';
export const RF_RECOVERY_CONNECT_COMMAND = '9023';
export const RF_RECOVERY_CONNECTION_ID = '9024';
export const RF_RECOVERY_DISCONNECT_COMMAND = '9025';
export const RF_RSSI_COMMAND = '9026';
export const RF_CONNECTION_TIMEOUT_MS = 45_000;
export const RF_POST_DISCONNECT_OBSERVATION_MS = 1_000;
export const RF_INITIALIZATION_TIMEOUT_MS = 120_000;
export const RF_SCAN_TIMEOUT_MS = 45_000;
export const RF_POST_STOP_OBSERVATION_MS = 2_000;
export const RF_POST_NOTIFICATION_DISABLE_OBSERVATION_MS = 2_000;
export const MAX_RF_EVENT_COUNT = 256;
export const RF_EXPECTED_LOCAL_NAME = 'BleHubNrf52840';
export const RF_PENDING_READ_EXPECTED_LOCAL_NAME = 'BleHubAttHoldDK';
export const PENDING_READ_GENERATION_KEY = 'blehub.nitro.rfPendingReadReloadGeneration';
export const PENDING_READ_ARM_TIMEOUT_MS = 30_000;
export const STRESS_CYCLES = 8;
export const STRESS_WAIT_MS = 150;
export const SAMPLE_BYTES = new Uint8Array([0x01, 0x02, 0x7f, 0x00]);
export const INVALID_COMMAND_IDS = ['', '00', '01', '-1', '1.0', '18446744073709551616'];

export type RuntimeMode =
  | 'basic'
  | 'stress'
  | 'reload'
  | 'rf'
  | 'rf-connect'
  | 'rf-gatt'
  | 'rf-io'
  | 'rf-rssi'
  | 'rf-notification'
  | 'rf-indication'
  | 'rf-passive-disconnect'
  | 'rf-recovery'
  | 'rf-recovery-ten'
  | 'rf-multi'
  | 'rf-multi-soak'
  | 'rf-multi-fault'
  | 'rf-reload'
  | 'rf-pending-read-reload';
export type RfSubscriptionMode = 'notification' | 'indication';
export type StepState = 'pending' | 'passed' | 'failed';
export type StepId = 'round-trip' | 'dispatch' | 'completion' | 'listener-removal' | 'close';
export type RfStepId =
  | 'initialize'
  | 'scan-start'
  | 'advertisement'
  | 'scan-stop'
  | 'silence'
  | 'connecting'
  | 'connected'
  | 'gatt-discovery'
  | 'rssi'
  | 'read-initial'
  | 'write-response'
  | 'read-response'
  | 'write-without-response'
  | 'read-without-response'
  | 'indication-rejection'
  | 'notification-enable'
  | 'notification-value'
  | 'notification-disable'
  | 'indication-enable'
  | 'indication-value'
  | 'indication-disable'
  | 'disconnected'
  | 'connection-silence'
  | 'recovery-connecting'
  | 'recovery-connected'
  | 'recovery-disconnected'
  | 'recovery-connection-silence'
  | `cycle-${number}`
  | 'close';

export interface Step {
  id: StepId;
  label: string;
  state: StepState;
  detail: string;
}

export interface RfStep {
  id: RfStepId;
  label: string;
  state: StepState;
  detail: string;
}

export function isRfMode(mode: RuntimeMode): boolean {
  return mode === 'rf' || mode === 'rf-connect' || mode === 'rf-gatt' || mode === 'rf-io' || mode === 'rf-rssi' || mode === 'rf-notification' || mode === 'rf-indication' || mode === 'rf-passive-disconnect' || mode === 'rf-recovery' || mode === 'rf-recovery-ten' || mode === 'rf-multi' || mode === 'rf-multi-soak' || mode === 'rf-multi-fault' || mode === 'rf-reload' || mode === 'rf-pending-read-reload';
}

export function rfMarkerPrefix(mode: RuntimeMode): string {
  return mode === 'rf-pending-read-reload' ? 'rf-pending-read-reload' : mode === 'rf-reload' ? 'rf-reload' : mode === 'rf-recovery-ten' ? 'rf-recovery-ten' : mode === 'rf-recovery' ? 'rf-recovery' : mode === 'rf-multi-fault' ? 'rf-multi-fault' : mode === 'rf-multi-soak' ? 'rf-multi-soak' : mode === 'rf-multi' ? 'rf-multi' : mode === 'rf-passive-disconnect' ? 'rf-passive-disconnect' : mode === 'rf-indication' ? 'rf-indication' : mode === 'rf-notification' ? 'rf-notification' : mode === 'rf-io' ? 'rf-io' : mode === 'rf-rssi' ? 'rf-rssi' : mode === 'rf-gatt' ? 'rf-gatt' : mode === 'rf-connect' ? 'rf-connect' : 'rf';
}

export const INITIAL_STEPS: Step[] = [
  { id: 'round-trip', label: 'ArrayBuffer and typed error transport', state: 'pending', detail: 'waiting' },
  { id: 'dispatch', label: 'Command dispatch accepted', state: 'pending', detail: 'waiting' },
  { id: 'completion', label: 'Asynchronous max-u64 completion', state: 'pending', detail: 'waiting' },
  { id: 'listener-removal', label: 'Listener removal blocks late delivery', state: 'pending', detail: 'waiting' },
  { id: 'close', label: 'Explicit close resolves', state: 'pending', detail: 'waiting' },
];

export const INITIAL_RF_STEPS: RfStep[] = [
  { id: 'initialize', label: 'PoweredOn + Authorized and initialize completion', state: 'pending', detail: 'waiting' },
  { id: 'scan-start', label: 'scanStarted and start command completion', state: 'pending', detail: 'waiting' },
  { id: 'advertisement', label: 'Matching nRF service advertisement', state: 'pending', detail: 'waiting' },
  { id: 'scan-stop', label: 'Requested scanStopped and stop command completion', state: 'pending', detail: 'waiting' },
  { id: 'silence', label: 'Two-second post-stop same-session silence', state: 'pending', detail: 'waiting' },
  { id: 'close', label: 'RF listener removal and probe close', state: 'pending', detail: 'waiting' },
];

export const INITIAL_RF_CONNECT_STEPS: RfStep[] = [
  ...INITIAL_RF_STEPS.slice(0, -1),
  { id: 'connecting', label: 'Matching Connecting', state: 'pending', detail: 'waiting' },
  { id: 'connected', label: 'Matching Connected and connect completion', state: 'pending', detail: 'waiting' },
  { id: 'disconnected', label: 'Requested Disconnected and disconnect completion', state: 'pending', detail: 'waiting' },
  { id: 'connection-silence', label: 'One-second post-disconnect JS silence', state: 'pending', detail: 'waiting' },
  INITIAL_RF_STEPS[INITIAL_RF_STEPS.length - 1],
];

export const INITIAL_RF_RSSI_STEPS: RfStep[] = [
  ...INITIAL_RF_CONNECT_STEPS.slice(0, -3),
  { id: 'rssi', label: 'Connected CoreBluetooth RSSI and terminal', state: 'pending', detail: 'waiting' },
  ...INITIAL_RF_CONNECT_STEPS.slice(-3),
];

export const INITIAL_RF_GATT_STEPS: RfStep[] = [
  ...INITIAL_RF_STEPS.slice(0, -1),
  { id: 'connecting', label: 'Matching Connecting', state: 'pending', detail: 'waiting' },
  { id: 'connected', label: 'Matching Connected and connect completion', state: 'pending', detail: 'waiting' },
  { id: 'gatt-discovery', label: 'Aggregate GATT database and instance mapping', state: 'pending', detail: 'waiting' },
  { id: 'disconnected', label: 'Requested Disconnected and disconnect completion', state: 'pending', detail: 'waiting' },
  { id: 'connection-silence', label: 'One-second post-disconnect JS silence', state: 'pending', detail: 'waiting' },
  INITIAL_RF_STEPS[INITIAL_RF_STEPS.length - 1],
];

export const INITIAL_RF_IO_STEPS: RfStep[] = [
  ...INITIAL_RF_STEPS.slice(0, -1),
  { id: 'connecting', label: 'Matching Connecting', state: 'pending', detail: 'waiting' },
  { id: 'connected', label: 'Matching Connected and connect completion', state: 'pending', detail: 'waiting' },
  { id: 'gatt-discovery', label: 'Aggregate GATT database and instance mapping', state: 'pending', detail: 'waiting' },
  { id: 'read-initial', label: 'Initial read of read-write characteristic', state: 'pending', detail: 'waiting' },
  { id: 'write-response', label: 'Write with response and terminal', state: 'pending', detail: 'waiting' },
  { id: 'read-response', label: 'Readback after write with response', state: 'pending', detail: 'waiting' },
  { id: 'write-without-response', label: 'Write without response local submission', state: 'pending', detail: 'waiting' },
  { id: 'read-without-response', label: 'Readback after write without response', state: 'pending', detail: 'waiting' },
  { id: 'disconnected', label: 'Requested Disconnected and disconnect completion', state: 'pending', detail: 'waiting' },
  { id: 'connection-silence', label: 'One-second post-disconnect JS silence', state: 'pending', detail: 'waiting' },
  INITIAL_RF_STEPS[INITIAL_RF_STEPS.length - 1],
];

export const INITIAL_RF_NOTIFICATION_STEPS: RfStep[] = [
  ...INITIAL_RF_STEPS.slice(0, -1),
  { id: 'connecting', label: 'Matching Connecting', state: 'pending', detail: 'waiting' },
  { id: 'connected', label: 'Matching Connected and connect completion', state: 'pending', detail: 'waiting' },
  { id: 'gatt-discovery', label: 'Aggregate GATT database and instance mapping', state: 'pending', detail: 'waiting' },
  { id: 'notification-enable', label: 'Enable PlatformDefault notification route', state: 'pending', detail: 'waiting' },
  { id: 'notification-value', label: 'Receive exact notification value', state: 'pending', detail: 'waiting' },
  { id: 'notification-disable', label: 'Disable route and terminal completion', state: 'pending', detail: 'waiting' },
  { id: 'disconnected', label: 'Requested Disconnected and disconnect completion', state: 'pending', detail: 'waiting' },
  { id: 'connection-silence', label: 'One-second post-disconnect JS silence', state: 'pending', detail: 'waiting' },
  INITIAL_RF_STEPS[INITIAL_RF_STEPS.length - 1],
];

export const INITIAL_RF_INDICATION_STEPS: RfStep[] = [
  ...INITIAL_RF_STEPS.slice(0, -1),
  { id: 'connecting', label: 'Matching Connecting', state: 'pending', detail: 'waiting' },
  { id: 'connected', label: 'Matching Connected and connect completion', state: 'pending', detail: 'waiting' },
  { id: 'gatt-discovery', label: 'Aggregate GATT database and instance mapping', state: 'pending', detail: 'waiting' },
  { id: 'indication-rejection', label: 'Reject strict Indication on dual-property characteristic', state: 'pending', detail: 'waiting' },
  { id: 'indication-enable', label: 'Enable strict Indication route', state: 'pending', detail: 'waiting' },
  { id: 'indication-value', label: 'Receive exact indication value', state: 'pending', detail: 'waiting' },
  { id: 'indication-disable', label: 'Disable indication route and terminal completion', state: 'pending', detail: 'waiting' },
  { id: 'disconnected', label: 'Requested Disconnected and disconnect completion', state: 'pending', detail: 'waiting' },
  { id: 'connection-silence', label: 'One-second post-disconnect JS silence', state: 'pending', detail: 'waiting' },
  INITIAL_RF_STEPS[INITIAL_RF_STEPS.length - 1],
];

export const INITIAL_RF_RECOVERY_STEPS: RfStep[] = [
  ...[0, 1, 2].map((cycle) => ({ id: `cycle-${cycle}` as RfStepId, label: `Passive disconnect and recovery cycle ${cycle + 1}`, state: 'pending' as StepState, detail: 'waiting' })),
  { id: 'close', label: 'One listener removed and one probe closed', state: 'pending', detail: 'waiting' },
];

export const INITIAL_RF_RECOVERY_TEN_STEPS: RfStep[] = [
  ...Array.from({ length: 10 }, (_, cycle) => ({ id: `cycle-${cycle}` as RfStepId, label: `Passive disconnect and recovery cycle ${cycle + 1}`, state: 'pending' as StepState, detail: 'waiting' })),
  INITIAL_RF_RECOVERY_STEPS[INITIAL_RF_RECOVERY_STEPS.length - 1],
];

export const INITIAL_RF_PASSIVE_DISCONNECT_STEPS: RfStep[] = [
  ...INITIAL_RF_STEPS.slice(0, -1),
  { id: 'connecting', label: 'Matching Connecting', state: 'pending', detail: 'waiting' },
  { id: 'connected', label: 'Matching Connected and connect completion', state: 'pending', detail: 'waiting' },
  { id: 'gatt-discovery', label: 'Aggregate GATT database and instance mapping', state: 'pending', detail: 'waiting' },
  { id: 'notification-enable', label: 'Enable notification route', state: 'pending', detail: 'waiting' },
  { id: 'notification-value', label: 'Receive exact notification before fault', state: 'pending', detail: 'waiting' },
  { id: 'disconnected', label: 'Passive Disconnected without command ID', state: 'pending', detail: 'waiting' },
  { id: 'connection-silence', label: 'One-second stale-route silence after passive disconnect', state: 'pending', detail: 'waiting' },
  { id: 'recovery-connecting', label: 'Recovery Connecting', state: 'pending', detail: 'waiting' },
  { id: 'recovery-connected', label: 'Recovery Connected and connect completion', state: 'pending', detail: 'waiting' },
  { id: 'recovery-disconnected', label: 'Recovery requested disconnect', state: 'pending', detail: 'waiting' },
  { id: 'recovery-connection-silence', label: 'Recovery post-disconnect silence', state: 'pending', detail: 'waiting' },
  INITIAL_RF_STEPS[INITIAL_RF_STEPS.length - 1],
];

export function isCanonicalU64(value: string | undefined): value is string {
  if (value === undefined || value === '') return false;
  if (value === '0') return true;
  if (!/^[1-9][0-9]*$/.test(value)) return false;
  return value.length < MAX_U64.length || (value.length === MAX_U64.length && value <= MAX_U64);
}

const EXPECTED_RF_GATT_CHARACTERISTICS = [
  {
    uuid: 'fff10000-0000-4000-8000-000000000000',
    readable: true,
    writableWithResponse: true,
    writableWithoutResponse: true,
    notifiable: false,
    indicatable: false,
    descriptorUuids: [],
  },
  {
    uuid: 'fff20000-0000-4000-8000-000000000000',
    readable: true,
    writableWithResponse: false,
    writableWithoutResponse: false,
    notifiable: true,
    indicatable: true,
    descriptorUuids: ['00002902-0000-1000-8000-00805f9b34fb'],
  },
  {
    uuid: 'fff50000-0000-4000-8000-000000000000',
    readable: true,
    writableWithResponse: false,
    writableWithoutResponse: false,
    notifiable: false,
    indicatable: true,
    descriptorUuids: ['00002902-0000-1000-8000-00805f9b34fb'],
  },
  {
    uuid: 'fff30000-0000-4000-8000-000000000000',
    readable: true,
    writableWithResponse: false,
    writableWithoutResponse: false,
    notifiable: false,
    indicatable: false,
    descriptorUuids: [],
  },
  {
    uuid: 'fff40000-0000-4000-8000-000000000000',
    readable: false,
    writableWithResponse: true,
    writableWithoutResponse: true,
    notifiable: false,
    indicatable: false,
    descriptorUuids: [],
  },
] as const;

export interface RfGattValidation {
  gattSessionId: string;
  serviceCount: number;
  characteristicCount: number;
  descriptorCount: number;
  discoveredServiceCount: number;
  readWriteCharacteristicId: string;
  notificationCharacteristicId: string;
  indicationCharacteristicId: string;
}

export function validateRfGattDatabase(
  event: RfScanEvent,
  serviceUuid: string,
  connectionId = RF_CONNECTION_ID,
): RfGattValidation {
  if (event.kind !== 'gattDiscovered' || event.connectionId !== connectionId) {
    throw new Error(`unexpected GATT discovery event: kind=${event.kind}:connectionId=${event.connectionId ?? 'missing'}`);
  }
  if (!isCanonicalU64(event.gattSessionId)) {
    throw new Error(`GATT session ID is not canonical u64: ${event.gattSessionId ?? 'missing'}`);
  }
  const services = event.gattServices ?? [];
  const matchingServices = services.filter((candidate) => candidate.uuid === serviceUuid);
  if (matchingServices.length !== 1) {
    throw new Error(`expected one canonical GATT service ${serviceUuid}, got ${matchingServices.length} of ${services.length} discovered services`);
  }
  const service = matchingServices[0];
  if (service.primary !== true) {
    throw new Error(`unexpected canonical GATT service: uuid=${service.uuid}:primary=${String(service.primary)}`);
  }

  const ids = new Set<string>();
  let readWriteCharacteristicId: string | undefined;
  let notificationCharacteristicId: string | undefined;
  let indicationCharacteristicId: string | undefined;
  const requireUniqueId = (id: string, kind: string): void => {
    if (!isCanonicalU64(id) || ids.has(id)) throw new Error(`invalid or duplicate ${kind} ID: ${id}`);
    ids.add(id);
  };
  requireUniqueId(service.id, 'service');
  if (service.characteristics.length !== EXPECTED_RF_GATT_CHARACTERISTICS.length) {
    throw new Error(`expected ${EXPECTED_RF_GATT_CHARACTERISTICS.length} characteristics, got ${service.characteristics.length}`);
  }

  for (const expected of EXPECTED_RF_GATT_CHARACTERISTICS) {
    const matches = service.characteristics.filter((characteristic) => characteristic.uuid === expected.uuid);
    if (matches.length !== 1) throw new Error(`expected one characteristic ${expected.uuid}, got ${matches.length}`);
    const characteristic = matches[0];
    requireUniqueId(characteristic.id, 'characteristic');
    if (expected.uuid === 'fff10000-0000-4000-8000-000000000000') readWriteCharacteristicId = characteristic.id;
    if (expected.uuid === 'fff20000-0000-4000-8000-000000000000') notificationCharacteristicId = characteristic.id;
    if (expected.uuid === 'fff50000-0000-4000-8000-000000000000') indicationCharacteristicId = characteristic.id;
    for (const property of ['readable', 'writableWithResponse', 'writableWithoutResponse', 'notifiable', 'indicatable'] as const) {
      if (characteristic[property] !== expected[property]) {
        throw new Error(`characteristic ${expected.uuid} property ${property} mismatch`);
      }
    }
    const descriptorUuids = characteristic.descriptors.map((descriptor) => descriptor.uuid);
    if (descriptorUuids.length !== expected.descriptorUuids.length || descriptorUuids.some((uuid, index) => uuid !== expected.descriptorUuids[index])) {
      throw new Error(`characteristic ${expected.uuid} descriptor mapping mismatch`);
    }
    for (const descriptor of characteristic.descriptors) requireUniqueId(descriptor.id, 'descriptor');
  }

  if (!readWriteCharacteristicId) throw new Error('read-write characteristic ID was not discovered');
  if (!notificationCharacteristicId) throw new Error('notification characteristic ID was not discovered');
  if (!indicationCharacteristicId) throw new Error('indication characteristic ID was not discovered');
  return {
    gattSessionId: event.gattSessionId,
    serviceCount: matchingServices.length,
    characteristicCount: service.characteristics.length,
    descriptorCount: service.characteristics.reduce((count, characteristic) => count + characteristic.descriptors.length, 0),
    discoveredServiceCount: services.length,
    readWriteCharacteristicId,
    notificationCharacteristicId,
    indicationCharacteristicId,
  };
}
