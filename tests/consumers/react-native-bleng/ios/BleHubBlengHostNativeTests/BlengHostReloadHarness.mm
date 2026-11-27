#import "BlengHostReloadHarness.h"
#import "../BleHubBlengHost/FormalReactNativeDelegate.h"

#import <BleHubReactNativeBleng/BlengEngineLifecycle.h>
#import <React/RCTAssert.h>
#import <React/RCTBridgeModule.h>
#import <React/RCTBundleManager.h>
#import <React/RCTDevMenu.h>
#import <React/RCTReloadCommand.h>
#import <UIKit/UIKit.h>
#import <ReactCommon/RCTHost+Internal.h>
#import <ReactCommon/RCTTurboModule.h>



#include <cstring>
#include <memory>


@interface BlengHostReloadSlot : NSObject
@property(nonatomic, strong) BlengEngineLifecycle *module;
@property(nonatomic) BOOL configured;
@property(nonatomic) BOOL evidenceCreated;
@property(nonatomic) BOOL submitted;
@property(nonatomic) BOOL settled;
@property(nonatomic) BOOL resolved;
@end
@implementation BlengHostReloadSlot
@end

@interface BlengHostReloadState : NSObject
- (instancetype)initWithConfiguration:(void (^)(NSObject *, NSUInteger))configuration
                               event:(void (^)(NSDictionary *))event;
@property(nonatomic, copy, readonly) NSString *runId;
@property(nonatomic, copy, readonly) NSArray<NSObject *> *lifetimes;
@property(nonatomic, copy, readonly) NSArray<NSDictionary *> *events;
@property(nonatomic, readonly) BOOL sameHostAfterReload;
- (BOOL)enableRenderedSurface;
- (BOOL)enableConfigurationFailure;
- (BOOL)beginStart;
- (BOOL)beginReload;
- (BOOL)stop;
- (void)fail:(NSString *)code;
- (void)configureLifecycle:(BlengEngineLifecycle *)module;
- (NSUInteger)claimEvidenceGeneration;
- (void)report:(NSString *)stage error:(NSString *)error generation:(NSUInteger)generation;
- (void)hostDidStart:(RCTHost *)host;
@end

@implementation BlengHostReloadState {
  NSString *_runId;
  NSNumber *_pid;
  NSMutableArray<BlengHostReloadSlot *> *_slots;
  NSMutableArray<NSDictionary *> *_records;
  void (^_configuration)(NSObject *, NSUInteger);
  void (^_event)(NSDictionary *);
  NSUInteger _generation;
  NSUInteger _hostStarts;
  __weak RCTHost *_observedHost;
  BOOL _failed;
  BOOL _stopped;
  BOOL _sameHostAfterReload;
  BOOL _configurationFailureEnabled;
}

- (instancetype)initWithConfiguration:(void (^)(NSObject *, NSUInteger))configuration
                               event:(void (^)(NSDictionary *))event {
  if ((self = [super init])) {
    _runId = [NSUUID.UUID.UUIDString copy];
    _pid = @(NSProcessInfo.processInfo.processIdentifier);
    _slots = [NSMutableArray new];
    _records = [NSMutableArray new];
    _configuration = [configuration copy];
    _event = [event copy];
  }
  return self;
}

- (NSString *)runId { return _runId; }

- (NSArray<NSObject *> *)lifetimes {
  @synchronized(self) {
    NSMutableArray<NSObject *> *result = [NSMutableArray new];
    for (BlengHostReloadSlot *slot in _slots) [result addObject:slot.module.engineLifetime];
    return [result copy];
  }
}

- (NSArray<NSDictionary *> *)events {
  @synchronized(self) { return [_records copy]; }
}

- (BOOL)sameHostAfterReload {
  @synchronized(self) { return _sameHostAfterReload; }
}

// Caller holds the monitor. Reserve the last record for an explicit overflow failure.
- (NSDictionary *)appendStage:(NSString *)stage error:(NSString *)error generation:(NSUInteger)generation {
  if (_records.count >= 32) return nil;
  if (_records.count == 31) {
    stage = @"native-failed";
    error = @"event-capacity-exceeded";
    _failed = YES;
  }
  NSDictionary *record = @{
    @"stage": stage, @"error": error, @"runId": _runId, @"pid": _pid,
    @"generation": @(generation), @"sequence": @(_records.count + 1),
  };
  [_records addObject:record];
  return record;
}

