#pragma once

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Native-test-only Hermes/RN harness. Serialize construction and all method calls.
@interface BlengEngineRuntimeHarness : NSObject

/// Consumes one retained generated Swift wrapper reference from each pointer.
- (instancetype)initWithFirstOwner:(void *)firstOwner
                      secondOwner:(void *)secondOwner NS_DESIGNATED_INITIALIZER;
- (instancetype)init NS_UNAVAILABLE;
+ (instancetype)new NS_UNAVAILABLE;

/// These are the exact manager-created module lifetimes, retained after runtime destruction.
@property(nonatomic, strong, readonly) NSObject *firstLifetime;
@property(nonatomic, strong, readonly) NSObject *secondLifetime;
@property(nonatomic, copy, readonly) NSDictionary<NSString *, NSNumber *> *checks;

/// RN invokes the module callback; native cleanup completion is observed through the lifetimes.
- (void)invalidateFirst;
- (void)invalidateSecond;

/// Consumes one wrapper retain; checks the old runtime's binder after RN invalidation.
- (BOOL)rejectsBindingAfterFirstInvalidation:(void *)owner
    NS_SWIFT_NAME(rejectsBinding(afterFirstInvalidation:));

/// Requires prior invalidation of the corresponding manager. Repeated destruction is harmless.
- (void)destroyFirstRuntime;
- (void)destroySecondRuntime;

@end

NS_ASSUME_NONNULL_END

#import "BlengHostReloadHarness.h"
