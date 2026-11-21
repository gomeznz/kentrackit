import UIKit

/// The phone's own settings: what the web pages can't control.
final class SettingsViewController: UITableViewController {
    private enum Row { case share, shareNote, always, alarms, others, notifyNote, test, server, version }
    private let sections: [(String, [Row], String?)] = [
        ("Location", [.share, .shareNote, .always], nil),
        ("Alerts", [.alarms, .others, .notifyNote, .test], nil),
        ("App", [.server, .version], nil),
    ]

    init() { super.init(style: .insetGrouped) }
    required init?(coder: NSCoder) { fatalError() }

    override func viewDidLoad() {
        super.viewDidLoad()
        title = "Phone settings"
        navigationItem.rightBarButtonItem = UIBarButtonItem(systemItem: .done, primaryAction: UIAction { [weak self] _ in
            self?.dismiss(animated: true)
        })
        LocationSharing.shared.onChange = { [weak self] in
            self?.tableView.reloadData()
            ((self?.presentingViewController) as? WebViewController)?.installBridge()
        }
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        if let web = presentingViewController as? WebViewController {
            web.installBridge()
            LocationSharing.shared.onChange = { [weak web] in web?.installBridge() }
        }
    }

    override func numberOfSections(in tableView: UITableView) -> Int { sections.count }
    override func tableView(_ tableView: UITableView, titleForHeaderInSection s: Int) -> String? { sections[s].0 }
    override func tableView(_ tableView: UITableView, numberOfRowsInSection s: Int) -> Int { sections[s].1.count }

    override func tableView(_ tableView: UITableView, cellForRowAt ip: IndexPath) -> UITableViewCell {
        let row = sections[ip.section].1[ip.row]
        let cell = UITableViewCell(style: .subtitle, reuseIdentifier: nil)
        var c = cell.defaultContentConfiguration()
        c.secondaryTextProperties.color = .secondaryLabel
        c.secondaryTextProperties.numberOfLines = 0
        let p = Prefs.shared
        let loc = LocationSharing.shared
        switch row {
        case .share:
            c.text = "Share this phone's location"
            cell.accessoryView = toggle(p.sharing) { [weak self] on in self?.setSharing(on) }
            cell.selectionStyle = .none
        case .shareNote:
            c.text = nil
            c.secondaryText = shareNote()
            cell.selectionStyle = .none
        case .always:
            c.text = loc.always ? "Location: Always" : "Allow location all the time"
            c.secondaryText = loc.always ? "Sends with the screen off, and keeps emergency alerts coming."
                : "Opens Settings: choose Location → Always, so sharing and alerts continue with the screen off."
            c.textProperties.color = loc.always ? .secondaryLabel : view.tintColor
            cell.selectionStyle = loc.always ? .none : .default
        case .alarms:
            c.text = "Emergency alerts"
            cell.accessoryView = toggle(p.watchAlerts) { on in
                p.watchAlerts = on
                AlertWatcher.shared.sync()
            }
            cell.selectionStyle = .none
        case .others:
            c.text = "Other alerts"
            c.secondaryText = "Overdue units, speeding, zones and other warnings."
            cell.accessoryView = toggle(p.otherAlerts) { on in
                p.otherAlerts = on
                PushToken.shared.upload(force: true)
            }
            cell.selectionStyle = .none
        case .notifyNote:
            c.secondaryText = Prefs.shared.pushActive
                ? "Emergencies arrive even with KenTrackIt closed. Allow notifications for KenTrackIt in the iPhone's Settings."
                : "Alerts arrive while KenTrackIt is open, or with the screen off while it's sharing this phone's location. "
                + "With the app closed too, once your KenTrackIt server has iPhone alerts set up."
            cell.selectionStyle = .none
        case .test:
            c.text = "Test the alarm"
            c.textProperties.color = view.tintColor
        case .server:
            c.text = "Change server"
            c.secondaryText = p.server
            c.textProperties.color = view.tintColor
        case .version:
            c.text = "Version \(AppInfo.version)"
            cell.selectionStyle = .none
        }
        cell.contentConfiguration = c
        return cell
    }

    override func tableView(_ tableView: UITableView, didSelectRowAt ip: IndexPath) {
        tableView.deselectRow(at: ip, animated: true)
        switch sections[ip.section].1[ip.row] {
        case .always where !LocationSharing.shared.always:
            if LocationSharing.shared.status == .notDetermined || LocationSharing.shared.status == .authorizedWhenInUse {
                LocationSharing.shared.requestPermission()
            } else if let url = URL(string: UIApplication.openSettingsURLString) {
                UIApplication.shared.open(url)
            }
        case .test:
            Alarm.shared.requestPermission()
            Alarm.shared.test()
        case .server:
            let vc = SetupViewController()
            navigationController?.pushViewController(vc, animated: true)
        default:
            break
        }
    }

    private func toggle(_ on: Bool, _ changed: @escaping (Bool) -> Void) -> UISwitch {
        let s = UISwitch()
        s.isOn = on
        s.addAction(UIAction { a in changed((a.sender as? UISwitch)?.isOn ?? false) }, for: .valueChanged)
        return s
    }

    private func shareNote() -> String {
        let p = Prefs.shared
        let loc = LocationSharing.shared
        if !p.sharing { return "Show this phone on the KenTrackIt map as its own unit, with your name." }
        if !loc.allowed { return "Location permission is off, so nothing is being sent." }
        let last = loc.lastResult.isEmpty ? "" : " Last: \(loc.lastResult)."
        if !loc.always { return "Sent every \(p.intervalS) s while KenTrackIt is open.\(last)" }
        return "Sent every \(p.intervalS) s, also with the screen off.\(last)"
    }

    private func setSharing(_ on: Bool) {
        let loc = LocationSharing.shared
        if on && !loc.allowed {
            loc.requestPermission()
        }
        Task {
            let r = try? await Api.request("POST", "/api/mobile/sharing", json: ["on": on])
            let code = r?.0 ?? 0
            var msg: String?
            if on {
                if code == 403 { msg = "Phone tracking is turned off on this KenTrackIt system." }
                else if code == 401 { msg = "Sign in first, then turn this on." }
                else if code != 200 { msg = "Couldn't reach KenTrackIt." }
            }
            Prefs.shared.sharing = on && msg == nil
            loc.sync()
            tableView.reloadData()
            (presentingViewController as? WebViewController)?.installBridge()
            if let msg {
                let a = UIAlertController(title: nil, message: msg, preferredStyle: .alert)
                a.addAction(UIAlertAction(title: "OK", style: .default))
                present(a, animated: true)
            }
        }
    }
}
