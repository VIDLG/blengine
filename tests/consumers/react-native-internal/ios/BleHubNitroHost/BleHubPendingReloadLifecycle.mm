#import "BleHubPendingReloadHost.h"
#import <React/RCTBridgeModule.h>
#import <React/RCTInvalidating.h>
#import <ReactCommon/RCTTurboModule.h>
#import <ReactCommon/RCTTurboModuleWithJSIBindings.h>
#import <jsi/jsi.h>
#import <NitroModules/HybridObject.hpp>
// Generated types are consumed, never edited. Swift's compatibility header is
// supplied by the pod build product (see the host HEADER_SEARCH_PATHS).
#include <BleHubReactNative/HybridBleHubProbeSpecSwift.hpp>

using namespace facebook;
using margelo::nitro::blehubreactnative::HybridBleHubProbeSpecSwift;

static void defineReadonly(jsi::Runtime &runtime, jsi::Object &target, const char *name, jsi::Value value) {
  jsi::Object descriptor(runtime);
  descriptor.setProperty(runtime, "value", std::move(value));
  descriptor.setProperty(runtime, "writable", false);
  descriptor.setProperty(runtime, "configurable", false);
  descriptor.setProperty(runtime, "enumerable", true);
  runtime.global().getPropertyAsObject(runtime, "Object").getPropertyAsFunction(runtime, "defineProperty")
      .call(runtime, target, jsi::String::createFromAscii(runtime, name), descriptor);
}

@interface BleHubPendingReloadLifecycle : NSObject <RCTBridgeModule, RCTTurboModule, RCTTurboModuleWithJSIBindings, RCTInvalidating>
@end

@implementation BleHubPendingReloadLifecycle {
  dispatch_queue_t _queue;
  NSString *_ownerId;
  BOOL _installed;
}
RCT_EXPORT_MODULE(BleHubPendingReloadLifecycle)

+ (BOOL)requiresMainQueueSetup { return NO; }

- (instancetype)init {
  if ((self = [super init])) {
    _queue = dispatch_queue_create("dev.blehub.rn.pending-reload-lifecycle", DISPATCH_QUEUE_SERIAL);
    _ownerId = NSUUID.UUID.UUIDString.lowercaseString;
  }
  return self;
}

- (dispatch_queue_t)methodQueue { return _queue; }

- (std::shared_ptr<react::TurboModule>)getTurboModule:(const react::ObjCTurboModule::InitParams &)params {
  return std::make_shared<react::ObjCTurboModule>(params);
}

- (void)installJSIBindingsWithRuntime:(jsi::Runtime &)runtime
                        callInvoker:(const std::shared_ptr<react::CallInvoker> &)callInvoker {
  if (![BleHubPendingReloadBridge enabled]) {
    throw jsi::JSError(runtime, "pending reload lifecycle is not enabled for this launch");
  }
  if (_installed) {
    throw jsi::JSError(runtime, "lifecycle module installed twice");
  }
  NSDictionary *config = [BleHubPendingReloadBridge installOwner:_ownerId];
  if (!config) {
    throw jsi::JSError(runtime, "native pending reload runtime installation rejected");
  }
  _installed = YES;
  jsi::Object props(runtime);
  for (NSString *key in config) {
    id value = config[key];
    if ([value isKindOfClass:NSNumber.class]) {
      defineReadonly(runtime, props, key.UTF8String, jsi::Value([value doubleValue]));
    } else {
      defineReadonly(runtime, props, key.UTF8String, jsi::String::createFromUtf8(runtime, [value UTF8String]));
    }
  }
  auto global = runtime.global();
  defineReadonly(runtime, global, "__blehubPendingReloadConfig", std::move(props));
  NSString *ownerId = _ownerId;
  auto bind = jsi::Function::createFromHostFunction(runtime,
      jsi::PropNameID::forAscii(runtime, "bindPendingReloadProbe"), 1,
      [ownerId](jsi::Runtime &rt, const jsi::Value &, const jsi::Value *args, size_t count) -> jsi::Value {
        if (count != 1 || !args[0].isObject()) {
          throw jsi::JSError(rt, "expected the actual Nitro BleHubProbe object");
        }
        auto object = args[0].asObject(rt);
        if (!object.hasNativeState(rt)) {
          throw jsi::JSError(rt, "probe has no Nitro NativeState");
        }
        auto native = object.getNativeState(rt);
        auto probe = std::dynamic_pointer_cast<HybridBleHubProbeSpecSwift>(native);
        if (!probe) {
          throw jsi::JSError(rt, "NativeState is not the Swift BleHubProbe implementation");
        }
        // toUnsafe acquires exactly one Swift reference; fromUnsafe in the SPI consumes it.
        if (![BleHubPendingReloadBridge bindProbe:probe->getSwiftPart().toUnsafe() ownerId:ownerId]) {
          throw jsi::JSError(rt, "native probe/runtime owner binding rejected");
        }
        return jsi::Value::undefined();
      });
  defineReadonly(runtime, global, "__blehubPendingReloadBindProbe", std::move(bind));
}

