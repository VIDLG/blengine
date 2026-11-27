const path = require('node:path');
const { getDefaultConfig, mergeConfig } = require('@react-native/metro-config');

const moduleRoot = path.resolve(__dirname, '../../../integrations/react-native-bleng');
const hostNodeModules = path.resolve(__dirname, 'node_modules');

module.exports = mergeConfig(getDefaultConfig(__dirname), {
  watchFolders: [moduleRoot],
  resolver: {
    blockList: [new RegExp(`${moduleRoot}/node_modules/.*`)],
    extraNodeModules: {
      '@blehub/react-native-bleng': moduleRoot,
      // The package's own node_modules is blocklisted below, so every bare import its source
      // makes resolves through this host. `@babel/runtime` belongs here because the package
      // ships source: babel's helpers for its own syntax (an Error subclass) are imported by
      // the transform, from the importing file's location.
      '@babel/runtime': path.join(hostNodeModules, '@babel/runtime'),
      react: path.join(hostNodeModules, 'react'),
      'react-native': path.join(hostNodeModules, 'react-native'),
      'react-native-nitro-modules': path.join(hostNodeModules, 'react-native-nitro-modules'),
    },
  },
});
