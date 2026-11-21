import UIKit
import UserNotifications

@main
final class AppDelegate: UIResponder, UIApplicationDelegate, UNUserNotificationCenterDelegate {

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        if !Prefs.shared.server.isEmpty {
            Alarm.shared.requestPermission()
            AlertWatcher.shared.sync()
            LocationSharing.shared.sync()
        }
        return true
    }

    func application(_ application: UIApplication, configurationForConnecting session: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let c = UISceneConfiguration(name: "Default", sessionRole: session.role)
        c.delegateClass = SceneDelegate.self
        return c
    }

    /// The map once a server is set, otherwise the first-run screen.
    func showStart() {
        SceneDelegate.current?.showStart()
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PushToken.shared.set(deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        // e.g. built without a team that has push; alerts still come while the app runs
        print("KenTrackIt: push registration failed: \(error.localizedDescription)")
    }

    // an alert while the app is open: an emergency rings on screen as well
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        let info = notification.request.content.userInfo
        if let id = info["alert_id"] as? Int {
            if info["ack"] as? Bool == true {
                Alarm.shared.acknowledged(id: id)
            } else if notification.request.content.threadIdentifier == "emergency" {
                Alarm.shared.raise(id: id, title: notification.request.content.title,
                                   text: notification.request.content.body, notify: false)
            }
        }
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        Alarm.shared.silence()
        completionHandler()
    }
}
