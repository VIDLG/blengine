import React from 'react';
import { CCC_ENABLE_REJECT_STEPS, cccRejectSession, RfCccRejectLab, runRfCccReject, type CccRejectProfile, type CccRejectRunner } from './rf_ccc_reject';

export const CCC_REJECT_STEPS = CCC_ENABLE_REJECT_STEPS;
export const cccEnableRejectSession = (raw?: string): string | undefined => cccRejectSession(raw, 'rf-ccc-enable-reject');
export const runRfCccEnableReject: CccRejectRunner = (probe, profile, report, cancelled) =>
  runRfCccReject(probe, profile, report, cancelled, 'rf-ccc-enable-reject');
export function RfCccEnableRejectLab(profile: CccRejectProfile): React.JSX.Element {
  return <RfCccRejectLab {...profile} mode="rf-ccc-enable-reject" />;
}
