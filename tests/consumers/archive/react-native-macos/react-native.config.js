const path = require('node:path');
const {
  getDependencyConfig,
  getProjectConfig,
} = require('@react-native-community/cli-config-apple');

module.exports = {
  platforms: {
    macos: {
      dependencyConfig: getDependencyConfig({ platformName: 'macos' }),
      projectConfig: getProjectConfig({ platformName: 'macos' }),
    },
  },
  project: {
    macos: {
      sourceDir: 'macos',
    },
  },
  dependencies: {
    '@blehub/react-native-internal': {
      root: path.resolve(__dirname, '../../../../integrations/react-native'),
    },
  },
};
