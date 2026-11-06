module.exports = {
  preset: '@react-native/jest-preset',
  setupFilesAfterEnv: ['<rootDir>/jest.setup.js'],
  transformIgnorePatterns: [
    'node_modules/(?!((jest-)?react-native|@react-native(-community)?|react-native-safe-area-context)/)',
  ],
  moduleNameMapper: {
    '^@blehub/react-native-bleng$': '<rootDir>/../../../integrations/react-native-bleng/src/index.ts',
  },
};
