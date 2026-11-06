import React from 'react';
import { AppRegistry, Platform, TurboModuleRegistry } from 'react-native';
import App from './src/App';
import { name as appName } from './app.json';

let pendingConfig;
function HostRoot(props) {
  if (Platform.OS !== 'ios' || props.runtimeMode !== 'rf-pending-read-reload') {
    return React.createElement(App, props);
  }
  if (!pendingConfig) {
    // Lazily instantiated ONLY for this explicit test mode. Its installation
    // receives the actual Runtime&, not a generation supplied by JavaScript.
    TurboModuleRegistry.getEnforcing('BleHubPendingReloadLifecycle');
    const config = globalThis.__blehubPendingReloadConfig;
    if (!config || ![1, 2].includes(config.rfPendingReadGeneration)) {
      throw new Error('native pending-reload runtime configuration is missing');
    }
    pendingConfig = Object.freeze({ ...config });
  }
  return React.createElement(App, { ...props, ...pendingConfig });
}

AppRegistry.registerComponent(appName, () => HostRoot);
