#import "FormalReactNativeDelegate.h"

#import <BleHubReactNativeBleng/BlengEngineLifecycle.h>
#import <React/RCTBundleURLProvider.h>
#import <ReactCommon/RCTTurboModule.h>
#import <ReactAppDependencyProvider/RCTAppDependencyProvider.h>

#include <stdint.h>
#include <string.h>

// Match the public Swift selector's NSObject reference, BOOL result and UInt64 timeout ABI
// without importing the package's generated Swift/C++ compatibility header.
@protocol FormalBlengEnginePredecessor <NSObject>
- (BOOL)configurePredecessor:(NSObject *)previous timeoutMs:(uint64_t)timeoutMs;
- (void)rejectHostAdmission;
@end

@implementation FormalReactNativeDelegate {
  NSLock *_creationLock;
  BOOL _creating;
  NSObject *_latestLifetime;
  BOOL _creationFailed;
}

- (instancetype)init
{
  if ((self = [super init])) {
    self.dependencyProvider = [RCTAppDependencyProvider new];
    _creationLock = [NSLock new];
  }
  return self;
}

- (NSURL *_Nullable)sourceURLForBridge:(RCTBridge *)bridge
{
  return [self bundleURL];
}

- (NSURL *_Nullable)bundleURL
{
#if DEBUG
  return [[RCTBundleURLProvider sharedSettings] jsBundleURLForBundleRoot:@"index"];
#else
  return [[NSBundle mainBundle] URLForResource:@"main" withExtension:@"jsbundle"];
#endif
}

- (Class)getModuleClassFromName:(const char *)name
{
  if (name != NULL && strcmp(name, "BleHubBlengEngineLifecycle") == 0) {
    return [BlengEngineLifecycle class];
  }
  return [super getModuleClassFromName:name];
}

- (id<RCTTurboModule>)getModuleInstanceFromClass:(Class)moduleClass
{
  if (moduleClass != [BlengEngineLifecycle class]) {
    return nil;
  }

  BlengEngineLifecycle *module = [BlengEngineLifecycle new];
  @try {
    [_creationLock lock];
    @try {
      // Keep admission reserved through the unlocked hook; overlapping creation fails closed.
      if (_creating || _creationFailed) {
        _creationFailed = YES;
        [NSException raise:NSInternalInconsistencyException
                    format:@"BLeng engine lifecycle creation is already in progress or previously failed."];
      }
      _creating = YES;
      NSObject *lifetime = module.engineLifetime;
      if (lifetime == nil) {
        [NSException raise:NSInternalInconsistencyException
                    format:@"BLeng engine lifecycle has no engine lifetime."];
      }
      if (_latestLifetime != nil &&
          (![lifetime respondsToSelector:@selector(configurePredecessor:timeoutMs:)] ||
           ![(id<FormalBlengEnginePredecessor>)lifetime configurePredecessor:_latestLifetime timeoutMs:10000])) {
        [NSException raise:NSInternalInconsistencyException
                    format:@"BLeng engine lifecycle predecessor linkage is unavailable or rejected."];
      }
      _latestLifetime = lifetime;
    } @finally {
      [_creationLock unlock];
    }

    [self didCreateBlengEngineLifecycle:module];

    [_creationLock lock];
    @try {
      // A hook that catches a nested creation exception must not unseal the delegate.
      if (_creationFailed) {
        [NSException raise:NSInternalInconsistencyException
                    format:@"BLeng engine lifecycle creation failed during the host hook."];
      }
      _creating = NO;
    } @finally {
      [_creationLock unlock];
    }
    return (id<RCTTurboModule>)module;
  } @catch (NSException *exception) {
    [_creationLock lock];
    _creationFailed = YES;
    _creating = NO;
    [_creationLock unlock];
    // RN must finish registering its holder before it can deliver the real invalidation callback.
    // Throwing here would strand that holder in its creating state, even during teardown.
    [(id<FormalBlengEnginePredecessor>)module.engineLifetime rejectHostAdmission];
    return (id<RCTTurboModule>)module;
  }
}

- (void)didCreateBlengEngineLifecycle:(BlengEngineLifecycle *)module
{
}

@end
