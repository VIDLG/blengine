#import "BlengEngineLifecycle.h"
#import <React/RCTBridgeModule.h>
#import <React/RCTInvalidating.h>
#import <ReactCommon/RCTTurboModule.h>
#import <ReactCommon/RCTTurboModuleWithJSIBindings.h>
#import <jsi/jsi.h>
#include "HybridBlengCentralSpecSwift.hpp"

using namespace facebook;
using margelo::nitro::blehubreactnativebleng::HybridBlengCentralSpecSwift;

@interface BlengEngineLifecycle () <RCTBridgeModule, RCTTurboModule, RCTTurboModuleWithJSIBindings, RCTInvalidating>
@end

@implementation BlengEngineLifecycle {
  dispatch_queue_t _queue;
  BOOL _installed;
}

RCT_EXPORT_MODULE(BleHubBlengEngineLifecycle)

+ (BOOL)requiresMainQueueSetup { return NO; }

- (instancetype)init {
  if ((self = [super init])) {
    _queue = dispatch_queue_create("dev.blehub.rn.bleng-engine-lifecycle", DISPATCH_QUEUE_SERIAL);
    _engineLifetime = [BlengEngineLifetime new];
  }
  return self;
}

- (dispatch_queue_t)methodQueue { return _queue; }

- (std::shared_ptr<react::TurboModule>)getTurboModule:(const react::ObjCTurboModule::InitParams &)params {
  return std::make_shared<react::ObjCTurboModule>(params);
}

- (void)installJSIBindingsWithRuntime:(jsi::Runtime &)runtime
                        callInvoker:(const std::shared_ptr<react::CallInvoker> &)callInvoker {
  if (_installed) {
    throw jsi::JSError(runtime, "BLeng engine lifecycle bindings installed twice");
  }

  // ARC retains this exact module's lifetime in the HostFunction, including after retirement.
  BlengEngineLifetime *lifetime = (BlengEngineLifetime *)_engineLifetime;
  auto bind = jsi::Function::createFromHostFunction(runtime,
      jsi::PropNameID::forAscii(runtime, "__blehubBindBlengCentral"), 1,
      [lifetime](jsi::Runtime &rt, const jsi::Value &, const jsi::Value *args, size_t count) -> jsi::Value {
        if (count != 1 || !args[0].isObject()) {
          throw jsi::JSError(rt, "expected the actual Nitro BlengCentral object");
        }
        auto object = args[0].asObject(rt);
        if (!object.hasNativeState(rt)) {
          throw jsi::JSError(rt, "BlengCentral has no Nitro NativeState");
        }
        auto native = std::dynamic_pointer_cast<HybridBlengCentralSpecSwift>(object.getNativeState(rt));
        if (!native) {
          throw jsi::JSError(rt, "NativeState is not the Swift BlengCentral implementation");
        }
        // toUnsafe retains once; Swift bindOwner consumes it even when binding is rejected.
        if (![lifetime bindOwner:native->getSwiftPart().toUnsafe()]) {
          throw jsi::JSError(rt, "BLeng session owner binding rejected: duplicate, rebound, or retired engine");
        }
        return jsi::Value::undefined();
      });

  jsi::Object descriptor(runtime);
  descriptor.setProperty(runtime, "value", std::move(bind));
  descriptor.setProperty(runtime, "writable", false);
  descriptor.setProperty(runtime, "configurable", false);
  descriptor.setProperty(runtime, "enumerable", false);
  runtime.global().getPropertyAsObject(runtime, "Object").getPropertyAsFunction(runtime, "defineProperty")
      .call(runtime, runtime.global(), jsi::String::createFromAscii(runtime, "__blehubBindBlengCentral"), descriptor);
  _installed = YES;
}

- (void)invalidate {
  // RN invalidation seals admission synchronously; native cleanup completes asynchronously.
  [(BlengEngineLifetime *)_engineLifetime invalidate];
}

@end