- (void)invalidate {
  // RCTInstance's OLD TurboModuleManager calls this on methodQueue. Neither
  // requestReload nor an NSNotification invokes it. No JS/JSI is touched here.
  if (_installed) {
    [BleHubPendingReloadBridge invalidateOwner:_ownerId];
  }
}
@end

@implementation BleHubPendingReloadHost
+ (BOOL)enabled { return [BleHubPendingReloadBridge enabled]; }
+ (BOOL)configureWithRunId:(NSString *)runId notificationName:(NSString *)name bootId:(NSString *)bootId {
  return [BleHubPendingReloadBridge configureWithRunId:runId notificationName:name bootId:bootId];
}
+ (void)observe:(NSString *)marker { [BleHubPendingReloadBridge observe:marker]; }
+ (NSString *)acceptControl:(NSString *)url { return [BleHubPendingReloadBridge acceptControl:url]; }
+ (void)fail:(NSString *)reason { [BleHubPendingReloadBridge fail:reason]; }
@end


#if DEBUG
#import <ReactCommon/RCTTurboModuleManager.h>
#import <hermes/hermes.h>
#import <NitroModules/InstallNitro.hpp>

class PendingReloadTestInvoker final : public react::CallInvoker {
 public:
  void invokeAsync(react::CallFunc &&) noexcept override {}
  void invokeSync(react::CallFunc &&) override {}
};

@interface PendingReloadTestDelegate : NSObject <RCTTurboModuleManagerDelegate>
@end
@implementation PendingReloadTestDelegate
- (Class)getModuleClassFromName:(const char *)name {
  return strcmp(name, "BleHubPendingReloadLifecycle") == 0 ? BleHubPendingReloadLifecycle.class : Nil;
}
- (id<RCTTurboModule>)getModuleInstanceFromClass:(Class)moduleClass { return [moduleClass new]; }
@end

// Exercises real Hermes NativeState + real RN TurboModuleManager installation and
// invalidation. Deliberately has NO accepted read: it must FAIL the pending-RF gate.
@interface BleHubPendingReloadRuntimeTest : NSObject
+ (NSDictionary *)evaluate;
@end
@implementation BleHubPendingReloadRuntimeTest
+ (NSDictionary *)evaluate {
  if (![BleHubPendingReloadBridge beginRuntimeBindingTest]) return @{ @"setup": @NO };
  auto runtime = facebook::hermes::makeHermesRuntime();
  PendingReloadTestDelegate *delegate = [PendingReloadTestDelegate new];
  RCTTurboModuleManager *manager = [[RCTTurboModuleManager alloc]
      initWithBridgeProxy:nil bridgeModuleDecorator:nil delegate:delegate
      jsInvoker:std::make_shared<PendingReloadTestInvoker>() devMenuConfigurationDecorator:nil];
  [manager installJSBindings:*runtime];
  runtime->global().getPropertyAsFunction(*runtime, "__turboModuleProxy")
      .call(*runtime, jsi::String::createFromAscii(*runtime, "BleHubPendingReloadLifecycle"));
  margelo::nitro::install(*runtime);
  auto config = runtime->global().getPropertyAsObject(*runtime, "__blehubPendingReloadConfig");
  NSString *ownerId = [NSString stringWithUTF8String:config.getProperty(*runtime, "rfPendingReadOwnerId").asString(*runtime).utf8(*runtime).c_str()];
  auto bind = runtime->global().getPropertyAsFunction(*runtime, "__blehubPendingReloadBindProbe");
  bool plainRejected = false;
  try { bind.call(*runtime, jsi::Object(*runtime)); } catch (const jsi::JSError &) { plainRejected = true; }
  auto probe = BleHubReactNative::BleHubReactNativeAutolinking::createBleHubProbe();
  bind.call(*runtime, probe->toObject(*runtime));
  BOOL nativeGeneration = config.getProperty(*runtime, "rfPendingReadGeneration").asNumber() == 1;
  bool immutable = false;
  try {
    runtime->evaluateJavaScript(std::make_unique<jsi::StringBuffer>(
        "'use strict'; __blehubPendingReloadConfig.rfPendingReadGeneration = 2;"), "native-unit-test");
  } catch (const jsi::JSError &) { immutable = true; }
  // No direct call to our module's invalidate: RN locates and calls the module.
  [manager invalidate];
  NSMutableDictionary *result = [[BleHubPendingReloadBridge endRuntimeBindingTest:ownerId] mutableCopy];
  result[@"plainObjectRejected"] = @(plainRejected);
  result[@"nativeGeneration"] = @(nativeGeneration);
  result[@"configurationImmutable"] = @(immutable);
  return result;
}
@end
#endif
