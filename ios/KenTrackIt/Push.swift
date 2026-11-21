import UIKit
import UserNotifications

/// This phone's Apple push token, passed to KenTrackIt while signed in so emergencies arrive
/// with the app closed.
final class PushToken {
    static let shared = PushToken()
    private(set) var token: String?
    private var lastSent: (token: String, at: Date, other: Bool)?

    #if DEBUG
    let env = "development"     // run from Xcode: Apple's sandbox
    #else
    let env = "production"      // TestFlight and the App Store
    #endif

    func set(_ data: Data) {
        token = data.map { String(format: "%02x", $0) }.joined()
        upload(force: true)
    }

    /// After sign-in, on each launch, and when the alert settings change.
    func upload(force: Bool = false) {
        guard let token, !Prefs.shared.server.isEmpty else { return }
        let other = Prefs.shared.otherAlerts
        if !force, let l = lastSent, l.token == token, l.other == other, Date().timeIntervalSince(l.at) < 3600 { return }
        Task {
            let r = try? await Api.request("POST", "/api/mobile/push-token",
                                           json: ["token": token, "env": env, "platform": "ios", "other_alerts": other])
            guard let r, r.0 == 200 else { return }
            let data = r.1
            lastSent = (token, Date(), other)
            if let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                Prefs.shared.pushActive = (j["push"] as? Bool) ?? false
            }
        }
    }

    /// Before signing out or changing server: stop alerts to this phone.
    func remove() async {
        guard let token else { return }
        _ = try? await Api.request("POST", "/api/mobile/push-token", json: ["token": token, "remove": true])
        lastSent = nil
    }
}
