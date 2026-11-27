import mockSafeAreaContext from 'react-native-safe-area-context/jest/mock';

jest.mock('react-native-safe-area-context', () => mockSafeAreaContext);

// The package barrel imports the Nitro bridge at module load, and nitro registers a TurboModule
// on import. Jest has no native binary, so importing the real barrel (which the App now does for
// its pure-JS `beginPending` layer) would throw before a single assertion runs.
//
// Only `react-native-nitro-modules` is stubbed, never `react-native`: the component suites need
// the real renderer. The package source resolves the bridge from its own node_modules tree, which
// is why both resolutions are registered.
const { dirname } = require('node:path');
const packageSourceDirectory = dirname(require.resolve('@blengine/react-native-bleng'));
const consumerDirectory = __dirname;
for (const directory of [consumerDirectory, packageSourceDirectory]) {
  jest.doMock(require.resolve('react-native-nitro-modules', { paths: [directory] }), () => ({
    NitroModules: { createHybridObject: jest.fn() },
  }));
}
