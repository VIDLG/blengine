import UIKit
import React
import React_RCTAppDelegate


@main
class AppDelegate: UIResponder, UIApplicationDelegate {
  var window: UIWindow?
  var reactNativeDelegate: FormalReactNativeDelegate?
  var reactNativeFactory: RCTReactNativeFactory?
  private static let nativeProcessIdentity = FormalNativeProcessIdentity()

  func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
  ) -> Bool {
    do {
      try Self.nativeProcessIdentity.start(
        arguments: ProcessInfo.processInfo.arguments,
        documents: try FileManager.default.url(
          for: .documentDirectory, in: .userDomainMask, appropriateFor: nil, create: false
        )
      )
    } catch {
      // Do not log container paths, identity payloads or observer pointers.
      NSLog("BleHub formal native identity startup rejected.")
      return false
    }
    let delegate = FormalReactNativeDelegate()
    reactNativeDelegate = delegate
    reactNativeFactory = RCTReactNativeFactory(delegate: delegate)
    // The window is created by the scene, not here: iOS 26+ traps an app that never
    // adopts the scene lifecycle (EXC_BREAKPOINT in
    // ___UIApplicationEvaluateRuntimeIssueForNoSceneLifecycleAdoption_block_invoke,
    // measured on iOS 27.2), and an app-delegate window alone is not adoption.
    return true
  }

  func application(
    _ application: UIApplication,
    configurationForConnecting connectingSceneSession: UISceneSession,
    options: UIScene.ConnectionOptions
  ) -> UISceneConfiguration {
    UISceneConfiguration(
      name: "Default Configuration", sessionRole: connectingSceneSession.role
    )
  }
}

/// Owns the single window and starts React Native inside it.
///
/// This exists only to adopt the scene lifecycle the platform now requires. It adds no
/// BLE, bridge or identity surface: the native process identity is still started and
/// owned by `AppDelegate`, and the React Native factory is still created there so a
/// scene connection cannot create a second one.
final class FormalSceneDelegate: UIResponder, UIWindowSceneDelegate {
  var window: UIWindow?

  func scene(
    _ scene: UIScene,
    willConnectTo session: UISceneSession,
    options connectionOptions: UIScene.ConnectionOptions
  ) {
    guard let windowScene = scene as? UIWindowScene,
          let appDelegate = UIApplication.shared.delegate as? AppDelegate,
          let factory = appDelegate.reactNativeFactory
    else { return }
    let window = UIWindow(windowScene: windowScene)
    self.window = window
    factory.startReactNative(
      withModuleName: "BleHubBlengHost",
      in: window,
      launchOptions: nil
    )
  }
}
