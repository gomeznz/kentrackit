import UIKit
import WebKit

/// KenTrackIt's own web pages (map, Dispatch, reports...) so the app always matches the server,
/// plus what a browser can't do: alerts, location sharing and the phone's settings.
final class WebViewController: UIViewController, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
    private var web: WKWebView!
    private let userContent = WKUserContentController()
    private var downloadURL: URL?

    override func loadView() {
        let cfg = WKWebViewConfiguration()
        cfg.applicationNameForUserAgent = Api.userAgentSuffix
        cfg.allowsInlineMediaPlayback = true
        cfg.mediaTypesRequiringUserActionForPlayback = []
        userContent.add(WeakHandler(self), name: "kt")
        cfg.userContentController = userContent
        installBridge()
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = true
        web.isOpaque = false
        web.backgroundColor = AppInfo.navy
        web.scrollView.backgroundColor = AppInfo.navy
        if #available(iOS 16.4, *) { web.isInspectable = true }   // Safari > Develop, for testing
        view = web
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        load("/")
        LocationSharing.shared.onChange = { [weak self] in self?.installBridge() }
    }

    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }

    func load(_ path: String) {
        guard let url = URL(string: Prefs.shared.server + path) else { return }
        web.load(URLRequest(url: url))
    }

    /// What the KenTrackIt pages can ask of the app. The pages look for window.KTAndroid
    /// (the Android app came first), so the same object is offered here.
    func installBridge() {
        userContent.removeAllUserScripts()
        let js = """
        (function () {
          var app = {
            isApp: function () { return true; },
            platform: function () { return "ios"; },
            appVersion: function () { return \(jsString(AppInfo.version)); },
            sharingLocation: function () { return \(Prefs.shared.sharing ? "true" : "false"); },
            openSettings: function () { window.webkit.messageHandlers.kt.postMessage("settings"); }
          };
          window.KTAndroid = app;
          window.KTApp = app;
        })();
        """
        userContent.addUserScript(WKUserScript(source: js, injectionTime: .atDocumentStart, forMainFrameOnly: true))
    }

    private func jsString(_ s: String) -> String {
        let data = try? JSONSerialization.data(withJSONObject: [s])
        let arr = data.flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
        return String(arr.dropFirst().dropLast())
    }

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.body as? String == "settings" { openSettings() }
    }

    func openSettings() {
        let nav = UINavigationController(rootViewController: SettingsViewController())
        present(nav, animated: true)
    }

    // ---- navigation ------------------------------------------------------------------------------
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.shouldPerformDownload {
            decisionHandler(.download)
            return
        }
        guard let url = action.request.url else { decisionHandler(.allow); return }
        if ["http", "https", "about", "blob", "data"].contains(url.scheme ?? ""),
           url.scheme == "about" || url.scheme == "blob" || url.scheme == "data" || Urls.sameHost(url.absoluteString, Prefs.shared.server) {
            decisionHandler(.allow)
        } else {
            // map credits, help links, tel: and mailto: open outside the app
            UIApplication.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if let r = response.response as? HTTPURLResponse,
           let cd = r.value(forHTTPHeaderField: "Content-Disposition"), cd.lowercased().contains("attachment") {
            decisionHandler(.download)
            return
        }
        decisionHandler(response.canShowMIMEType ? .allow : .download)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // just signed in, perhaps: the alert stream can connect now
        if let u = webView.url?.path, !u.hasPrefix("/login") { AlertWatcher.shared.sync() }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let html = """
        <html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
        <body style="background:#0b1b33;color:#e6e9ef;font:17px -apple-system;padding:48px 24px;text-align:center">
        <h2>Can't reach KenTrackIt</h2><p>\(error.localizedDescription)</p>
        <p style="color:#94a3b8">\(Prefs.shared.server)</p>
        <p><a style="color:#2dd4bf" href="\(Prefs.shared.server)/">Try again</a></p></body></html>
        """
        webView.loadHTMLString(html, baseURL: URL(string: Prefs.shared.server))
    }

    // target="_blank" links and window.open
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if Urls.sameHost(url.absoluteString, Prefs.shared.server) { webView.load(action.request) } else { UIApplication.shared.open(url) }
        }
        return nil
    }

    // the Dispatch page's microphone, for this server only
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(type == .microphone && Urls.sameHost(origin.host, Prefs.shared.server) ? .grant : .deny)
    }

    // alert() / confirm() from the pages
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        present(a, animated: true)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
        present(a, animated: true)
    }

    // ---- downloads (CSV exports, backups, recordings): offered to Files, Mail, AirDrop... -----------
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent(suggestedFilename)
        downloadURL = dest
        completionHandler(dest)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let url = downloadURL else { return }
        let share = UIActivityViewController(activityItems: [url], applicationActivities: nil)
        share.popoverPresentationController?.sourceView = view
        share.popoverPresentationController?.sourceRect = CGRect(x: view.bounds.midX, y: view.bounds.midY, width: 1, height: 1)
        present(share, animated: true)
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        let a = UIAlertController(title: "Download failed", message: error.localizedDescription, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "OK", style: .default))
        present(a, animated: true)
    }
}

/// WKUserContentController keeps a strong reference to its handlers; this breaks the cycle.
private final class WeakHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?
    init(_ target: WKScriptMessageHandler) { self.target = target }
    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.userContentController(c, didReceive: message)
    }
}