- (void)deliver:(NSDictionary *)record {
  if (!record) return;
  @try {
    _event(record);
  } @catch (NSException *exception) {
    (void)exception;
    @synchronized(self) {
      if (!_failed) {
        _failed = YES;
        [self appendStage:@"native-failed" error:@"event-callback-failed" generation:_generation];
      }
    }
  }
}

- (void)fail:(NSString *)code {
  NSDictionary *record;
  @synchronized(self) {
    if (_failed) return;
    _failed = YES;
    record = [self appendStage:@"native-failed" error:code generation:_generation];
  }
  [self deliver:record];
}

- (BOOL)enableRenderedSurface {
  BOOL admitted;
  @synchronized(self) { admitted = !_failed && !_stopped && _generation == 0; }
  if (!admitted) [self fail:@"surface-mode-not-admitted"];
  return admitted;
}

- (BOOL)enableConfigurationFailure {
  BOOL admitted;
  @synchronized(self) {
    admitted = !_failed && !_stopped && _generation == 0;
    if (admitted) _configurationFailureEnabled = YES;
  }
  if (!admitted) [self fail:@"configuration-failure-mode-not-admitted"];
  return admitted;
}

- (BOOL)beginStart {
  BOOL admitted;
  @synchronized(self) {
    admitted = !_failed && !_stopped && _generation == 0;
    if (admitted) _generation = 1;
  }
  if (!admitted) [self fail:@"start-not-admitted"];
  return admitted;
}

- (BOOL)beginReload {
  BOOL admitted;
  @synchronized(self) {
    admitted = !_failed && !_stopped && _generation == 1 && _hostStarts == 1 &&
        _slots.count == 1 && _slots[0].configured && _slots[0].resolved;
    if (admitted) _generation = 2;
  }
  if (!admitted) [self fail:@"reload-not-admitted"];
  return admitted;
}

- (BOOL)stop {
  @synchronized(self) {
    if (_stopped) return NO;
    _stopped = YES;
    return YES;
  }
}

- (void)rejectModuleWithCode:(NSString *)code {
  [self fail:code];
  // Returning nil would let RN silently construct an unconfigured replacement module.
  @throw [NSException exceptionWithName:NSInternalInconsistencyException reason:code userInfo:nil];
}

- (void)configureLifecycle:(BlengEngineLifecycle *)module {
  NSUInteger generation;
  BOOL configurationFailureEnabled;
  BlengHostReloadSlot *slot = [BlengHostReloadSlot new];
  BOOL admitted;
  @synchronized(self) {
    generation = _generation;
    configurationFailureEnabled = _configurationFailureEnabled;
    admitted = !_failed && !_stopped && module != nil && generation >= 1 && generation <= 2 &&
        _slots.count == generation - 1;
    if (admitted && generation == 2) {
      admitted = _slots[0].module != module && _slots[0].module.engineLifetime != module.engineLifetime;
    }
    if (admitted) {
      slot.module = module;
      [_slots addObject:slot];
    }
  }
  if (!admitted) [self rejectModuleWithCode:@"lifecycle-slot-not-admitted"];

  @try {
    if (configurationFailureEnabled) {
      // Throw before crossing the Swift configuration callback; never unwind through Swift frames.
      @throw [NSException exceptionWithName:NSInternalInconsistencyException
                                   reason:@"intentional-host-configuration-failure" userInfo:nil];
    }
    // The shared formal-host delegate has already installed this exact module's predecessor.
    _configuration(module.engineLifetime, generation);
  } @catch (NSException *exception) {
    (void)exception;
    [self rejectModuleWithCode:@"lifetime-configuration-failed"];
  }

  NSDictionary *record = nil;
  @synchronized(self) {
    admitted = !_failed && !_stopped;
    if (admitted) {
      slot.configured = YES;
      record = [self appendStage:@"lifetime-configured" error:@"" generation:generation];
    }
  }
  if (!admitted) [self rejectModuleWithCode:@"lifecycle-configuration-superseded"];
  [self deliver:record];
  @synchronized(self) { admitted = !_failed && !_stopped; }
  if (!admitted) [self rejectModuleWithCode:@"lifecycle-configuration-superseded"];
}

