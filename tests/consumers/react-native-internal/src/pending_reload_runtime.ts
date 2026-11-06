import type { createBleHubProbe } from '@blehub/react-native-internal';

/** Host-only JSI binding. Installed only after requiring the iOS lifecycle module. */
export function bindPendingReloadProbe(probe: ReturnType<typeof createBleHubProbe>): void {
  const bind = (globalThis as typeof globalThis & {
    __blehubPendingReloadBindProbe?: (value: unknown) => void;
  }).__blehubPendingReloadBindProbe;
  if (!bind) throw new Error('native pending-reload runtime binding was not installed');
  bind(probe);
}
