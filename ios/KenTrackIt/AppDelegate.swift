import UIKit
import UserNotifications

@main
final class AppDelegate: UIResponder, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    var window: UIWindow?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        let w = UIWindow(frame: UIScreen.main.bounds)
        w.backgroundColor = AppInfo.navy
        w.tintColor = AppInfo.teal
        window = w
        showStart()
        w.makeKeyAndVisible()
        if !Prefs.shared.server.isEmpty {
            Alarm.shared.requestPermission()
            AlertWatcher.shared.sync()
            LocationSharing.shared.sync()
        }
        return true
    }

    /// The map once a server is set, otherwise the first-run screen.
    func showStart() {
        if Prefs.shared.server.isEmpty {
            window?.rootViewController = UINavigationController(rootViewController: SetupViewController())
        } else {
            window?.rootViewController = WebViewController()
        }
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        AlertWatcher.shared.sync()
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        // without location sharing iOS suspends the app soon anyway; with it the stream keeps going
        if !(Prefs.shared.sharing && LocationSharing.shared.allowed) { AlertWatcher.shared.stop() }
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        AlertWatcher.shared.sync()
    }

    // show alerts while the app is open, too
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        Alarm.shared.silence()
        completionHandler()
    }
}