- (NSUInteger)claimEvidenceGeneration {
  NSUInteger generation = 0;
  @synchronized(self) {
    if (!_failed && !_stopped && _generation >= 1 && _generation <= 2 && _slots.count == _generation) {
      BlengHostReloadSlot *slot = _slots[_generation - 1];
      if (slot.configured && !slot.evidenceCreated) {
        slot.evidenceCreated = YES;
        generation = _generation;
      }
    }
  }
  if (generation == 0) [self rejectModuleWithCode:@"evidence-slot-not-admitted"];
  return generation;
}

- (void)report:(NSString *)stage error:(NSString *)error generation:(NSUInteger)generation {
  NSDictionary *record = nil;
  BOOL invalid = NO;
  @synchronized(self) {
    if (_failed) return;
    if (_stopped || generation != _generation || generation < 1 || generation > _slots.count ||
        ![stage isKindOfClass:NSString.class] || ![error isKindOfClass:NSString.class] ||
        stage.length > 32 || error.length > 64) {
      invalid = YES;
    } else {
      BlengHostReloadSlot *slot = _slots[generation - 1];
      BOOL submitted = [stage isEqualToString:@"start-submitted"];
      BOOL resolved = [stage isEqualToString:@"start-resolved"];
      BOOL rejected = [stage isEqualToString:@"start-rejected"];
      BOOL validError = rejected ? [error isEqualToString:@"start-failed"] : error.length == 0;
      if (!slot.evidenceCreated || !validError ||
          (submitted ? slot.submitted || slot.settled : !(resolved || rejected) || !slot.submitted || slot.settled)) {
        invalid = YES;
      } else {
        if (submitted) slot.submitted = YES;
        else {
          slot.settled = YES;
          slot.resolved = resolved;
        }
        record = [self appendStage:stage error:error generation:generation];
      }
    }
  }
  if (invalid) [self fail:@"invalid-evidence"];
  else [self deliver:record];
}

- (void)hostDidStart:(RCTHost *)host {
  NSDictionary *record = nil;
  BOOL invalid;
  @synchronized(self) {
    invalid = _failed || _stopped || _generation != _hostStarts + 1 || _hostStarts >= 2 ||
        (_hostStarts == 1 && _observedHost != host);
    if (!invalid) {
      if (_hostStarts == 0) _observedHost = host;
      else _sameHostAfterReload = YES;
      _hostStarts += 1;
      record = [self appendStage:@"host-started" error:@"" generation:_generation];
    }
  }
  if (invalid) [self fail:@"host-start-not-admitted"];
  else [self deliver:record];
}
@end

@interface BlengHostReloadEvidence : NSObject <RCTBridgeModule, RCTTurboModule>
- (instancetype)init NS_UNAVAILABLE;
- (instancetype)initWithState:(BlengHostReloadState *)state generation:(NSUInteger)generation;
- (void)report:(NSString *)stage error:(NSString *)error;
@end

namespace {
class BlengHostReloadEvidenceTurboModule final : public facebook::react::ObjCTurboModule {
 public:
  explicit BlengHostReloadEvidenceTurboModule(const facebook::react::ObjCTurboModule::InitParams &params)
      : ObjCTurboModule(params) {
    methodMap_["report"] = MethodMetadata{
        2, [](facebook::jsi::Runtime &runtime, facebook::react::TurboModule &module,
              const facebook::jsi::Value *args, size_t count) -> facebook::jsi::Value {
          return static_cast<facebook::react::ObjCTurboModule &>(module).invokeObjCMethod(
              runtime, facebook::react::VoidKind, "report", @selector(report:error:), args, count);
        }};
  }
};
} // namespace

@implementation BlengHostReloadEvidence {
  BlengHostReloadState *_state;
  NSUInteger _generation;
  dispatch_queue_t _queue;
}
RCT_EXPORT_MODULE(BlengHostReloadEvidence)
+ (BOOL)requiresMainQueueSetup { return NO; }

- (instancetype)initWithState:(BlengHostReloadState *)state generation:(NSUInteger)generation {
  if ((self = [super init])) {
    _state = state;
    _generation = generation;
    _queue = dispatch_queue_create("dev.blehub.rn.host-reload-evidence", DISPATCH_QUEUE_SERIAL);
  }
  return self;
}

- (dispatch_queue_t)methodQueue { return _queue; }

