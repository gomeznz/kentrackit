import UIKit

/// First run (or "Change server"): where is KenTrackIt?
final class SetupViewController: UIViewController, UITextFieldDelegate {
    private let field = UITextField()
    private let status = UILabel()
    private var go: UIButton!

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = AppInfo.navy
        navigationController?.setNavigationBarHidden(presentingViewController == nil, animated: false)
        if presentingViewController != nil {
            navigationItem.leftBarButtonItem = UIBarButtonItem(systemItem: .cancel, primaryAction: UIAction { [weak self] _ in
                self?.dismiss(animated: true)
            })
        }

        let logo = UIImageView(image: UIImage(named: "Logo"))
        logo.contentMode = .scaleAspectFit
        logo.heightAnchor.constraint(equalToConstant: 96).isActive = true

        let title = UILabel()
        title.text = "KenTrackIt"
        title.font = .systemFont(ofSize: 30, weight: .bold)
        title.textColor = .white
        title.textAlignment = .center

        let intro = UILabel()
        intro.text = "Enter the address you use to open KenTrackIt in a browser."
        intro.font = .systemFont(ofSize: 15)
        intro.textColor = UIColor(white: 0.82, alpha: 1)
        intro.textAlignment = .center
        intro.numberOfLines = 0

        field.placeholder = "e.g. track.example.co.nz or 192.168.1.46:8100"
        field.text = Prefs.shared.server
        field.keyboardType = .URL
        field.autocapitalizationType = .none
        field.autocorrectionType = .no
        field.textContentType = .URL
        field.returnKeyType = .go
        field.clearButtonMode = .whileEditing
        field.borderStyle = .roundedRect
        field.delegate = self
        field.heightAnchor.constraint(equalToConstant: 48).isActive = true

        var cfg = UIButton.Configuration.filled()
        cfg.title = "Connect"
        cfg.baseBackgroundColor = AppInfo.teal
        cfg.baseForegroundColor = AppInfo.navy
        cfg.cornerStyle = .large
        go = UIButton(configuration: cfg, primaryAction: UIAction { [weak self] _ in self?.connect() })
        go.heightAnchor.constraint(equalToConstant: 52).isActive = true

        status.font = .systemFont(ofSize: 14)
        status.textAlignment = .center
        status.numberOfLines = 0

        let stack = UIStackView(arrangedSubviews: [logo, title, intro, field, go, status])
        stack.axis = .vertical
        stack.spacing = 14
        stack.setCustomSpacing(4, after: title)
        stack.setCustomSpacing(22, after: intro)
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 28),
            stack.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -28),
            stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 56),
        ])
    }

    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }

    func textFieldShouldReturn(_ textField: UITextField) -> Bool {
        connect()
        return true
    }

    private func say(_ text: String, error: Bool) {
        status.text = text
        status.textColor = error ? UIColor(red: 252 / 255, green: 165 / 255, blue: 165 / 255, alpha: 1) : UIColor(white: 0.82, alpha: 1)
    }

    private func connect() {
        guard let url = Urls.normalise(field.text ?? "") else {
            say("That doesn't look like an address.", error: true)
            return
        }
        go.isEnabled = false
        say("Checking \(url) …", error: false)
        Task {
            let err = await probe(url)
            go.isEnabled = true
            if let err {
                say(err, error: true)
                return
            }
            let p = Prefs.shared
            if !Urls.sameHost(p.server, url) {
                await PushToken.shared.remove()          // the old server stops sending this phone alerts
                p.sharing = false
                p.pushActive = false
            }
            p.server = url
            LocationSharing.shared.sync()
            Alarm.shared.requestPermission()
            AlertWatcher.shared.restart()
            (UIApplication.shared.delegate as? AppDelegate)?.showStart()
        }
    }

    /// nil when the address answers like KenTrackIt.
    private func probe(_ base: String) async -> String? {
        guard let url = URL(string: base + "/api/auth-status") else { return "That doesn't look like an address." }
        var r = URLRequest(url: url, timeoutInterval: 8)
        r.setValue(Api.userAgentSuffix, forHTTPHeaderField: "User-Agent")
        do {
            let (data, resp) = try await Api.session.data(for: r)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            if code == 200, (try? JSONSerialization.jsonObject(with: data)) is [String: Any] { return nil }
            return "That address answered, but it isn't KenTrackIt (HTTP \(code))."
        } catch let e as URLError where e.code == .appTransportSecurityRequiresSecureConnection {
            return "iOS refused a plain http address. Use the https:// address."
        } catch {
            return "Couldn't reach \(base) (\(error.localizedDescription)). Check the address and that this phone is on the right network."
        }
    }
}
