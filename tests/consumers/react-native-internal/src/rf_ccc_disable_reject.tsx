import React from 'react';
import { CCC_DISABLE_REJECT_STEPS, cccRejectSession, RfCccRejectLab, runRfCccReject, type CccRejectProfile, type CccRejectRunner } from './rf_ccc_reject';

export { CCC_DISABLE_REJECT_STEPS };
export const cccDisableRejectSession = (raw?: string): string | undefined => cccRejectSession(raw, 'rf-ccc-disable-reject');
export const runRfCccDisableReject: CccRejectRunner = (probe, profile, report, cancelled) =>
  runRfCccReject(probe, profile, report, cancelled, 'rf-ccc-disable-reject');
export function RfCccDisableRejectLab(profile: CccRejectProfile): React.JSX.Element {
  return <RfCccRejectLab {...profile} mode="rf-ccc-disable-reject" />;
}