- (std::shared_ptr<facebook::react::TurboModule>)getTurboModule:(const facebook::react::ObjCTurboModule::InitParams &)params {
  return std::make_shared<BlengHostReloadEvidenceTurboModule>(params);
}

RCT_EXPORT_METHOD(report:(NSString *)stage error:(NSString *)error) {
  [_state report:stage error:error generation:_generation];
}
@end

@interface BlengHostReloadDelegate : FormalReactNativeDelegate
- (instancetype)initWithBundleURL:(NSURL *)url state:(BlengHostReloadState *)state;
@end

@implementation BlengHostReloadDelegate {
  NSURL *_url;
  BlengHostReloadState *_state;
}
- (instancetype)initWithBundleURL:(NSURL *)url state:(BlengHostReloadState *)state {
  if ((self = [super init])) {
    _url = [url copy];
    _state = state;
  }
  return self;
}

- (NSURL *)bundleURL { return _url; }
- (NSURL *)sourceURLForBridge:(RCTBridge *)bridge { return _url; }

- (Class)getModuleClassFromName:(const char *)name {

  if (std::strcmp(name, "BlengHostReloadEvidence") == 0) return BlengHostReloadEvidence.class;
  return [super getModuleClassFromName:name];
}

- (id<RCTTurboModule>)getModuleInstanceFromClass:(Class)moduleClass {

  if (moduleClass == BlengHostReloadEvidence.class) {
    NSUInteger generation = [_state claimEvidenceGeneration];
    return [[BlengHostReloadEvidence alloc] initWithState:_state generation:generation];
  }
  return [super getModuleInstanceFromClass:moduleClass];
}

- (void)didCreateBlengEngineLifecycle:(BlengEngineLifecycle *)module {
  [super didCreateBlengEngineLifecycle:module];
  [_state configureLifecycle:module];
}

- (void)hostDidStart:(RCTHost *)host { [_state hostDidStart:host]; }
@end

@implementation BlengHostReloadHarness {
  NSURL *_url;
  BlengHostReloadState *_state;
  BlengHostReloadDelegate *_delegate;
  RCTReactNativeFactory *_factory;
  RCTHost *_host;
  UIView *_rootView;
  BOOL _renderedSurfaceEnabled;
  BOOL _inTransition;
  BOOL _stopRequested;
}

- (instancetype)initWithBundleURL:(NSURL *)url
               configureLifetime:(void (^)(NSObject *, NSUInteger))configuration
                           event:(void (^)(NSDictionary *))event {
  if (!NSThread.isMainThread || !url || !configuration || !event) {
    @throw [NSException exceptionWithName:NSInvalidArgumentException
                                 reason:@"invalid-host-reload-configuration" userInfo:nil];
  }
  RCTAssertMainQueue();
  if ((self = [super init])) {
    _url = [url copy];
    _state = [[BlengHostReloadState alloc] initWithConfiguration:configuration event:event];
  }
  return self;
}

- (NSArray<NSObject *> *)lifetimes { return _state.lifetimes; }
- (NSArray<NSDictionary *> *)events { return _state.events; }
- (NSString *)runId { return _state.runId; }
- (BOOL)sameHostAfterReload { return _state.sameHostAfterReload; }

- (void)enableRenderedSurface {
  RCTAssertMainQueue();
  if (_renderedSurfaceEnabled || _stopRequested) return;
  if ([_state enableRenderedSurface]) _renderedSurfaceEnabled = YES;
}

- (void)enableConfigurationFailure {
  if (!NSThread.isMainThread) { [_state fail:@"main-thread-required"]; return; }
  RCTAssertMainQueue();
  [_state enableConfigurationFailure];
}

