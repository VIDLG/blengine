import React, { useState } from 'react';
import { Platform, Pressable, ScrollView, StatusBar, Text, View } from 'react-native';
import type { RuntimeMode } from './rf_contract';
import { findScenario, TEST_CATEGORIES, TEST_SCENARIOS, type TestCategoryId, type TestScenario } from './rf_lab_catalog';
import { RunSessionProvider, useRunPresentation } from './rf_lab_session';
import { styles } from './rf_lab_screen';

type Page = 'home' | 'current' | 'evidence';
const STATE_LABELS = { ready: 'Ready', running: 'Running', passed: 'Passed', failed: 'Failed' };
const FIXTURES = { none: 'No Peripheral', 'single-DK': 'One attested DK', 'two-DK': 'Two configured DKs' };

/** Catalog → scenario setup is browsing. Run → evidence describes the one mounted session. */
export function RfLabWorkbench({ mode, runId, serviceUuid, fullSuite = false, adapterMode, children, interactive }: {
  mode?: RuntimeMode; runId?: string; serviceUuid?: string; fullSuite?: boolean; adapterMode?: 'startup-off' | 'active-loss';
  children: React.ReactNode; interactive: React.ReactNode;
}): React.JSX.Element {
  const hostSelected = mode !== undefined && mode !== 'rf-workbench';
  const [interactiveOpened, setInteractiveOpened] = useState(false);
  const [page, setPage] = useState<Page>(hostSelected ? 'current' : 'home');
  const [category, setCategory] = useState<TestCategoryId>('full-suite');
  const [selectedId, setSelectedId] = useState('full');
  const [setupOpen, setSetupOpen] = useState(false);
  const hasCurrent = hostSelected || interactiveOpened;
  const currentMode = hostSelected ? mode : interactiveOpened ? 'rf-lab' : undefined;
  const current = findScenario(currentMode, { fullSuite, adapterMode });
  const selected = TEST_SCENARIOS.find((scenario) => scenario.id === selectedId)!;
  const platform = Platform.OS === 'ios' ? 'ios' : 'android';
  const supported = selected.supportedPlatforms.includes(platform);
  const canOpenInteractive = !hostSelected && Boolean(serviceUuid?.trim());
  const { summary, value } = useRunPresentation(currentMode, fullSuite);
  const stateStyle = summary.state === 'failed' ? styles.errorText : summary.state === 'passed' ? styles.successText : styles.statusValue;
  const title = current?.title ?? (hasCurrent ? 'Unrecognized host scenario' : 'No test started');
  const selectScenario = (scenario: TestScenario): void => { setSelectedId(scenario.id); setSetupOpen(true); };
  const availability = selected.executionKind === 'planned' ? 'Not implemented' : !supported ? 'Not available on this platform'
    : selected.executionKind === 'interactive' ? 'Interactive diagnostic' : 'Host-controlled';
  const tabs: readonly [Page, string, string][] = [
    ['home', 'Tests', 'rf-workbench-home'], ['current', 'Current run', 'rf-workbench-current'], ['evidence', 'Evidence', 'rf-workbench-evidence'],
  ];
  return (
    <RunSessionProvider value={value}>
      <View style={[styles.safeArea, { paddingTop: Platform.OS === 'android' ? (StatusBar.currentHeight ?? 0) : 0 }]}
        testID="rf-workbench-root" collapsable={false}>
        <StatusBar barStyle="dark-content" />
        <View style={styles.workbenchHeader}>
          <Text style={styles.eyebrow}>BLEHUB / RN NITRO</Text>
          <Text style={styles.workbenchTitle}>RF Lab</Text>
        </View>
        <View style={[styles.actions, styles.navigation]}>
          {tabs.map(([target, label, id]) => (
            <Pressable key={target} testID={id} accessibilityRole="button" disabled={target === 'current' && !hasCurrent}
              accessibilityState={{ selected: page === target, disabled: target === 'current' && !hasCurrent }}
              style={[styles.tab, page === target ? styles.primaryButton : styles.secondaryButton]}
              onPress={() => setPage(target)}>
              <Text style={page === target ? styles.primaryButtonText : styles.secondaryButtonText}>{label}</Text>
            </Pressable>
          ))}
        </View>
        <View testID="rf-workbench-overview" style={page === 'home' ? styles.pane : styles.hiddenPane}
          accessibilityElementsHidden={page !== 'home'} importantForAccessibility={page === 'home' ? 'auto' : 'no-hide-descendants'}>
          <ScrollView contentContainerStyle={styles.root} removeClippedSubviews={false}>
            <View style={styles.card}>
              <Text style={styles.sectionCaption}>CURRENT SESSION</Text>
              <Text style={styles.cardTitle} testID="rf-workbench-session-mode">{title}</Text>
              <Text style={stateStyle} testID="rf-workbench-summary">{hasCurrent ? `App checks · ${STATE_LABELS[summary.state]}` : 'Choose a test to view its setup'}</Text>
              <Text style={styles.stepDetail}>{hasCurrent ? summary.phase : 'Browsing does not start BLE or change phone settings.'}</Text>
              <Pressable accessibilityRole="button" disabled={!hasCurrent} accessibilityState={{ disabled: !hasCurrent }}
                style={[styles.button, styles.secondaryButton]} onPress={() => setPage('current')}>
                <Text style={styles.secondaryButtonText}>View progress & result</Text>
              </Pressable>
            </View>
            <Text style={styles.cardTitle}>Test catalog</Text>
            <View style={styles.categoryList}>
              {TEST_CATEGORIES.map((item) => (
                <Pressable key={item.id} testID={`rf-workbench-category-${item.id}`} accessibilityRole="button"
                  accessibilityState={{ selected: category === item.id }}
                  style={[styles.category, category === item.id && styles.labModeSelected]}
                  onPress={() => { setCategory(item.id); setSetupOpen(false); }}>
                  <Text style={styles.labModeLabel}>{item.title}</Text>
                </Pressable>
              ))}
            </View>
            {TEST_SCENARIOS.filter((scenario) => scenario.category === category && scenario.id !== 'flow-suite').map((scenario) => (
              <Pressable key={scenario.id} testID={`rf-workbench-scenario-${scenario.id}`} accessibilityRole="button"
                style={styles.card} onPress={() => selectScenario(scenario)}>
                <View style={styles.cardHeader}>
                  <Text style={[styles.cardTitle, styles.stepCopy]}>{scenario.title}</Text>
                  <Text style={styles.badge}>{scenario.executionKind === 'planned' ? 'Planned' : scenario.executionKind === 'interactive' ? 'Interactive' : 'Host'}</Text>
                </View>
                <Text style={styles.subtitle}>{scenario.description}</Text>
                <Text style={styles.stepDetail}>{FIXTURES[scenario.fixtureRequirement]} · {scenario.supportedPlatforms.join(' / ') || 'Not implemented'} · View setup →</Text>
              </Pressable>
            ))}
            <View testID="rf-workbench-setup" style={setupOpen ? styles.card : styles.hiddenPane}
              accessibilityElementsHidden={!setupOpen} importantForAccessibility={setupOpen ? 'auto' : 'no-hide-descendants'}>
              <Text style={styles.sectionCaption}>SCENARIO SETUP</Text>
              <Text style={styles.cardTitle}>{selected.title}</Text>
              <Text style={styles.badge}>{availability}</Text>
              <Text style={styles.subtitle}>{selected.coverageNote}</Text>
              <Text style={styles.stepDetail}>{FIXTURES[selected.fixtureRequirement]}. This selection does not launch a test.</Text>
              <Text selectable style={styles.eventLine}>{selected.recipes[platform] ?? 'No maintained host recipe on this platform.'}</Text>
              <Pressable testID="rf-workbench-view-scenario" accessibilityRole="button"
                disabled={!hasCurrent || selected.id !== current?.id} accessibilityState={{ disabled: !hasCurrent || selected.id !== current?.id }}
                style={[styles.button, styles.secondaryButton]} onPress={() => setPage('current')}>
                <Text style={styles.secondaryButtonText}>View matching session</Text>
              </Pressable>
            </View>
            <Pressable testID="rf-workbench-open-interactive" accessibilityRole="button" disabled={!canOpenInteractive}
              accessibilityState={{ disabled: !canOpenInteractive }} style={[styles.button, styles.secondaryButton]}
              onPress={() => { setInteractiveOpened(true); setPage('current'); }}>
              <Text style={styles.secondaryButtonText}>Open interactive diagnostics</Text>
            </Pressable>
            <Text style={styles.footer}>{hostSelected ? 'This session is host-controlled. Other scenarios require a new explicit host launch.'
              : serviceUuid?.trim() ? 'Interactive diagnostics use the supplied fixture. BLE starts only when you press Run.'
              : 'To enable interactive diagnostics, launch with a service UUID and prepare the matching fixture on the host.'}</Text>
          </ScrollView>
        </View>
        <View testID="rf-workbench-active" collapsable={false} style={page === 'current' ? styles.pane : styles.hiddenPane}
          accessibilityElementsHidden={page !== 'current'} importantForAccessibility={page === 'current' ? 'auto' : 'no-hide-descendants'}>
          <View style={styles.runStrip} accessibilityLiveRegion="polite">
            <Text style={stateStyle} testID="rf-workbench-run-state">App checks · {STATE_LABELS[summary.state]}</Text>
            <Text style={styles.stepDetail} testID="rf-workbench-run-phase">{summary.phase}</Text>
            <Text style={styles.stepDetail}>{summary.total ? `${summary.completed}/${summary.total} stages` : 'Scenario-specific progress below'}</Text>
          </View>
          {hostSelected ? children : interactiveOpened ? interactive : null}
        </View>
        <View testID="rf-workbench-evidence-pane" style={page === 'evidence' ? styles.pane : styles.hiddenPane}
          accessibilityElementsHidden={page !== 'evidence'} importantForAccessibility={page === 'evidence' ? 'auto' : 'no-hide-descendants'}>
          <ScrollView contentContainerStyle={styles.root}>
            <Text style={styles.cardTitle}>Evidence & execution context</Text>
            <View style={styles.card}>
              <Text style={styles.cardTitle}>{title}</Text>
              <Text style={stateStyle}>App checks · {hasCurrent ? STATE_LABELS[summary.state] : 'Not run'}</Text>
              <Text style={styles.subtitle}>Host gate · Verify on the host</Text>
              <Text style={styles.stepDetail}>The App does not receive the host's final physical, cleanup or bundle verdict. App Passed is not host-gate Passed.</Text>
              <Text style={styles.errorText} testID="rf-workbench-failure">{summary.failure || 'No App failure reported.'}</Text>
            </View>
            <View style={styles.card}>
              <Text style={styles.cardTitle}>Technical identity</Text>
              <Text selectable style={styles.eventLine}>Mode: {currentMode ?? 'none'}</Text>
              <Text selectable style={styles.eventLine}>Session: {runId || 'not supplied'}</Text>
              <Text selectable style={styles.eventLine}>Service: {serviceUuid || 'not configured'}</Text>
              <Text style={styles.stepDetail}>One in-memory session. Navigation keeps it mounted; engine reload or process restart does not preserve a history.</Text>
            </View>
            <View style={styles.card}>
              <Text style={styles.cardTitle}>Coverage boundary</Text>
              <Text style={styles.subtitle}>{current?.coverageNote ?? 'No execution scenario selected.'}</Text>
              <Text selectable style={styles.eventLine}>{current?.recipes[platform] ?? 'No recipe selected.'}</Text>
              <Text style={styles.stepDetail}>Raw events stay in host logs. Offline bundle verification checks integrity, not a new RF execution.</Text>
            </View>
          </ScrollView>
        </View>
      </View>
    </RunSessionProvider>
  );
}
