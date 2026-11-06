import UIKit

/// Owns the single application scene window for every BLeng iOS HIL mode.
/// The AppDelegate remains the owner of probe configuration and BLE work.
final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(
        _ scene: UIScene,
        willConnectTo session: UISceneSession,
        options connectionOptions: UIScene.ConnectionOptions
    ) {
        guard let windowScene = scene as? UIWindowScene,
              let appDelegate = UIApplication.shared.delegate as? AppDelegate else {
            return
        }
        let window = UIWindow(windowScene: windowScene)
        self.window = window
        appDelegate.connect(window: window)
    }
}
