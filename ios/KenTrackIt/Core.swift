import Foundation
import UIKit
import WebKit

/// The app's own settings, kept on the phone.
final class Prefs {
    static let shared = Prefs()
    private let d = UserDefaults.standard

    /// The KenTrackIt server, e.g. https://track.example.co.nz (no trailing slash).
    var server: String {
        get { d.string(forKey: "server") ?? "" }
        set { d.set(newValue, forKey: "server") }
    }
    var sharing: Bool {
        get { d.bool(forKey: "sharing") }
        set { d.set(newValue, forKey: "sharing") }
    }
    /// Watch for emergencies (while the app runs, or in the background while sharing location).
    var watchAlerts: Bool {
        get { d.object(forKey: "watch") as? Bool ?? true }
        set { d.set(newValue, forKey: "watch") }
    }
    /// Also notify for warnings (overdue, speeding...).
    var otherAlerts: Bool {
        get { d.object(forKey: "other_alerts") as? Bool ?? true }
        set { d.set(newValue, forKey: "other_alerts") }
    }
    var intervalS: Int {
        get { let v = d.integer(forKey: "interval_s"); return v > 0 ? v : 15 }
        set { d.set(newValue, forKey: "interval_s") }
    }
}

enum AppInfo {
    static var version: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        return "\(v) (\(b))"
    }
    static let navy = UIColor(red: 11 / 255, green: 27 / 255, blue: 51 / 255, alpha: 1)
    static let teal = UIColor(red: 45 / 255, green: 212 / 255, blue: 191 / 255, alpha: 1)
}

/// Turning what someone types into a server address.
enum Urls {
    private static func matches(_ s: String, _ pattern: String) -> Bool {
        s.range(of: pattern, options: .regularExpression) != nil
    }

    /// "track.example.co.nz" -> "https://track.example.co.nz"; nil if it can't be an address.
    static func normalise(_ input: String) -> String? {
        var s = input.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        if s.isEmpty { return nil }
        if !s.contains("://") {
            // a bare IP address, or a name with a port, is most likely on the LAN, over http
            let lan = matches(s, #"^\d{1,3}(\.\d{1,3}){3}(:\d+)?$"#) || s.contains(":")
            s = (lan ? "http://" : "https://") + s
        }
        guard let sep = s.range(of: "://") else { return nil }
        let scheme = s[..<sep.lowerBound].lowercased()
        guard scheme == "http" || scheme == "https" else { return nil }
        let rest = String(s[sep.upperBound...])
        let host = rest.split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false).first.map(String.init) ?? ""
        guard matches(host, #"^[A-Za-z0-9.-]+(:\d{1,5})?$"#) else { return nil }
        var path = String(rest.dropFirst(host.count))
        while path.hasSuffix("/") { path.removeLast() }
        return "\(scheme)://\(host)\(path)"
    }

    /// The host name alone, from a URL or a bare host.
    static func host(_ s: String) -> String {
        var h = s
        if let r = h.range(of: "://") { h = String(h[r.upperBound...]) }
        h = String(h.split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false).first ?? "")
        h = String(h.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false).first ?? "")
        return h.lowercased()
    }

    static func sameHost(_ a: String, _ b: String) -> Bool {
        let x = host(a)
        return !x.isEmpty && x == host(b)
    }
}

/// Small HTTP calls to the server, signed in with the web view's session cookie.
enum Api {
    static var userAgentSuffix: String { "KenTrackItApp/\(AppInfo.version) (iOS \(UIDevice.current.systemVersion))" }

    static let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.httpShouldSetCookies = false
        c.httpCookieAcceptPolicy = .never
        c.timeoutIntervalForRequest = 20
        return URLSession(configuration: c)
    }()

    /// For the alert stream: the server sends a keep-alive every 15 s.
    static let streamSession: URLSession = {
        let c = URLSessionConfiguration.default
        c.httpShouldSetCookies = false
        c.httpCookieAcceptPolicy = .never
        c.timeoutIntervalForRequest = 60
        c.timeoutIntervalForResource = 7 * 24 * 3600
        return URLSession(configuration: c)
    }()

    @MainActor
    static func cookieHeader(for url: URL) async -> String? {
        guard let host = url.host?.lowercased() else { return nil }
        let all = await WKWebsiteDataStore.default().httpCookieStore.allCookies()
        let mine = all.filter { c in
            let d = c.domain.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
            return host == d || host.hasSuffix("." + d)
        }
        return HTTPCookie.requestHeaderFields(with: mine)["Cookie"]
    }

    static func request(_ method: String, _ path: String, json: [String: Any]? = nil) async throws -> (Int, Data) {
        guard let url = URL(string: Prefs.shared.server + path) else { throw URLError(.badURL) }
        var r = URLRequest(url: url)
        r.httpMethod = method
        r.setValue(userAgentSuffix, forHTTPHeaderField: "User-Agent")
        if let c = await cookieHeader(for: url) { r.setValue(c, forHTTPHeaderField: "Cookie") }
        if let json {
            r.httpBody = try JSONSerialization.data(withJSONObject: json)
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, resp) = try await session.data(for: r)
        return ((resp as? HTTPURLResponse)?.statusCode ?? 0, data)
    }
}

/// Server-sent events (text/event-stream), one line at a time.
final class SseParser {
    private var event = "message"
    private var data = ""
    private let onEvent: (String, String) -> Void

    init(_ onEvent: @escaping (String, String) -> Void) { self.onEvent = onEvent }

    func line(_ raw: String) {
        let l = raw.hasSuffix("\r") ? String(raw.dropLast()) : raw
        if l.isEmpty {
            if !data.isEmpty { onEvent(event, data) }
            event = "message"
            data = ""
            return
        }
        if l.hasPrefix(":") { return }        // a keep-alive comment
        let field: String
        var value: String
        if let i = l.firstIndex(of: ":") {
            field = String(l[..<i])
            value = String(l[l.index(after: i)...])
        } else {
            field = l
            value = ""
        }
        if value.hasPrefix(" ") { value.removeFirst() }
        switch field {
        case "event": event = value
        case "data": data += (data.isEmpty ? "" : "\n") + value
        default: break
        }
    }
}
