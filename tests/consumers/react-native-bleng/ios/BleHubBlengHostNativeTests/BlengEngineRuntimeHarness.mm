#import "BlengEngineRuntimeHarness.h"
#import <BleHubReactNativeBleng/BlengEngineLifecycle.h>
#import <ReactCommon/RCTTurboModuleManager.h>
#include <BleHubReactNativeBleng/HybridBlengCentralSpecSwift.hpp>
#include <NitroModules/InstallNitro.hpp>
#include <hermes/hermes.h>
#include <cstring>
#include <exception>
#include <memory>

using namespace facebook;
using margelo::nitro::blehubreactnativebleng::HybridBlengCentralSpecSwift;

namespace {
constexpr const char *moduleName = "BleHubBlengEngineLifecycle";
constexpr const char *binderName = "__blehubBindBlengCentral";

// Like the legacy native harness, this exercises synchronous installation/binding only.
// Native cleanup is awaited by Swift, never by a JS Promise or a callback into these VMs.
class BlengEngineTestInvoker final : public react::CallInvoker {
 public:
  void invokeAsync(react::CallFunc &&) noexcept override {}
  void invokeSync(react::CallFunc &&) override {}
};
} // namespace

@interface BlengEngineRuntimeDelegate : NSObject <RCTTurboModuleManagerDelegate>
@property(nonatomic, strong, readonly) BlengEngineLifecycle *module;
@end

@implementation BlengEngineRuntimeDelegate
- (Class)getModuleClassFromName:(const char *)name {
  return std::strcmp(name, moduleName) == 0 ? BlengEngineLifecycle.class : Nil;
}

- (id<RCTTurboModule>)getModuleInstanceFromClass:(Class)moduleClass {
  if (moduleClass != BlengEngineLifecycle.class || _module != nil) {
    @throw [NSException exceptionWithName:NSInternalInconsistencyException
                                   reason:@"Expected exactly one BLeng lifecycle module per manager"
                                 userInfo:nil];
  }
  _module = [BlengEngineLifecycle new];
  return (id<RCTTurboModule>)_module;
}
@end

