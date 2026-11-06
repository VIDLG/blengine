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
      react: path.join(hostNodeModules, 'react'),
      'react-native': path.join(hostNodeModules, 'react-native'),
      'react-native-nitro-modules': path.join(hostNodeModules, 'react-native-nitro-modules'),
    },
  },
});
