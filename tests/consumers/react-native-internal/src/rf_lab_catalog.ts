import type { RuntimeMode } from './rf_contract';

export type TestPlatform = 'ios' | 'android';
export type ExecutionKind = 'interactive' | 'host' | 'planned';
export type FixtureRequirement = 'none' | 'single-DK' | 'two-DK';
export type AdapterMode = 'startup-off' | 'active-loss';
export type TestCategoryId = 'full-suite' | 'diagnostics' | 'lifecycle' | 'multi-peripheral' | 'abi-runtime';

export interface TestCategory {
  readonly id: TestCategoryId;
  readonly title: string;
  readonly description: string;
}

export interface TestScenario {
  readonly id: string;
  readonly category: TestCategoryId;
  readonly title: string;
  readonly description: string;
  readonly runtimeModes: readonly RuntimeMode[];
  /** Disambiguates rf-lab-suite: true means rfRecoveryRunId was supplied. */
  readonly fullSuite?: boolean;
  /** Disambiguates rf-adapter-transition; omitted launch mode defaults to startup-off. */
  readonly adapterMode?: AdapterMode;
  /** Maintained RN lanes, not a record of passing hardware validation; planned lanes are empty. */
  readonly supportedPlatforms: readonly TestPlatform[];
  readonly executionKind: ExecutionKind;
  readonly fixtureRequirement: FixtureRequirement;
  /** Display-only commands from just/react-native/mod.just; <...> denotes required local arguments.
   * An absent platform entry means there is no maintained recipe, not an inferred command.
   */
  readonly recipes: Readonly<Partial<Record<TestPlatform, string>>>;
  readonly coverageNote: string;
}

export const TEST_CATEGORIES: readonly TestCategory[] = [
  { id: 'full-suite', title: 'Recommended full validation', description: 'The maintained single-launch full suite: ten RF flows plus ten recovery cycles.' },
  { id: 'diagnostics', title: 'Capability diagnostics', description: 'Interactive diagnostics and focused RF gates; none alone is full-suite evidence.' },
  { id: 'lifecycle', title: 'Lifecycle / fault recovery', description: 'Cancellation, watchdog, passive loss, teardown, recovery and explicit opt-in mutations.' },
  { id: 'multi-peripheral', title: 'Multi-Peripheral', description: 'Two-DK isolation, continuous pressure and independent peer fault recovery.' },
  { id: 'abi-runtime', title: 'Host-only ABI runtime', description: 'RN / Nitro transport and runtime lifecycle gates without a Peripheral or BLE RF.' },
];

