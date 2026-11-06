import { findScenario, TEST_SCENARIOS } from './rf_lab_catalog';
import type { RuntimeMode } from './rf_contract';

describe('RF Lab catalog route boundaries', () => {
  it('keeps write REJECT host-only and independent from suite / recovery discriminators', () => {
    const scenario = findScenario('rf-write-reject')!;
    expect(scenario.executionKind).toBe('host');
    expect(scenario.fixtureRequirement).toBe('single-DK');
    expect(scenario.supportedPlatforms).toEqual(['ios', 'android']);
    expect(scenario.recipes.android).toContain('host-android-rf-write-reject');
    expect(scenario.recipes.ios).toContain('host-ios-device-rf-write-reject');
    expect(findScenario('rf-write-reject', { fullSuite: true })).toBe(scenario);
    expect(findScenario('rf-lab-suite', { fullSuite: true })?.id).toBe('full');
  });
  it('requires the recovery-run discriminator before identifying a suite as full', () => {
    expect(findScenario('rf-lab-suite')?.id).toBe('flow-suite');
    expect(findScenario('rf-lab-suite', { fullSuite: false })?.id).toBe('flow-suite');
    expect(findScenario('rf-lab-suite', { fullSuite: true })?.id).toBe('full');
    expect(findScenario('rf-lab')?.executionKind).toBe('interactive');
    expect(findScenario('rf-lab', { fullSuite: true })?.id).toBe('rf-lab');
    expect(findScenario('rf-recovery-ten', { fullSuite: true })?.id).toBe('rf-recovery-ten');
    expect(findScenario(undefined)).toBeUndefined();
    expect(findScenario('rf-workbench', { fullSuite: true })).toBeUndefined();
  });

  it('keeps each remaining executor mode unambiguous instead of matching a broad RF group', () => {
    const modes: readonly RuntimeMode[] = [
      'rf-write-reject', 'basic', 'stress', 'reload', 'rf', 'rf-connect', 'rf-gatt', 'rf-io', 'rf-rssi',
      'rf-notification', 'rf-indication', 'rf-passive-disconnect', 'rf-cancellation',
      'rf-recovery', 'rf-recovery-ten', 'rf-multi', 'permission-authorization',
      'rf-multi-soak', 'rf-multi-fault', 'rf-reload', 'rf-pending-read-reload', 'rf-lab', 'rf-watchdog',
    ];
    for (const mode of modes) {
      expect(TEST_SCENARIOS.filter((scenario) => scenario.runtimeModes.includes(mode))).toHaveLength(1);
      expect(findScenario(mode)?.id).toBe(mode);
    }
  });

  it('strictly distinguishes startup-off and active-loss on the shared adapter runtime route', () => {
    const startup = findScenario('rf-adapter-transition')!;
    expect(startup.id).toBe('rf-adapter-transition');
    expect(findScenario('rf-adapter-transition', { adapterMode: 'startup-off' })).toBe(startup);
    expect(startup.recipes.android).toBe('pixi run just react-native host-android-rf-adapter-transition <device> <fixture_contract> <fixture_profile>');
    const active = findScenario('rf-adapter-transition', { adapterMode: 'active-loss' })!;
    expect(active.id).toBe('active-operation-radio-loss');
    expect(active.executionKind).toBe('host');
    expect(active.supportedPlatforms).toEqual(['android']);
    expect(active.recipes.ios).toBeUndefined();
    expect(active.recipes.android).toBe(`${startup.recipes.android} active-loss`);
    expect(findScenario('rf-adapter-transition', { fullSuite: true, adapterMode: 'active-loss' })).toBe(active);
    // Native launch strings still need fail-closed behavior if they escape the typed boundary.
    expect(findScenario('rf-adapter-transition', { adapterMode: 'unknown' as 'active-loss' })).toBeUndefined();
    expect(findScenario('rf-lab-suite', { fullSuite: true, adapterMode: 'active-loss' })?.id).toBe('full');
  });

  it('routes Android host permission phases separately from radio modes without inventing platform recipes', () => {
    const adapter = findScenario('rf-adapter-transition')!;
    expect(adapter.supportedPlatforms).toEqual(['android']);
    expect(adapter.recipes.ios).toBeUndefined();
    expect(adapter.coverageNote).toContain('iOS startup-off candidate foundation only');
    expect(adapter.coverageNote).toContain('integration pending');
    expect(adapter.coverageNote).toContain('not an available maintained iOS live gate');
    expect(findScenario('rf-reload')?.recipes.android).toBeUndefined();
    expect(findScenario('rf-lab-suite')?.recipes).toEqual({});
    const permission = findScenario('rf-permission-transition')!;
    expect(permission.id).toBe('permission-transition');
    expect(TEST_SCENARIOS.filter((scenario) => scenario.runtimeModes.includes('rf-permission-transition'))).toEqual([permission]);
    expect(permission.executionKind).toBe('host');
    expect(permission.supportedPlatforms).toEqual(['android']);
    expect(permission.fixtureRequirement).toBe('single-DK');
    expect(permission.recipes).toEqual({
      android: 'pixi run just react-native host-android-rf-permission-transition <device> <fixture_contract> <fixture_profile>',
    });
    expect(findScenario('rf-permission-transition', { fullSuite: true, adapterMode: 'active-loss' })).toBe(permission);
    expect(permission.coverageNote).toMatch(/denied, active and restored phases in independent App processes/);
    expect(permission.coverageNote).toMatch(/CONNECT denial and OS process death require host OS-state evidence/);
    expect(permission.coverageNote).toMatch(/old terminals, close and resource-zero state are unobservable/);
    expect(permission.coverageNote).toMatch(/fresh-App rf-io, not same-Hub recovery or in-flight write\/CCCD coverage/);
    expect(permission.coverageNote).toMatch(/No maintained iOS RF permission gate/);
    expect(permission.coverageNote).not.toMatch(/No maintained Just recipe/);
    const ios = findScenario('permission-authorization')!;
    expect(ios.supportedPlatforms).toEqual(['ios']);
    expect(ios.fixtureRequirement).toBe('none');
    expect(ios.recipes.ios).toContain('host-ios-device-permission-test');
    expect(ios.coverageNote).toMatch(/No Peripheral, authorized scan, GATT\/RF recovery/);
  });
});
