import React from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';

/** Shared RF Lab presentation; scenario controllers still own their lifecycle and verdict. */
export function RfLabRunScreen({ title, description, status, children }: {
  title: string;
  description: string;
  status?: string;
  children: React.ReactNode;
}): React.JSX.Element {
  return (
    <View style={styles.safeArea} testID="rf-lab-suite-root" collapsable={false}>
      <ScrollView contentContainerStyle={styles.root} contentInsetAdjustmentBehavior="automatic"
        removeClippedSubviews={false} testID="rf-lab-full-scroll">
        <View style={styles.header} collapsable={false}>
          <Text style={styles.runTitle}>{title}</Text>
          <Text style={styles.subtitle}>{description}</Text>
          {status !== undefined && <Text style={styles.statusValue} testID="rf-lab-suite-runtime-status">{status}</Text>}
        </View>
        {children}
      </ScrollView>
    </View>
  );
}

export const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: '#f5f7fa' },
  root: { padding: 20, paddingBottom: 32, gap: 16 },
  navigation: { paddingHorizontal: 20, paddingBottom: 10, gap: 8 },
  workbenchHeader: { paddingHorizontal: 20, paddingTop: 10, paddingBottom: 12, gap: 4 },
  workbenchTitle: { color: '#122033', fontSize: 24, fontWeight: '800' },
  runTitle: { color: '#122033', fontSize: 22, fontWeight: '800' },
  tab: { flex: 1, alignItems: 'center', borderRadius: 8, paddingVertical: 10 },
  sectionCaption: { color: '#526173', fontSize: 11, fontWeight: '700', letterSpacing: 1 },
  categoryList: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  category: { borderWidth: 1, borderColor: '#d7e0e9', borderRadius: 8, paddingHorizontal: 12, paddingVertical: 10 },
  runStrip: { marginHorizontal: 20, marginBottom: 8, padding: 12, backgroundColor: '#edf2f7', borderRadius: 8, gap: 4 },
  pane: { flex: 1 },
  hiddenPane: { display: 'none' },
  header: { gap: 16 },
  headerCopy: { gap: 6 },
  eyebrow: { color: '#526173', fontSize: 12, fontWeight: '700', letterSpacing: 1.2 },
  title: { color: '#122033', fontSize: 30, fontWeight: '800' },
  subtitle: { color: '#526173', fontSize: 15, lineHeight: 22 },
  statusCard: { borderLeftWidth: 3, borderLeftColor: '#218739', paddingLeft: 12, gap: 4 },
  statusCaption: { color: '#526173', fontSize: 12, fontWeight: '600' },
  statusValue: { color: '#122033', fontSize: 16, fontWeight: '700' },
  actions: { flexDirection: 'row', flexWrap: 'wrap', gap: 10 },
  button: { borderRadius: 8, paddingHorizontal: 14, paddingVertical: 11 },
  primaryButton: { backgroundColor: '#1769aa' },
  secondaryButton: { backgroundColor: '#e7edf3' },
  warningButton: { backgroundColor: '#9a6700' },
  primaryButtonText: { color: '#ffffff', fontSize: 14, fontWeight: '700' },
  secondaryButtonText: { color: '#17324d', fontSize: 14, fontWeight: '700' },
  card: { backgroundColor: '#ffffff', borderRadius: 10, padding: 16, gap: 8 },
  labMode: { borderWidth: 1, borderColor: '#e4e9ef', borderRadius: 8, padding: 12, gap: 3 },
  labModeSelected: { borderColor: '#1769aa', backgroundColor: '#edf6ff' },
  labModeLabel: { color: '#243447', fontSize: 14, fontWeight: '700' },
  cardHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 },
  cardTitle: { color: '#122033', fontSize: 18, fontWeight: '800' },
  badge: { color: '#1769aa', fontSize: 14, fontWeight: '800' },
  commandInput: { borderWidth: 1, borderColor: '#c9d3df', borderRadius: 8, color: '#122033', fontFamily: 'Menlo', fontSize: 14, paddingHorizontal: 10, paddingVertical: 8 },
  stepRow: { flexDirection: 'row', alignItems: 'center', gap: 10, borderBottomWidth: 1, borderBottomColor: '#e4e9ef', paddingVertical: 10 },
  stepIndicator: { color: '#8a98a8', fontSize: 11, fontWeight: '800', width: 32 },
  stepCopy: { flex: 1, gap: 2 },
  stepLabel: { color: '#243447', fontSize: 14, fontWeight: '600' },
  stepDetail: { color: '#718096', fontSize: 12 },
  successText: { color: '#218739' },
  errorText: { color: '#b42318' },
  eventLine: { color: '#526173', fontFamily: 'Menlo', fontSize: 12, paddingVertical: 4 },
  emptyText: { color: '#718096', fontSize: 14 },
  footer: { color: '#718096', fontSize: 12, lineHeight: 18, paddingBottom: 20 },
});
