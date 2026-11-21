import AVFoundation
import UIKit
import UserNotifications

/// Listens to the server's alert stream while the app can run: on screen, or in the background
/// while it's sharing location. (Alerts with the app fully closed need Apple push notifications,
/// which come with ASI's Apple Developer account.)
final class AlertWatcher {
    static let shared = AlertWatcher()
    private var task: Task<Void, Never>?
    private(set) var connected = false

    func sync() {
        let p = Prefs.shared
        if p.watchAlerts && !p.server.isEmpty { start() } else { stop() }
    }

    func restart() {
        stop()
        sync()
    }

    private func start() {
        guard task == nil else { return }
        task = Task.detached(priority: .utility) { [weak self] in await self?.loop() }
    }

    func stop() {
        task?.cancel()
        task = nil
        connected = false
    }

    private func loop() async {
        var backoff: UInt64 = 2
        while !Task.isCancelled {
            do {
                guard let url = URL(string: Prefs.shared.server + "/api/stream?events=alert,alert_ack,signed_out") else { return }
                var r = URLRequest(url: url)
                r.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                r.setValue(Api.userAgentSuffix, forHTTPHeaderField: "User-Agent")
                if let c = await Api.cookieHeader(for: url) { r.setValue(c, forHTTPHeaderField: "Cookie") }
                let (bytes, resp) = try await Api.streamSession.bytes(for: r)
                guard (resp as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.userAuthenticationRequired) }
                connected = true
                backoff = 2
                let parser = SseParser { [weak self] ev, data in self?.handle(ev, data) }
                // split lines ourselves: AsyncBytes.lines drops the empty lines that end each event
                var buf = Data()
                for try await b in bytes {
                    if b == 0x0A {
                        parser.line(String(decoding: buf, as: UTF8.self))
                        buf.removeAll(keepingCapacity: true)
                    } else {
                        buf.append(b)
                    }
                }
            } catch {
                // dropped, or not signed in yet: try again shortly
            }
            connected = false
            if Task.isCancelled { return }
            try? await Task.sleep(nanoseconds: backoff * 1_000_000_000)
            backoff = min(backoff * 2, 60)
        }
    }

    private func handle(_ event: String, _ data: String) {
        guard let obj = try? JSONSerialization.jsonObject(with: Data(data.utf8)) as? [String: Any] else { return }
        let id = (obj["id"] as? Int) ?? 0
        switch event {
        case "alert":
            let detail = obj["detail"] as? String ?? ""
            let severity = obj["severity"] as? String ?? ""
            DispatchQueue.main.async {
                if severity == "critical" {
                    Alarm.shared.raise(id: id, title: "Emergency", text: detail)
                } else if severity == "warn" && Prefs.shared.otherAlerts {
                    Alarm.shared.notify(id: id, title: (obj["kind"] as? String ?? "Alert").capitalized, text: detail, critical: false)
                }
            }
        case "alert_ack":
            DispatchQueue.main.async { Alarm.shared.acknowledged(id: id) }
        default:
            break
        }
    }
}

/// An emergency on the phone: a notification, and while the app is on screen a full-screen alarm
/// with a looping sound until someone silences it.
final class Alarm {
    static let shared = Alarm()
    private var player: AVAudioPlayer?
    private var active = Set<Int>()
    private var stopTimer: Timer?
    private weak var screen: AlarmViewController?

    func requestPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    func raise(id: Int, title: String, text: String) {
        active.insert(id)
        notify(id: id, title: title, text: text, critical: true)
        if UIApplication.shared.applicationState == .active {
            startSound()
            showScreen(title: title, text: text)
        }
    }

    func notify(id: Int, title: String, text: String, critical: Bool) {
        let c = UNMutableNotificationContent()
        c.title = title
        c.body = text
        c.sound = critical ? UNNotificationSound(named: UNNotificationSoundName("alarm.wav")) : .default
        c.interruptionLevel = critical ? .timeSensitive : .active
        c.threadIdentifier = critical ? "emergency" : "alerts"
        let req = UNNotificationRequest(identifier: "alert-\(id)", content: c, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }

    func startSound() {
        guard player == nil, let url = Bundle.main.url(forResource: "alarm", withExtension: "wav") else { return }
        do {
            // .playback sounds even with the ring/silent switch on silent
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
            try AVAudioSession.sharedInstance().setActive(true)
            let p = try AVAudioPlayer(contentsOf: url)
            p.numberOfLoops = -1
            p.volume = 1
            p.play()
            player = p
        } catch {
            player = nil
        }
        stopTimer?.invalidate()
        stopTimer = Timer.scheduledTimer(withTimeInterval: 300, repeats: false) { [weak self] _ in self?.silence() }
    }

    func silence() {
        player?.stop()
        player = nil
        stopTimer?.invalidate()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        screen?.dismiss(animated: true)
    }

    /// Acknowledged on KenTrackIt (by anyone): take it off this phone.
    func acknowledged(id: Int) {
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: ["alert-\(id)"])
        active.remove(id)
        if active.isEmpty { silence() }
    }

    func test() {
        raise(id: 0, title: "Test alarm", text: "This is how an emergency looks on this phone. Tap Silence to stop it.")
        active.remove(0)
    }

    private func showScreen(title: String, text: String) {
        if let s = screen {
            s.update(title: title, text: text)
            return
        }
        guard let top = UIApplication.shared.topViewController() else { return }
        let vc = AlarmViewController(title: title, text: text)
        screen = vc
        top.present(vc, animated: true)
    }
}

final class AlarmViewController: UIViewController {
    private let titleLabel = UILabel()
    private let textLabel = UILabel()

    init(title: String, text: String) {
        super.init(nibName: nil, bundle: nil)
        modalPresentationStyle = .fullScreen
        update(title: title, text: text)
    }

    required init?(coder: NSCoder) { fatalError() }

    func update(title: String, text: String) {
        titleLabel.text = title
        textLabel.text = text
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 153 / 255, green: 27 / 255, blue: 27 / 255, alpha: 1)
        let big = UILabel()
        big.text = "EMERGENCY"
        big.font = .systemFont(ofSize: 40, weight: .heavy)
        titleLabel.font = .systemFont(ofSize: 24, weight: .bold)
        textLabel.font = .systemFont(ofSize: 18)
        for l in [big, titleLabel, textLabel] {
            l.textColor = .white
            l.textAlignment = .center
            l.numberOfLines = 0
        }
        func button(_ title: String, _ action: @escaping () -> Void) -> UIButton {
            var cfg = UIButton.Configuration.filled()
            cfg.title = title
            cfg.baseBackgroundColor = .white
            cfg.baseForegroundColor = .black
            cfg.cornerStyle = .large
            cfg.buttonSize = .large
            let b = UIButton(configuration: cfg, primaryAction: UIAction { _ in action() })
            b.heightAnchor.constraint(equalToConstant: 60).isActive = true
            return b
        }
        let stack = UIStackView(arrangedSubviews: [
            big, titleLabel, textLabel,
            button("Open the map") { Alarm.shared.silence() },
            button("Silence") { Alarm.shared.silence() },
        ])
        stack.axis = .vertical
        stack.spacing = 18
        stack.setCustomSpacing(36, after: textLabel)
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 28),
            stack.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -28),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor),
        ])
    }

    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }
}

extension UIApplication {
    func topViewController() -> UIViewController? {
        var top = connectedScenes.compactMap { ($0 as? UIWindowScene)?.keyWindow }.first?.rootViewController
            ?? (delegate as? AppDelegate)?.window?.rootViewController
        while let p = top?.presentedViewController { top = p }
        return top
    }
}
