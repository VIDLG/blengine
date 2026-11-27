#import <React_RCTAppDelegate/RCTDefaultReactNativeFactoryDelegate.h>

@class BlengEngineLifecycle;

NS_ASSUME_NONNULL_BEGIN

@interface FormalReactNativeDelegate : RCTDefaultReactNativeFactoryDelegate

/// Called after predecessor linkage succeeds and before the module is returned to React Native.
- (void)didCreateBlengEngineLifecycle:(BlengEngineLifecycle *)module;

@end

NS_ASSUME_NONNULL_END
