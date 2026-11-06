import { NitroModules } from 'react-native-nitro-modules'
import type { BlengSessionOwner } from '../specs/BlengSessionOwner.nitro'

export type {
  BlengAdapterSnapshot,
  BlengAdapterState,
  BlengAuthorizationState,
  BlengCloseFailure,
  BlengCloseResult,
  BlengSessionOwner,
} from '../specs/BlengSessionOwner.nitro'

export type {
  BlengScan, BlengScanOptions, BlengScanSubmission, BlengScanStop, BlengScanStopSubmission,
  BlengScanNext, BlengScanCommandResult, BlengScanFailure, BlengScanFailureInfo,
  BlengScanFailureDomain, BlengScanPlatform, BlengScanPlatformDetails,
  BlengScanAdvertisement, BlengScanAdvertisementEvent, BlengScanEnd,
  BlengScanEndReason, BlengScanEndedEvent, BlengPeripheral,
  BlengManufacturerData, BlengServiceData, BlengScanAccepted, BlengScanStopAccepted,
} from '../specs/BlengScan.nitro'

/** Typed Apple/Android bridge; construction does not initialize BLE. */
export function createBlengSessionOwner(): BlengSessionOwner {
  return NitroModules.createHybridObject<BlengSessionOwner>('BlengSessionOwner')
}
