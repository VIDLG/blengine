import type { ReactElement } from 'react';
import { AppRegistry, Platform, TurboModuleRegistry } from 'react-native';

jest.mock('react-native', () => ({
  AppRegistry: { registerComponent: jest.fn() },
  Platform: { OS: 'ios' },
  TurboModuleRegistry: { getEnforcing: jest.fn() },
}));
jest.mock('./App', () => ({ __esModule: true, default: () => null }));

const globals = globalThis as any;
function root(): (props: object) => ReactElement {
  jest.isolateModules(() => { require('../index'); });
  return (AppRegistry.registerComponent as jest.Mock).mock.calls.at(-1)[1]();
}

beforeEach(() => { jest.clearAllMocks(); });
afterEach(() => { delete globals.__blehubPendingReloadConfig; jest.restoreAllMocks(); });

test.each(['basic', 'rf-lab', 'rf-lab-suite', 'rf-recovery-ten'])('%s never instantiates the lifecycle module', (runtimeMode) => {
  const props = { runtimeMode, rfServiceUuid: 'provider-service' };
  expect(root()(props).props).toEqual(props);
  expect(TurboModuleRegistry.getEnforcing).not.toHaveBeenCalled();
});

test('Android pending mode retains its existing native props without the iOS module', () => {
  jest.replaceProperty(Platform, 'OS', 'android');
  const props = { runtimeMode: 'rf-pending-read-reload', rfPendingReadGeneration: 1 };
  expect(root()(props).props).toEqual(props);
  expect(TurboModuleRegistry.getEnforcing).not.toHaveBeenCalled();
});

test('the actual runtime installation overrides stale surface generation props once per boot', () => {
  const config = {
    runtimeMode: 'rf-pending-read-reload', rfPendingReadGeneration: 2,
    rfPendingReadRunId: 'native-session', rfPendingReadOwnerId: 'native-owner',
    rfReloadNotificationName: 'native-notification',
  };
  (TurboModuleRegistry.getEnforcing as jest.Mock).mockImplementation(() => {
    globals.__blehubPendingReloadConfig = Object.freeze(config);
    return {};
  });
  const Root = root();
  const stale = { runtimeMode: 'rf-pending-read-reload', rfPendingReadGeneration: 1, rfServiceUuid: 'provider-service' };
  expect(Root(stale).props).toEqual({ ...stale, ...config });
  expect(Root({ ...stale, rfPendingReadRunId: 'js-cannot-rebind' }).props).toEqual({ ...stale, ...config });
  expect(TurboModuleRegistry.getEnforcing).toHaveBeenCalledTimes(1);
  expect(TurboModuleRegistry.getEnforcing).toHaveBeenCalledWith('BleHubPendingReloadLifecycle');
});

test('missing native installation is not repaired with a JS generation default', () => {
  (TurboModuleRegistry.getEnforcing as jest.Mock).mockReturnValue({});
  expect(() => root()({ runtimeMode: 'rf-pending-read-reload' })).toThrow('native pending-reload runtime configuration is missing');
});
