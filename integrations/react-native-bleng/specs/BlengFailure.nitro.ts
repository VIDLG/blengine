import type { HybridObject } from 'react-native-nitro-modules'

/**
 * One failure projection for every BLeng operation view.
 *
 * The domain is fixed by Rust and describes where in the operation path the failure was observed;
 * it is not a second BLE taxonomy. `code` is stable and is the only field safe for control flow.
 */
export type BlengFailureDomain =
  | 'input'
  | 'submission'
  | 'operation'
  | 'observation'
  | 'delivery'
  | 'lifecycle'

// Diagnostic platform names are data, not control-state enums. A string also avoids the NDK's
// ANDROID macro colliding with Nitrogen's generated C++ enum member.
export type BlengPlatform = string

export interface BlengPlatformDetails {
  platform: BlengPlatform
  code?: string
  name?: string
  message?: string
  /** Numeric platform status when the platform reports one. */
  status?: number
  /** Bluetooth ATT error code when the failure came from an ATT response. */
  attCode?: number
}

/** Classified in Rust; `message` is for display and must never drive control flow. */
export interface BlengFailureInfo {
  domain: BlengFailureDomain
  code: string
  message: string
  platform?: BlengPlatformDetails
}

/**
 * Union member used by every operation view.
 *
 * `failure.domain` is a closed literal union, so `isBlengFailure` narrows on that discriminant rather
 * than on mere key presence. Success arms carry their own keys (`scan`, `value`, ...) and no tag.
 */
export interface BlengFailure {
  failure: BlengFailureInfo
}

/** Absence of failure means this call succeeded; cancel is a receipt, not remote rollback. */
export interface BlengReceipt {
  failure?: BlengFailureInfo
}
