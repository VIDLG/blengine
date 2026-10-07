const path = require('node:path');

module.exports = {
  dependencies: {
    '@blehub/react-native-internal': {
      root: path.resolve(__dirname, '../../../integrations/react-native-internal'),
    },
  },
};