// Static metadata only. Keep mode routing aligned with App.tsx, not with recipe-name prefixes.
export const TEST_SCENARIOS: readonly TestScenario[] = [
  {
    id: 'rf-ccc-enable-reject', category: 'diagnostics', title: 'CCC enable REJECT',
    description: 'Reject one held CCC enable, recover with a fresh subscription and prove post-disable silence.',
    runtimeModes: ['rf-ccc-enable-reject'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      android: 'pixi run just react-native host-android-rf-ccc-enable-reject <device> <fixture_contract> <fixture_profile>',
      ios: 'pixi run just react-native host-ios-device-rf-ccc-enable-reject <udid> <team> <fixture_contract> <fixture_profile>',
    },
    coverageNote: 'Opt-in host launch only, flow 11; not a full-suite flow. Seven control barriers, ATT error 14, independent FFF1 reads 00, fresh FFF2 Notification ef and at least 1000ms post-disable silence. Host tooling owns physical control and final verdict.',
  },
  {
    id: 'rf-write-reject', category: 'diagnostics', title: 'Response-write REJECT',
    description: 'Reject one held response-write, prove unchanged data and recover on the same route.',
    runtimeModes: ['rf-write-reject'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      android: 'pixi run just react-native host-android-rf-write-reject <device> <fixture_contract> <fixture_profile>',
      ios: 'pixi run just react-native host-ios-device-rf-write-reject <udid> <team> <fixture_contract> <fixture_profile>',
    },
    coverageNote: 'Opt-in host launch only: live ARMED / HELD / REJECT and recovery-done barriers; ATT error 14, unchanged 00 and same-route write/read 22. Not part of full-suite. Host owns BTP, physical cleanup and the final verdict; recipes require the paired host tooling.',
  },
  {
    id: 'full', category: 'full-suite', title: 'RF Lab full suite',
    description: 'One App / JS boot / fixture session: ten real RF flows, then ten recovery cycles.',
    runtimeModes: ['rf-lab-suite'], fullSuite: true,
    supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-lab-suite <udid> <team> <fixture_contract> <fixture_profile>',
      android: 'pixi run just react-native host-android-rf-lab-suite <device> <fixture_contract> <fixture_profile>',
    },
    coverageNote: '10 flows: scan, connect, GATT, I/O, RSSI, Notification, Indication, passive recovery, cancellation and watchdog; then RecoveryTen (10 cycles). Requires rfRecoveryRunId and the matching fixture controls. Excludes multi-DK, engine reload, phone radio and permission mutations; host verification remains authoritative.',
  },
  {
    id: 'rf-lab', category: 'diagnostics', title: 'Interactive RF diagnostics',
    description: 'Open the visible single-Peripheral lab and explicitly select and run a diagnostic.',
    runtimeModes: ['rf-lab'], supportedPlatforms: ['ios', 'android'], executionKind: 'interactive', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-lab <udid> <team> <service_uuid>',
      android: 'pixi run just react-native host-android-rf-lab <device> <service_uuid>',
    },
    coverageNote: 'The launch recipe neither starts the fixture nor waits for a gate verdict. Requires an already running matching fixture. This is not the full suite and does not run RecoveryTen or the integrated watchdog automatically.',
  },
  {
    id: 'flow-suite', category: 'diagnostics', title: 'Flow-only RF suite (legacy)',
    description: 'The rf-lab-suite route without a recovery run ID; may select a single flow.',
    runtimeModes: ['rf-lab-suite'], fullSuite: false,
    supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK', recipes: {},
    coverageNote: 'Without rfRecoveryRunId, App runs only runRfLabSuite: ten flows by default or one rfLabSuiteFlow. No ten-cycle recovery phase, so never label this full. No standalone maintained Just recipe; the similarly named rf-lab-suite recipes now launch full validation.',
  },
  {
    id: 'rf', category: 'diagnostics', title: 'Scan',
    description: 'Initialize, observe filtered advertisements, stop scanning and check silence.',
    runtimeModes: ['rf'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-scan <udid> <team>',
      android: 'pixi run just react-native host-android-rf <device> rf',
    },
    coverageNote: 'Scan start/stop ordering and post-stop silence; not connection or GATT evidence.',
  },
  {
    id: 'rf-connect', category: 'diagnostics', title: 'Connection lifecycle',
    description: 'Scan, connect, logically disconnect and check the retired route.',
    runtimeModes: ['rf-connect'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-connect <udid> <team>',
      android: 'pixi run just react-native host-android-rf <device> rf-connect',
    },
    coverageNote: 'Connecting / Connected and explicit disconnect ordering plus silence; not passive fault recovery.',
  },
  {
    id: 'rf-gatt', category: 'diagnostics', title: 'GATT discovery',
    description: 'Discover and validate the aggregate service, characteristic and descriptor database.',
    runtimeModes: ['rf-gatt'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-gatt <udid> <team>',
      android: 'pixi run just react-native host-android-rf <device> rf-gatt',
    },
    coverageNote: 'Includes connection and fixture database / CCCD validation; discovery alone is not characteristic I/O evidence.',
  },
  {
    id: 'rf-io', category: 'diagnostics', title: 'Characteristic I/O',
    description: 'Read, write with response, write without response and verify exact readback.',
    runtimeModes: ['rf-io'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-io <udid> <team>',
      android: 'pixi run just react-native host-android-rf <device> rf-io',
    },
    coverageNote: 'Includes scan / connect / GATT prerequisites. A no-response terminal is local submission progress, not remote application acknowledgement; readback is separate evidence.',
  },
  {
    id: 'rf-rssi', category: 'diagnostics', title: 'Connected RSSI',
    description: 'Read real connected-peripheral RSSI and correlate its completion.',
    runtimeModes: ['rf-rssi'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-rssi <udid> <team>',
      android: 'pixi run just react-native host-android-rf <device> rf-rssi',
    },
    coverageNote: 'Connected RSSI, not advertisement RSSI or a synthetic fixture value; a separate gate from GATT I/O.',
  },
  {
    id: 'rf-notification', category: 'diagnostics', title: 'Notification',
    description: 'Enable PlatformDefault updates, receive the exact payload, disable and check silence.',
    runtimeModes: ['rf-notification'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-notification <udid> <team>',
      android: 'pixi run just react-native host-android-rf <device> rf-notification',
    },
    coverageNote: 'Requires actual Notification mode and correct event / terminal ordering. Does not prove strict Indication.',
  },
  {
    id: 'rf-indication', category: 'diagnostics', title: 'Strict Indication',
    description: 'Check mode rejection, then enable, receive and disable pure-Indication updates.',
    runtimeModes: ['rf-indication'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-indication <udid> <team>',
      android: 'pixi run just react-native host-android-rf <device> rf-indication',
    },
    coverageNote: 'Rejects strict Indication on the dual-property target under the current policy, then validates actual Indication on the pure-Indication target and post-disable silence.',
  },
  {
    id: 'rf-ccc-disable-reject', category: 'diagnostics', title: 'CCC disable REJECT',
    description: 'Reject a held CCC disable, retain the original subscription, then retry disable and prove silence.',
    runtimeModes: ['rf-ccc-disable-reject'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      android: 'pixi run just react-native host-android-rf-ccc-disable-reject <device> <fixture_contract> <fixture_profile>',
      ios: 'pixi run just react-native host-ios-device-rf-ccc-disable-reject <udid> <team> <fixture_contract> <fixture_profile>',
    },
    coverageNote: 'Standalone flow 12, not a full-suite index. Host controls CCC receipts and the final verdict; App checks require ef/e1 on the original subscription without re-enable and bounded post-disable silence.',
  },
  {
    id: 'rf-cancellation', category: 'lifecycle', title: 'Queued-operation cancellation',
    description: 'Reject active cancellation, cancel a queued read and prove FIFO recovery.',
    runtimeModes: ['rf-cancellation'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-cancellation <udid> <team>',
      android: 'pixi run just react-native host-android-rf-cancellation <device>',
    },
    coverageNote: 'Checks queued cancelled terminal, active completion, subsequent I/O, late silence and unknown / completed targets. Cancellation acceptance is not completion; full-suite cancellation additionally uses attested fixture controls.',
  },
  {
    id: 'rf-watchdog', category: 'lifecycle', title: 'Native GATT watchdog',
    description: 'Hold a real ATT read, observe native timeout teardown and recover on a fresh route.',
    runtimeModes: ['rf-watchdog'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-watchdog <udid> <team> <fixture_contract> <fixture_profile>',
      android: 'pixi run just react-native host-android-rf-watchdog <device> <fixture_contract> <fixture_profile>',
    },
    coverageNote: 'Requires live attested ATT-hold-capable firmware, not an ordinary basic profile or a JS timeout. Also included as flow 10 of the full suite; does not prove phone radio-loss behavior.',
  },
  {
    id: 'rf-passive-disconnect', category: 'lifecycle', title: 'Passive disconnect / reconnect',
    description: 'Lose a subscribed fixture, reject stale-route events and reconnect.',
    runtimeModes: ['rf-passive-disconnect'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-passive-disconnect <udid> <team>',
      android: 'pixi run just react-native host-android-rf <device> rf-passive-disconnect',
    },
    coverageNote: 'Fixture-driven loss with an active Notification route, passive commandId=none event and recovery connection. This is not phone adapter loss during an active read/write/CCCD operation.',
  },
  {
    id: 'rf-recovery', category: 'lifecycle', title: 'Three-cycle passive recovery',
    description: 'Keep one Probe / listener across three fixture loss and reconnect cycles.',
    runtimeModes: ['rf-recovery'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-recovery <udid> <team>',
      android: 'pixi run just react-native host-android-rf <device> rf-recovery',
    },
    coverageNote: 'Repeated scan / connect / GATT / Notification / passive loss / reconnect with old-route silence; distinct from ten fresh Probe/Hub lifecycles.',
  },
  {
    id: 'rf-recovery-ten', category: 'lifecycle', title: 'RecoveryTen',
    description: 'Ten fresh Probe / Hub lifecycles within one App and JS runtime.',
    runtimeModes: ['rf-recovery-ten'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-recovery-ten <udid> <team> <fixture_contract> <fixture_profile>',
      android: 'pixi run just react-native host-android-rf-recovery-ten <device> <fixture_contract> <fixture_profile>',
    },
    coverageNote: 'Live RF, host readiness barriers, passive loss / reconnect and exact native resource accounting through close. Standalone recovery phase only, not the ten-flow full suite.',
  },
  {
    id: 'rf-reload', category: 'lifecycle', title: 'Subscribed RF engine reload',
    description: 'Invalidate the real RN engine while a Notification subscription is active.',
    runtimeModes: ['rf-reload'], supportedPlatforms: ['ios'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: { ios: 'pixi run just react-native host-ios-device-rf-reload <udid> <team>' },
    coverageNote: 'Fixture observes host disconnect during engine teardown. No maintained Android recipe for this route; Android runtime-reload is a different non-RF gate.',
  },
  {
    id: 'rf-pending-read-reload', category: 'lifecycle', title: 'Pending-read engine reload',
    description: 'Reload two RN engines in the same App / PID while a real ATT read is held.',
    runtimeModes: ['rf-pending-read-reload'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-pending-read-reload <udid> <team> <fixture_contract> <fixture_profile>',
      android: 'pixi run just react-native host-android-pending-read-reload <device> <fixture_contract> <fixture_profile>',
    },
    coverageNote: 'Requires attested multifunction firmware, native engine-invalidation close ownership, cleanup barriers and fresh-generation recovery. Not a process restart or a JS-only remount.',
  },
  {
    id: 'rf-adapter-transition', category: 'lifecycle', title: 'Adapter OFF → ON',
    description: 'Start authorized with the Android radio OFF, close, turn ON and run fresh I/O.',
    runtimeModes: ['rf-adapter-transition'], adapterMode: 'startup-off',
    supportedPlatforms: ['android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: { android: 'pixi run just react-native host-android-rf-adapter-transition <device> <fixture_contract> <fixture_profile>' },
    coverageNote: 'Opt-in radio mutation: PoweredOff initialization, adapter_unavailable scan terminal, exact-zero close, then a fresh Probe rf-io gate after ON. Not permission transition or radio loss during an active operation. iOS startup-off candidate foundation only; host transport / provider / evidence integration pending, not an available maintained iOS live gate.',
  },
  {
    id: 'permission-authorization', category: 'lifecycle', title: 'iOS authorization (non-RF)',
    description: 'XCUITest: authorized baseline, denied startup, then fresh authorized initialization and close.',
    runtimeModes: ['permission-authorization'], supportedPlatforms: ['ios'], executionKind: 'host', fixtureRequirement: 'none',
    recipes: { ios: 'pixi run just react-native host-ios-device-permission-test <udid> <team> <xcresult-path>' },
    coverageNote: 'Opt-in app-specific Bluetooth authorization reset and consent through public XCUITest APIs. Requires initial Authorized + PoweredOn, restores authorization before finishing, and checks 17-zero close on fresh audit Probes only. Consent-staging process termination is not close/zero evidence. No Peripheral, authorized scan, GATT/RF recovery, active revoke, OS death proof or radio mutation. Android permission RF remains a separate host gate.',
  },
  {
    id: 'permission-transition', category: 'lifecycle', title: 'Android permission transition',
    description: 'Denied start, permission revocation with active / queued reads, then fresh-App I/O after permission restoration.',
    runtimeModes: ['rf-permission-transition'], supportedPlatforms: ['android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: { android: 'pixi run just react-native host-android-rf-permission-transition <device> <fixture_contract> <fixture_profile>' },
    coverageNote: 'Opt-in Android host gate with denied, active and restored phases in independent App processes. Denied start checks scan Permission rejection; CONNECT denial and OS process death require host OS-state evidence. Active revocation requires an attested held read plus a queued read; old terminals, close and resource-zero state are unobservable after process death. Restored permissions permit fresh-App rf-io, not same-Hub recovery or in-flight write/CCCD coverage. No maintained iOS RF permission gate; iOS authorization-only checks have a separate entry. Catalog availability and current-phase App checks are not a full host-run verdict.',
  },
  {
    id: 'active-operation-radio-loss', category: 'lifecycle', title: 'Active-operation radio loss',
    description: 'Turn the Android phone radio OFF with a held ATT read and a queued read, then validate fresh-Probe I/O after ON.',
    runtimeModes: ['rf-adapter-transition'], adapterMode: 'active-loss',
    supportedPlatforms: ['android'], executionKind: 'host', fixtureRequirement: 'single-DK',
    recipes: { android: 'pixi run just react-native host-android-rf-adapter-transition <device> <fixture_contract> <fixture_profile> active-loss' },
    coverageNote: 'Catalog availability is not a run verdict; check the current session and host evidence. Requires an attested held read plus a queued read before phone OFF, settlement of both read terminals and passive loss on the old route, then teardown / close and fresh Probe rf-io after ON. This is not same-Hub recovery, write/CCCD in-flight coverage, startup-off or permission transition. Android only.',
  },
  {
    id: 'rf-multi', category: 'multi-peripheral', title: 'Two-Peripheral isolation',
    description: 'Use two profile-isolated DKs with independent connection, GATT and value routes.',
    runtimeModes: ['rf-multi'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'two-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-multi <udid> <team>',
      android: 'pixi run just react-native host-android-rf-multi <device>',
    },
    coverageNote: 'Requires explicit local DK/profile mapping. Recipes default to Notification and one outer cycle; optional subscription_mode selects Indication. Repeated outer cycles are not a continuous same-Probe soak.',
  },
  {
    id: 'rf-multi-soak', category: 'multi-peripheral', title: 'Continuous two-Peripheral soak',
    description: 'Keep one RN process / Probe and both fixtures alive across inner ledgers.',
    runtimeModes: ['rf-multi-soak'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'two-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-multi-soak <udid> <team>',
      android: 'pixi run just react-native host-android-rf-multi-soak <device>',
    },
    coverageNote: 'Defaults to ten inner cycles and Notification; optional subscription_mode / cycles parameters. Separate from outer process repetition and from single-DK RecoveryTen.',
  },
  {
    id: 'rf-multi-fault', category: 'multi-peripheral', title: 'Independent peer fault / recovery',
    description: 'Stop only DK-A, require DK-B survival, then recover DK-A on fresh routes.',
    runtimeModes: ['rf-multi-fault'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'two-DK',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-rf-multi-fault <udid> <team>',
      android: 'pixi run just react-native host-android-rf-multi-fault <device>',
    },
    coverageNote: 'Destructive fixture-side fault with independent peer survival and fresh connection / GATT recovery. Does not mutate the phone radio or permissions.',
  },
  {
    id: 'basic', category: 'abi-runtime', title: 'ABI transport smoke',
    description: 'Exercise ArrayBuffer / typed error transport, command completion and close.',
    runtimeModes: ['basic'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'none',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-runtime <udid> <team>',
      android: 'pixi run just react-native host-android-runtime <device>',
    },
    coverageNote: 'RN / Nitro transport with max-u64 correlation, listener removal and close. Host-only means no Peripheral / RF, not no phone; iOS also has host-ios-runtime <simulator_udid>. Not production BLE RF evidence.',
  },
  {
    id: 'stress', category: 'abi-runtime', title: 'ABI lifecycle stress',
    description: 'Repeat create / close, remove listeners and reject late or post-close delivery.',
    runtimeModes: ['stress'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'none',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-lifecycle-stress <udid> <team>',
      android: 'pixi run just react-native host-android-lifecycle-stress <device>',
    },
    coverageNote: 'Eight host-only lifecycle cycles plus close during a completion callback; no live BLE connection or RF recovery is exercised.',
  },
  {
    id: 'reload', category: 'abi-runtime', title: 'ABI engine reload',
    description: 'Exercise real RN runtime invalidation and fresh-runtime transport recovery.',
    runtimeModes: ['reload'], supportedPlatforms: ['ios', 'android'], executionKind: 'host', fixtureRequirement: 'none',
    recipes: {
      ios: 'pixi run just react-native host-ios-device-reload <udid> <team>',
      android: 'pixi run just react-native host-android-runtime-reload <device>',
    },
    coverageNote: 'Host-only engine reload, distinct from Activity/process restart, subscribed RF reload and physically pending-read teardown.',
  },
];

/** Pass fullSuite: rfRecoveryRunId !== undefined, matching App's route selection.
 * Pass adapterMode from rfAdapterMode; omission selects startup-off, never active-loss.
 * This identifies metadata, not a validated run or verdict. rf-workbench is navigation only.
 */
export function findScenario(
  mode: RuntimeMode | undefined,
  { fullSuite = false, adapterMode = 'startup-off' }: { fullSuite?: boolean; adapterMode?: AdapterMode } = {},
): TestScenario | undefined {
  return TEST_SCENARIOS.find((scenario) => mode !== undefined && scenario.runtimeModes.includes(mode) &&
    (scenario.fullSuite === undefined || scenario.fullSuite === fullSuite) &&
    (scenario.adapterMode === undefined || scenario.adapterMode === adapterMode));
}
