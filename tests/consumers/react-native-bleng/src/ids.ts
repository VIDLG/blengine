/**
 * The one accessibility-identifier vocabulary for the formal RN host.
 *
 * Every `testID` in this App comes from here and from nowhere else. That matters
 * because the same string is the *only* contract between three independent
 * drivers - our XCTest scene target, Android instrumented tests, and the
 * host-side scene code - and an identifier typo otherwise fails on a real device
 * minutes later instead of in CI.
 *
 * React Native maps `testID` to `accessibilityIdentifier` on iOS and to the
 * view's test tag / resource id on Android. Any native view added later (a Nitro
 * hybrid view, `requireNativeComponent`) must set the same string explicitly:
 * `accessibilityIdentifier` on iOS, `contentDescription` + tag on Android.
 * `tools/tests/react_native/test_workbench_ids.py` enforces both rules.
 */

const frozen = <T extends Record<string, unknown>>(value: T): T =>
  Object.freeze(value) as T

export const Ids = frozen({
  /** Page chrome and session controls. */
  workbench: frozen({
    title: 'formal-host-title',
    page: 'workbench-page',
    state: 'session-state',
    error: 'session-error',
    retirementWarning: 'session-retirement-warning',
    content: 'workbench-content',
    sessionControls: 'session-controls',
    startSession: 'start-session',
    closeSession: 'close-session',
    runStatus: 'run-status',
    scanLatest: 'scan-latest',
    notifyLatest: 'notify-latest',
    lifecycleEvidence: 'lifecycle-evidence',
    actionLedger: 'action-ledger',
    evidenceRun: 'evidence-run',
    evidenceOmitted: 'evidence-omitted',
  }),

  /** Bottom navigation; `page()` is the composed identifier a driver taps. */
  nav: frozen({
    tests: 'workbench-tests',
    current: 'workbench-current',
    probe: 'workbench-probe',
    evidence: 'workbench-evidence',
    page: (identifier: string): string => `workbench-${identifier}`,
  }),

  /** Scan projection and configuration. */
  scan: frozen({
    configuration: 'scan-configuration',
    filter: 'scan-filter',
    filterError: 'scan-filter-error',
    summary: 'scan-summary',
    state: 'scan-state',
    count: 'scan-count',
    accepted: 'scan-accepted',
    error: 'scan-error',
    ended: 'scan-ended',
    resource: 'scan-resource',
    sampleViewport: 'scan-sample-viewport',
    samples: 'scan-samples',
    primaryControls: 'scan-primary-controls',
    cancelStart: 'cancel-scan-start',
    cancelStop: 'cancel-scan-stop',
  }),

  /** Connection projection. */
  link: frozen({
    controls: 'link-controls',
    state: 'link-state',
    detail: 'link-detail',
  }),

  /** GATT projection. */
  gatt: frozen({
    actionStrip: 'gatt-action-strip',
    panel: 'gatt-panel',
    summary: 'gatt-summary',
    rows: 'gatt-rows',
    state: 'gatt-state',
    targetStatus: 'gatt-target-status',
    result: 'gatt-result',
    error: 'gatt-error',
    cancel: 'gatt-cancel',
  }),

  /** Notification/indication projection. */
  notify: frozen({
    actionStrip: 'notify-action-strip',
    panel: 'notify-panel',
    mode: 'notify-mode',
    count: 'notify-count',
    samples: 'notify-samples',
    state: 'notify-state',
    error: 'notify-error',
  }),

  rssi: frozen({
    value: 'rssi-value',
    error: 'rssi-error',
  }),

  command: frozen({
    console: 'command-console',
    outcome: 'command-outcome',
  }),

  /** The Probe page: capabilities that are not part of the observation main line. */
  probe: frozen({
    console: 'probe-console',
    autoStrip: 'probe-auto-strip',
    inventoryStrip: 'probe-inventory-strip',
    radioStrip: 'probe-radio-strip',
  }),

  /** Identifiers of the interactive controls themselves. */
  controls: frozen({
    startScan: 'start-scan',
    nextScanEvent: 'next-scan-event',
    stopScan: 'stop-scan',
    connectPeripheral: 'connect-peripheral',
    disconnectPeripheral: 'disconnect-peripheral',
    discoverGatt: 'discover-gatt',
    readRssi: 'read-rssi',
    mtuQuery: 'mtu-query',
    mtuRequest: 'mtu-request',
    priorityHigh: 'priority-high',
    refreshGatt: 'refresh-gatt',
    devicesQuery: 'devices-query',
    devicesBonded: 'devices-bonded',
    devicesCompanion: 'devices-companion',
    autoConnect: 'auto-connect',
    radioOff: 'radio-off',
    radioOn: 'radio-on',
    descriptorRead: 'descriptor-read',
    descriptorWrite: 'descriptor-write',
    gattTarget: 'gatt-target',
    gattRead: 'gatt-read',
    gattWrite: 'gatt-write',
    gattSubscribe: 'gatt-subscribe',
    gattWriteValue: 'gatt-write-value',
    cancelGattRead: 'cancel-gatt-read',
    pullNotification: 'pull-notification',
    disableNotifications: 'disable-notifications',
    scanObservationTimeout: 'scan-observation-timeout',
    scanServiceFilter: 'scan-service-filter',
  }),

  /** Adapter snapshot facts, optionally prefixed by their page. */
  snapshot: frozen({
    adapterState: (prefix = ''): string => `${prefix}adapter-state`,
    scanAuthorization: (prefix = ''): string => `${prefix}scan-authorization`,
    connectionAuthorization: (prefix = ''): string =>
      `${prefix}connection-authorization`,
    adapterName: (prefix = ''): string => `${prefix}adapter-name`,
  }),
})

export type IdVocabulary = typeof Ids
