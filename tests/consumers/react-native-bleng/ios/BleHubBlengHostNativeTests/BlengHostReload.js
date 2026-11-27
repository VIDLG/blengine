import {createBlengCentral} from '@blehub/react-native-bleng';
import {createElement, useSyncExternalStore} from 'react';
import {AppRegistry, Text, TurboModuleRegistry, View} from 'react-native';

// Construction must materialize/bind the real lifecycle module before evidence claims its slot.
const owner = createBlengCentral();
globalThis.__blengHostReloadOwner = owner;
const evidence = TurboModuleRegistry.getEnforcing('BlengHostReloadEvidence');
if (typeof evidence.report !== 'function') {
  throw new Error(
    'BlengHostReloadEvidence.report is unavailable; the native TurboModule must register report(stage, error).',
  );
}

// One status store per actual JS engine. A late surface mount reads the settled snapshot;
// remounting a component never creates another owner or starts native work again.
let status = 'pending';
const listeners = new Set();
function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
function getStatus() {
  return status;
}
function settle(nextStatus, stage, error) {
  status = nextStatus;
  listeners.forEach(listener => listener());
  evidence.report(stage, error);
}
function BlengHostReload() {
  const currentStatus = useSyncExternalStore(subscribe, getStatus);
  return createElement(
    View,
    {
      accessible: true,
      accessibilityLabel: currentStatus,
      testID: 'bleng-reload-status',
      collapsable: false,
      style: {flex: 1, justifyContent: 'center', alignItems: 'center'},
    },
    createElement(Text, null, currentStatus),
  );
}
AppRegistry.registerComponent('BlengHostReload', () => BlengHostReload);

evidence.report('start-submitted', '');
try {
  owner.initialize().then(
    () => settle('started', 'start-resolved', ''),
    () => settle('failed', 'start-rejected', 'start-failed'),
  );
} catch {
  // Do not export native error descriptions, paths, payloads, or owner identifiers to evidence.
  settle('failed', 'start-rejected', 'start-failed');
}
