import CoreLocation
import Foundation

/// Puts this phone on the KenTrackIt map as its own unit. With "Always" location permission it
/// keeps sending (and keeps the app running for alerts) with the screen off; iOS shows the blue
/// location indicator while it does.
final class LocationSharing: NSObject, CLLocationManagerDelegate {
    static let shared = LocationSharing()
    private let m = CLLocationManager()
    private var lastSent = Date.distantPast
    private var sending = false
    /// Called on the main thread when permission or sharing changes, so screens can refresh.
    var onChange: (() -> Void)?
    private(set) var lastResult = ""

    override init() {
        super.init()
        m.delegate = self
        m.desiredAccuracy = kCLLocationAccuracyBest
        m.distanceFilter = 5
        m.pausesLocationUpdatesAutomatically = false
        m.activityType = .otherNavigation
    }

    var status: CLAuthorizationStatus { m.authorizationStatus }
    var allowed: Bool { status == .authorizedAlways || status == .authorizedWhenInUse }
    var always: Bool { status == .authorizedAlways }

    func sync() {
        if Prefs.shared.sharing && allowed {
            m.allowsBackgroundLocationUpdates = always
            m.showsBackgroundLocationIndicator = true
            m.startUpdatingLocation()
        } else {
            m.stopUpdatingLocation()
            m.allowsBackgroundLocationUpdates = false
        }
    }

    /// Asks for "While Using" first; a second call asks to upgrade to "Always".
    func requestPermission() {
        switch status {
        case .notDetermined: m.requestWhenInUseAuthorization()
        case .authorizedWhenInUse: m.requestAlwaysAuthorization()
        default: break
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        sync()
        DispatchQueue.main.async { self.onChange?() }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let loc = locations.last, !sending,
              Date().timeIntervalSince(lastSent) >= Double(Prefs.shared.intervalS) - 0.5 else { return }
        lastSent = Date()
        sending = true
        Task {
            await send(loc)
            sending = false
        }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        lastResult = "Location unavailable: \(error.localizedDescription)"
    }

    private func send(_ loc: CLLocation) async {
        var body: [String: Any] = [
            "lat": loc.coordinate.latitude,
            "lon": loc.coordinate.longitude,
            "time": loc.timestamp.timeIntervalSince1970 * 1000,
            "accuracy": loc.horizontalAccuracy,
        ]
        if loc.speed >= 0 { body["speed"] = loc.speed }
        if loc.course >= 0 { body["course"] = loc.course }
        if loc.verticalAccuracy > 0 { body["altitude"] = loc.altitude }
        do {
            let (code, data) = try await Api.request("POST", "/api/mobile/position", json: body)
            switch code {
            case 200:
                lastResult = "Sent \(DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium))"
                if let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let iv = j["interval_s"] as? Int, iv > 0 {
                    Prefs.shared.intervalS = iv
                }
            case 403:
                // phone tracking turned off on this KenTrackIt system
                Prefs.shared.sharing = false
                lastResult = "Phone tracking is turned off on this KenTrackIt system"
                DispatchQueue.main.async {
                    self.sync()
                    self.onChange?()
                }
            case 401:
                lastResult = "Not signed in"
            default:
                lastResult = "Server said \(code)"
            }
        } catch {
            lastResult = "Couldn't reach the server"
        }
    }
}
