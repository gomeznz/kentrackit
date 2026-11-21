import UIKit

/// The app's window. Recent iOS versions only start apps that set their window up through a scene.
final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let ws = scene as? UIWindowScene else { return }
        let w = UIWindow(windowScene: ws)
        w.backgroundColor = AppInfo.navy
        w.tintColor = AppInfo.teal
        window = w
        showStart()
        w.makeKeyAndVisible()
    }

    /// The map once a server is set, otherwise the first-run screen.
    func showStart() {
        if Prefs.shared.server.isEmpty {
            window?.rootViewController = UINavigationController(rootViewController: SetupViewController())
        } else {
            window?.rootViewController = WebViewController()
        }
    }

    static var current: SceneDelegate? {
        UIApplication.shared.connectedScenes.compactMap { $0.delegate as? SceneDelegate }.first
    }

    func sceneDidBecomeActive(_ scene: UIScene) {
        AlertWatcher.shared.sync()
    }

    func sceneWillEnterForeground(_ scene: UIScene) {
        AlertWatcher.shared.sync()
    }

    func sceneDidEnterBackground(_ scene: UIScene) {
        // without location sharing iOS suspends the app soon anyway; with it the stream keeps going
        if !(Prefs.shared.sharing && LocationSharing.shared.allowed) { AlertWatcher.shared.stop() }
    }
}
