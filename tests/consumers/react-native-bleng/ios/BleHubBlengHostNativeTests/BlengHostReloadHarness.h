#pragma once

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// RCTHost reload in the existing native test process, with optional standalone Fabric rendering.
/// Neither mode is a full product App/UI or RF gate.
@interface BlengHostReloadHarness : NSObject

/// The file bundle must import the real package factory. Configuration runs synchronously before
/// RN receives each lifecycle module, after the shared formal-host delegate configures its predecessor.
/// Event callbacks may run on RN/native queues; snapshots are thread-safe. Do not retain the
/// harness from either callback. Call stop on main before releasing the harness.
- (instancetype)initWithBundleURL:(NSURL *)url
               configureLifetime:(void (^)(NSObject *lifetime, NSUInteger generation))configuration
                           event:(void (^)(NSDictionary *record))event NS_DESIGNATED_INITIALIZER;
- (instancetype)init NS_UNAVAILABLE;
+ (instancetype)new NS_UNAVAILABLE;

@property(nonatomic, copy, readonly) NSArray<NSObject *> *lifetimes;
@property(nonatomic, copy, readonly) NSArray<NSDictionary *> *events;
@property(nonatomic, copy, readonly) NSString *runId;
@property(nonatomic, readonly) BOOL sameHostAfterReload;

/// Main-thread only. One start, at most one reload, and terminal/idempotent stop.
/// Reload requires generation one's successful JS start evidence. Native admission failures are
/// recorded as native-failed and prevent further startup/reload; stop remains available.
/// Opt in before start. Retains one 390x844 root UIView, without an App, window, or UI runner.
/// The real product workbench is deliberately not mounted here: it requires window-backed
/// safe-area metrics, so product-window coverage belongs to the UI test target.
- (void)enableRenderedSurface;
/// Main-thread, pre-start only. Intentionally throw on the Objective-C configuration stack after
/// capturing the module, before invoking the configuration block. RN still owns invalidation.
- (void)enableConfigurationFailure;
- (void)start;
/// Rendered mode restarts the same host's attached surface; engine-only mode has no surface.
- (void)reload;
/// Main-thread only. Inspect the owned UIKit subtree for testID bleng-reload-status and the
/// exact accessibility label pending/started/failed. NO while absent or stopped; no JS evaluation.
- (BOOL)hasRenderedStatus:(NSString *)status;
/// Drops root-view and host ownership, leaving RN to invoke module invalidation. This is not a cleanup receipt;
/// observe the captured lifetimes' native retirement completion separately.
- (void)stop;

@end

NS_ASSUME_NONNULL_END
