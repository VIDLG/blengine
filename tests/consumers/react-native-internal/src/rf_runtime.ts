// Shared by both executors: a JS engine reload must never look like a phase transition.
export const RF_BOOT_NONCE = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