namespace {
void installModule(jsi::Runtime &runtime, RCTTurboModuleManager *manager,
                   BlengEngineRuntimeDelegate *delegate) {
  margelo::nitro::install(runtime);
  [manager installJSBindings:runtime];
  auto module = runtime.global().getPropertyAsFunction(runtime, "__turboModuleProxy")
      .call(runtime, jsi::String::createFromAscii(runtime, moduleName));
  if (!module.isObject() || delegate.module == nil || delegate.module.engineLifetime == nil) {
    throw jsi::JSError(runtime, "RN did not materialize the captured BLeng lifecycle module");
  }
}

bool binderImmutable(jsi::Runtime &runtime) {
  return runtime.evaluateJavaScript(std::make_unique<jsi::StringBuffer>(R"JS(
    (() => {
      'use strict';
      const original = globalThis.__blehubBindBlengCentral;
      const descriptor = Object.getOwnPropertyDescriptor(globalThis, '__blehubBindBlengCentral');
      let rejected = false;
      try { globalThis.__blehubBindBlengCentral = () => {}; }
      catch (_) { rejected = true; }
      return typeof original === 'function' && descriptor.writable === false &&
        descriptor.configurable === false && rejected &&
        globalThis.__blehubBindBlengCentral === original;
    })()
  )JS"), "bleng-engine-runtime-harness").getBool();
}

NSDictionary<NSString *, NSNumber *> *bindingChecks(
    jsi::Runtime &firstRuntime, jsi::Runtime &secondRuntime,
    const std::shared_ptr<HybridBlengCentralSpecSwift> &firstOwner,
    const std::shared_ptr<HybridBlengCentralSpecSwift> &secondOwner) {
  auto firstBinder = firstRuntime.global().getPropertyAsFunction(firstRuntime, binderName);
  auto secondBinder = secondRuntime.global().getPropertyAsFunction(secondRuntime, binderName);
  bool plainObjectRejected = false;
  try {
    firstBinder.call(firstRuntime, jsi::Object(firstRuntime));
  } catch (const jsi::JSError &) {
    plainObjectRejected = true;
  }

  auto firstObject = firstOwner->toObject(firstRuntime);
  firstBinder.call(firstRuntime, firstObject);
  bool duplicateBindingRejected = false;
  try {
    firstBinder.call(firstRuntime, firstObject);
  } catch (const jsi::JSError &) {
    duplicateBindingRejected = true;
  }

  bool crossEngineBindingRejected = false;
  try {
    // Wrap the same native owner in runtime TWO. Never pass a runtime-one JSI value here.
    secondBinder.call(secondRuntime, firstOwner->toObject(secondRuntime));
  } catch (const jsi::JSError &) {
    crossEngineBindingRejected = true;
  }
  // A cross-engine attempt must not poison this fresh engine's valid admission.
  secondBinder.call(secondRuntime, secondOwner->toObject(secondRuntime));

  return @{
    @"plainObjectRejected": @(plainObjectRejected),
    @"duplicateBindingRejected": @(duplicateBindingRejected),
    @"crossEngineBindingRejected": @(crossEngineBindingRejected),
    @"binderImmutable": @(binderImmutable(firstRuntime) && binderImmutable(secondRuntime)),
  };
}
} // namespace

@implementation BlengEngineRuntimeHarness {
  std::unique_ptr<jsi::Runtime> _firstRuntime;
  std::unique_ptr<jsi::Runtime> _secondRuntime;
  RCTTurboModuleManager *_firstManager;
  RCTTurboModuleManager *_secondManager;
  // RN holds its delegate weakly; each harness-owned delegate captures its own module.
  BlengEngineRuntimeDelegate *_firstDelegate;
  BlengEngineRuntimeDelegate *_secondDelegate;
  BOOL _firstInvalidated;
  BOOL _secondInvalidated;
}

- (instancetype)initWithFirstOwner:(void *)firstOwner secondOwner:(void *)secondOwner {
  // Consume BOTH incoming retains before any runtime setup can fail. These are the generated
  // Swift wrappers supplied by the test, not a factory-created replacement session owner.
  auto firstSwiftPart = BleHubReactNativeBleng::HybridBlengCentralSpec_cxx::fromUnsafe(firstOwner);
  auto secondSwiftPart = BleHubReactNativeBleng::HybridBlengCentralSpec_cxx::fromUnsafe(secondOwner);
  if ((self = [super init])) {
    try {
      auto firstNativeOwner = std::make_shared<HybridBlengCentralSpecSwift>(firstSwiftPart);
      auto secondNativeOwner = std::make_shared<HybridBlengCentralSpecSwift>(secondSwiftPart);
      _firstRuntime = facebook::hermes::makeHermesRuntime();
      _secondRuntime = facebook::hermes::makeHermesRuntime();
      _firstDelegate = [BlengEngineRuntimeDelegate new];
      _secondDelegate = [BlengEngineRuntimeDelegate new];
      _firstManager = [[RCTTurboModuleManager alloc]
          initWithBridgeProxy:nil bridgeModuleDecorator:nil delegate:_firstDelegate
          jsInvoker:std::make_shared<BlengEngineTestInvoker>() devMenuConfigurationDecorator:nil];
      _secondManager = [[RCTTurboModuleManager alloc]
          initWithBridgeProxy:nil bridgeModuleDecorator:nil delegate:_secondDelegate
          jsInvoker:std::make_shared<BlengEngineTestInvoker>() devMenuConfigurationDecorator:nil];
      installModule(*_firstRuntime, _firstManager, _firstDelegate);
      installModule(*_secondRuntime, _secondManager, _secondDelegate);
      _firstLifetime = _firstDelegate.module.engineLifetime;
      _secondLifetime = _secondDelegate.module.engineLifetime;
      _checks = [bindingChecks(*_firstRuntime, *_secondRuntime, firstNativeOwner, secondNativeOwner) copy];
    } catch (const std::exception &error) {
      @throw [NSException exceptionWithName:NSInternalInconsistencyException
                                     reason:[NSString stringWithUTF8String:error.what()]
                                   userInfo:nil];
    }
  }
  return self;
}

- (void)invalidateFirst {
  if (!_firstInvalidated) {
    [_firstManager invalidate];
    _firstInvalidated = YES;
  }
}

- (void)invalidateSecond {
  if (!_secondInvalidated) {
    [_secondManager invalidate];
    _secondInvalidated = YES;
  }
}

- (BOOL)rejectsBindingAfterFirstInvalidation:(void *)owner {
  auto swiftPart = BleHubReactNativeBleng::HybridBlengCentralSpec_cxx::fromUnsafe(owner);
  if (!_firstInvalidated || !_firstRuntime) {
    @throw [NSException exceptionWithName:NSInternalInconsistencyException
                                 reason:@"Expected an invalidated manager with its old runtime still alive"
                               userInfo:nil];
  }
  auto native = std::make_shared<HybridBlengCentralSpecSwift>(swiftPart);
  try {
    _firstRuntime->global().getPropertyAsFunction(*_firstRuntime, binderName)
        .call(*_firstRuntime, native->toObject(*_firstRuntime));
  } catch (const jsi::JSError &) {
    return YES;
  }
  return NO;
}

- (void)destroyFirstRuntime {
  if (!_firstInvalidated) {
    @throw [NSException exceptionWithName:NSInternalInconsistencyException
                                   reason:@"Invalidate the first RN manager before destroying its runtime"
                                 userInfo:nil];
  }
  // __turboModuleProxy itself retains the manager until VM destruction. Invalidation has
  // already cleared RN's module caches, and no harness-owned JSI handles escape setup.
  _firstManager = nil;
  _firstRuntime.reset();
  _firstDelegate = nil;
}

- (void)destroySecondRuntime {
  if (!_secondInvalidated) {
    @throw [NSException exceptionWithName:NSInternalInconsistencyException
                                   reason:@"Invalidate the second RN manager before destroying its runtime"
                                 userInfo:nil];
  }
  _secondManager = nil;
  _secondRuntime.reset();
  _secondDelegate = nil;
}

- (void)dealloc {
  // Also retire partially constructed/abandoned tests, always through RN, before either VM dies.
  if (!_firstInvalidated) [_firstManager invalidate];
  if (!_secondInvalidated) [_secondManager invalidate];
  _firstManager = nil;
  _secondManager = nil;
  _firstRuntime.reset();
  _secondRuntime.reset();
}
@end
