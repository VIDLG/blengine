#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN
// Keep Swift AppDelegate's imports Foundation/ObjC-only. JSI/Nitro interop stays in .mm.
@interface BleHubPendingReloadHost : NSObject
@property(class, nonatomic, readonly) BOOL enabled;
+ (BOOL)configureWithRunId:(NSString *)runId notificationName:(NSString *)notificationName bootId:(NSString *)bootId
    NS_SWIFT_NAME(configure(runId:notificationName:bootId:));
+ (void)observe:(NSString *)marker;
+ (nullable NSString *)acceptControl:(NSString *)url;
+ (void)fail:(NSString *)reason;
@end
NS_ASSUME_NONNULL_END