- (void)start {
  if (!NSThread.isMainThread) { [_state fail:@"main-thread-required"]; return; }
  RCTAssertMainQueue();
  if (![_state beginStart]) return;
  NSNumber *regularFile;
  NSError *error;
  if (!_url.isFileURL || ![_url getResourceValue:&regularFile forKey:NSURLIsRegularFileKey error:&error] ||
      !regularFile.boolValue) {
    [_state fail:@"file-bundle-required"];
    return;
  }
  _inTransition = YES;
  @try {
    _delegate = [[BlengHostReloadDelegate alloc] initWithBundleURL:_url state:_state];
    _factory = [[RCTReactNativeFactory alloc] initWithDelegate:_delegate];
    RCTBundleConfiguration *bundle = [[RCTBundleConfiguration alloc] initWithBundleFilePath:_url];
#if RCT_DEV_MENU
    RCTDevMenuConfiguration *menu = [[RCTDevMenuConfiguration alloc]
        initWithDevMenuEnabled:NO shakeGestureEnabled:NO keyboardShortcutsEnabled:NO];
#else
    RCTDevMenuConfiguration *menu = [RCTDevMenuConfiguration defaultConfiguration];
#endif
    [_factory.rootViewFactory initializeReactHostWithLaunchOptions:nil
                                              bundleConfiguration:bundle
                                             devMenuConfiguration:menu];
    _host = _factory.rootViewFactory.reactHost;
    if (!_host) [_state fail:@"host-not-created"];
    else if (_renderedSurfaceEnabled && !_stopRequested) {
      _rootView = [_factory.rootViewFactory viewWithModuleName:@"BlengHostReload"
                                           initialProperties:nil launchOptions:nil];
      _rootView.frame = CGRectMake(0, 0, 390, 844);
      [_rootView setNeedsLayout];
      [_rootView layoutIfNeeded];
    }
  } @catch (NSException *exception) {
    (void)exception;
    [_state fail:@"host-start-failed"];
    [self stop];
  } @finally {
    _inTransition = NO;
    if (_stopRequested) [self releaseHostOwnership];
  }
}

- (void)reload {
  if (!NSThread.isMainThread) { [_state fail:@"main-thread-required"]; return; }
  RCTAssertMainQueue();
  if (![_state beginReload]) return;
  if (!_host || _factory.rootViewFactory.reactHost != _host) {
    [_state fail:@"host-identity-mismatch"];
    return;
  }
  // Freeze the exact old native lifetime before RN can replace its registry's manager.
  NSObject *previous = _state.lifetimes.firstObject;
  if (!previous) {
    [_state fail:@"predecessor-missing"];
    return;
  }
  _inTransition = YES;
  @try {
    if (_renderedSurfaceEnabled) {
      // The per-host listener restarts attached surfaces; never broadcast to other hosts.
      [(id<RCTReloadListener>)_host didReceiveReloadCommand];
      [_rootView setNeedsLayout];
      [_rootView layoutIfNeeded];
    } else {
      [_host reload];
    }
    if (_factory.rootViewFactory.reactHost != _host || !_state.sameHostAfterReload) {
      [_state fail:@"host-identity-mismatch"];
    }
  } @catch (NSException *exception) {
    (void)exception;
    [_state fail:@"host-reload-failed"];
  } @finally {
    _inTransition = NO;
    if (_stopRequested) [self releaseHostOwnership];
  }
}

- (void)stop {
  if (!NSThread.isMainThread) { [_state fail:@"main-thread-required"]; return; }
  RCTAssertMainQueue();
  if (![_state stop]) return;
  _stopRequested = YES;
  // A main-thread event callback may request stop before rootViewFactory assigns its host.
  if (!_inTransition) [self releaseHostOwnership];
}

- (BOOL)hasRenderedStatus:(NSString *)status {
  if (!NSThread.isMainThread) { [_state fail:@"main-thread-required"]; return NO; }
  RCTAssertMainQueue();
  if (!_renderedSurfaceEnabled || _stopRequested || !_rootView || ![status isKindOfClass:NSString.class] ||
      ![@[@"pending", @"started", @"failed"] containsObject:status]) return NO;
  [_rootView layoutIfNeeded];
  NSMutableArray<UIView *> *pending = [NSMutableArray arrayWithObject:_rootView];
  while (pending.count != 0) {
    UIView *view = pending.lastObject;
    [pending removeLastObject];
    if ([view.accessibilityIdentifier isEqualToString:@"bleng-reload-status"] &&
        view.isAccessibilityElement && [view.accessibilityLabel isEqualToString:status]) return YES;
    [pending addObjectsFromArray:view.subviews];
  }
  return NO;
}

- (void)releaseHostOwnership {
  [_rootView removeFromSuperview];
  _rootView = nil;
  _factory.rootViewFactory.reactHost = nil;
  _host = nil;
  _factory = nil;
  // Keep the weak factory delegate available while RN's asynchronous teardown finishes.
  // It owns neither the factory nor the host. Captured lifetimes remain observable after stop.
}
@end
