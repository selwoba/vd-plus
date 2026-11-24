// VD Plus (Video Downloader Plus) - native macOS shell.
//
// A real Mac app window (WebKit) around the local VD Plus engine.
// On launch it starts execution/ripper_server.py (bundled in Resources) unless
// one is already running (e.g. started by the AI/MCP tools), proves the server is
// genuine (HMAC challenge), then shows the UI. Closing the window keeps it running
// in the Dock (subscriptions + clipboard watch keep working); Cmd+Q quits and stops
// the engine it started.
//
// Extras over a plain web view:
//   * Sign-in browser (globe button): log in to YouTube/Instagram/etc. inside the app;
//     cookies are exported to a private cookies.txt that downloads can use.
//   * Native folder picker for "Save to".

import Cocoa
import CryptoKit
import WebKit

let port = 8765
let appURL = URL(string: "http://127.0.0.1:\(port)/")!
let supportDir = NSHomeDirectory() + "/Library/Application Support/VD Plus"
let logPath = NSHomeDirectory() + "/Library/Logs/VDPlus.log"
let tokenPath = supportDir + "/token"
let cookiePath = supportDir + "/cookies.txt"

/// Appends one line to the engine log (shared with the Python server).
func appLog(_ msg: String) {
    let line = "[app \(ISO8601DateFormatter().string(from: Date()))] \(msg)\n"
    if let h = FileHandle(forWritingAtPath: logPath) { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile() }
}

/// Per-launch secret written (0600) by the engine; required on every API call.
func engineToken() -> String {
    (try? String(contentsOfFile: tokenPath, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}

func apiRequest(_ path: String, method: String = "GET", timeout: TimeInterval = 2, body: [String: Any]? = nil) -> URLRequest {
    var r = URLRequest(url: appURL.appendingPathComponent(path))
    r.httpMethod = method
    r.timeoutInterval = timeout
    r.setValue(engineToken(), forHTTPHeaderField: "X-Ripper-Token")
    r.setValue("application/json", forHTTPHeaderField: "Content-Type")
    if let b = body { r.httpBody = try? JSONSerialization.data(withJSONObject: b) }
    return r
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
                         WKDownloadDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    var signIn: SignInWindow?
    var sniffers: [SnifferWindow] = []

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        // Keep cookies.txt in sync with the built-in browser's logins (only if it holds any,
        // so an empty store never wipes the file).
        let store = WKWebsiteDataStore.default().httpCookieStore
        store.getAllCookies { cs in
            let sites = Set(cs.compactMap { SignInWindow.authSite($0) })
            if !sites.isEmpty {
                exportAppCookies(store) { _, _ in appLog("startup: refreshed saved logins (\(sites.sorted().joined(separator: ", ")))") }
            }
        }
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "vd")
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1120, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "VD Plus"
        window.minSize = NSSize(width: 760, height: 520)
        window.isReleasedWhenClosed = false
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("VDPlusMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        web.loadHTMLString(Self.page("Starting VD Plus…", ""), baseURL: nil)
        ping { state in
            switch state {
            case .genuine: self.waitForServer(attempt: 0)
            case .stale: self.replaceStaleEngine()
            case .down: self.startServer(); self.waitForServer(attempt: 0)
            case .impostor: self.showImpostor()
            }
        }
    }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }

    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        let active = activeDownloads()
        guard active > 0 else { return .terminateNow }
        let a = NSAlert()
        a.messageText = "\(active) download\(active == 1 ? " is" : "s are") still running"
        a.informativeText = "Quitting stops them. Interrupted downloads can be retried later."
        a.addButton(withTitle: "Keep Downloading")
        a.addButton(withTitle: "Quit Anyway")
        return a.runModal() == .alertSecondButtonReturn ? .terminateNow : .terminateCancel
    }

    func applicationWillTerminate(_ note: Notification) {
        // Quit means everything stops. The engine ends its downloads cleanly (they can be retried
        // later) whether this app started it or the AI bridge did.
        if let s = server, s.isRunning {
            s.terminate()
            s.waitUntilExit()
        } else {
            let sem = DispatchSemaphore(value: 0)
            URLSession.shared.dataTask(with: apiRequest("api/quit", method: "POST", timeout: 2, body: ["force": true])) { _, _, _ in
                sem.signal()
            }.resume()
            _ = sem.wait(timeout: .now() + 2.5)
        }
    }

    // MARK: engine

    func pythonPath() -> String {
        // Prefer the Python bundled in the app (works on Macs without Homebrew).
        let bundled = Bundle.main.resourcePath! + "/python/bin/python3"
        for p in [bundled, "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: p) { return p }
        return "/usr/bin/python3"
    }

    func startServer() {
        guard let script = Bundle.main.path(forResource: "ripper_server", ofType: "py", inDirectory: "execution") else {
            showError("Engine files are missing from the app bundle. Rebuild with execution/build_app.sh.")
            return
        }
        FileManager.default.createFile(atPath: logPath, contents: nil)
        let log = FileHandle(forWritingAtPath: logPath)
        log?.seekToEndOfFile()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath())
        p.arguments = [script, "--no-browser", "--port", String(port)]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        p.environment = env
        p.standardOutput = log
        p.standardError = log
        do {
            try p.run()
            server = p
        } catch {
            showError("Couldn't start the engine: \(error.localizedDescription)")
        }
    }

    enum EngineState { case genuine, stale, down, impostor }

    /// Identifies this build of the app; the engine reports the build it was started from.
    static let ownBuild: String = {
        guard let p = Bundle.main.path(forResource: "build-id", ofType: nil),
              let s = try? String(contentsOfFile: p, encoding: .utf8) else { return "dev" }
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }()

    /// An engine from an older copy of the app is still running (the AI bridge can start one that
    /// outlives the app). Ask it to quit and start the current one. It refuses while downloads are
    /// running; then we simply keep using it.
    func replaceStaleEngine() {
        appLog("engine: running build differs from this app (\(Self.ownBuild)), replacing")
        URLSession.shared.dataTask(with: apiRequest("api/quit", method: "POST", timeout: 3, body: [:])) { data, _, _ in
            let ok = (data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] })?["ok"] as? Bool ?? false
            DispatchQueue.main.async {
                if ok { self.startWhenPortIsFree(attempt: 0) } else { self.loadUI() }
            }
        }.resume()
    }

    func startWhenPortIsFree(attempt: Int) {
        ping { state in
            if state == .down || attempt > 24 {
                self.startServer()
                self.waitForServer(attempt: 0)
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.startWhenPortIsFree(attempt: attempt + 1) }
            }
        }
    }

    /// Challenge the server to prove it holds our token (HMAC of a fresh nonce), so a
    /// different program squatting on the port is never trusted.
    func ping(_ done: @escaping (EngineState) -> Void) {
        let nonce = UUID().uuidString
        var comps = URLComponents(url: appURL.appendingPathComponent("api/ping"), resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "nonce", value: nonce)]
        var req = URLRequest(url: comps.url!)
        req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            var state = EngineState.down
            if resp != nil {
                state = .impostor
                if let d = data, let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                   let proof = j["proof"] as? String, !engineToken().isEmpty {
                    let mac = HMAC<SHA256>.authenticationCode(for: Data(nonce.utf8),
                                                              using: SymmetricKey(data: Data(engineToken().utf8)))
                    if proof == mac.map({ String(format: "%02x", $0) }).joined() {
                        state = (j["build"] as? String ?? "") == Self.ownBuild ? .genuine : .stale
                    }
                }
            }
            DispatchQueue.main.async { done(state) }
        }.resume()
    }

    func showImpostor() {
        showError("Another program is using port \(port), so VD Plus won't connect to it. Quit that program and reopen VD Plus.")
    }

    func tokenURL() -> URL {
        var comps = URLComponents(url: appURL, resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "token", value: engineToken())]
        return comps.url!
    }

    func loadUI() { web.load(URLRequest(url: tokenURL())) }

    func waitForServer(attempt: Int) {
        ping { state in
            if state == .genuine || state == .stale {
                self.loadUI()
            } else if state == .impostor && attempt > 20 {
                self.showImpostor()
            } else if attempt < 60 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitForServer(attempt: attempt + 1) }
            } else {
                self.showError("The engine didn't start.<br>Log: \(logPath)")
            }
        }
    }

    func activeDownloads() -> Int {
        let sem = DispatchSemaphore(value: 0)
        var n = 0
        URLSession.shared.dataTask(with: apiRequest("api/jobs")) { data, _, _ in
            if let d = data, let arr = try? JSONSerialization.jsonObject(with: d) as? [[String: Any]] {
                n = arr.filter { ["downloading", "processing", "converting", "paused", "starting", "queued"].contains($0["status"] as? String ?? "") }.count
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 3)
        return n
    }

    func showError(_ msg: String) {
        web.loadHTMLString(Self.page("VD Plus couldn't start", msg), baseURL: nil)
    }

    static func page(_ title: String, _ body: String) -> String {
        """
        <html><head><meta name="color-scheme" content="light dark"><style>
        body{font:15px -apple-system,system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;
             text-align:center;color:#77706a;background:#fff}
        @media (prefers-color-scheme:dark){body{background:#121110;color:#a39b93}}
        h1{font-size:20px;color:#c2410c;font-weight:650} p{max-width:520px;line-height:1.5}
        </style></head><body><div><h1>\(title)</h1><p>\(body)</p></div></body></html>
        """
    }

    // MARK: JS bridge (only from our own page)

    func userContentController(_ ucc: WKUserContentController, didReceive msg: WKScriptMessage) {
        let origin = msg.frameInfo.securityOrigin
        guard msg.frameInfo.isMainFrame, origin.host == "127.0.0.1", origin.port == port,
              let body = msg.body as? [String: Any], let action = body["action"] as? String else { return }
        switch action {
        case "chooseFolder": chooseFolder()
        case "signIn": openSignIn(start: body["url"] as? String, reason: body["reason"] as? String)
        case "sniff":
            if let u = body["url"] as? String, let url = URL(string: u), ["http", "https"].contains(url.scheme ?? "") {
                let w = SnifferWindow(page: url) { [weak self] sw in self?.sniffers.removeAll { $0 === sw } }
                sniffers.append(w)
                w.show()
            }
        default: break
        }
    }

    func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.prompt = "Save Here"
        panel.message = "Choose where VD Plus saves downloads"
        panel.beginSheetModal(for: window) { r in
            guard r == .OK, let url = panel.url else { return }
            URLSession.shared.dataTask(with: apiRequest("api/settings", method: "POST", body: ["out_dir": url.path])) { _, _, _ in
                DispatchQueue.main.async { self.web.evaluateJavaScript("window.vdRefreshSettings && vdRefreshSettings()") }
            }.resume()
        }
    }

    func openSignIn(start: String?, reason: String? = nil) {
        if signIn == nil {
            signIn = SignInWindow(onGoogle: { [weak self] browser in
                // Google login comes from the user's real browser; in-app logins still apply.
                URLSession.shared.dataTask(with: apiRequest("api/settings", method: "POST",
                                                            body: ["cookies": "app", "google_cookies": browser])) { _, _, _ in
                    DispatchQueue.main.async {
                        self?.web.evaluateJavaScript("window.vdGoogleLogin && vdGoogleLogin('\(browser)')")
                    }
                }.resume()
            }) { [weak self] count in
                // Cookies exported: switch downloads to use them and tell the page.
                URLSession.shared.dataTask(with: apiRequest("api/settings", method: "POST", body: ["cookies": "app"])) { _, _, _ in
                    DispatchQueue.main.async {
                        self?.web.evaluateJavaScript("window.vdSignedIn && vdSignedIn(\(count))")
                    }
                }.resume()
            }
        }
        signIn?.show(start: start, reason: reason)
    }

    // MARK: web view behaviour

    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction,
                 preferences: WKWebpagePreferences,
                 decisionHandler: @escaping (WKNavigationActionPolicy, WKWebpagePreferences) -> Void) {
        if action.shouldPerformDownload {
            decisionHandler(.download, preferences)
            return
        }
        // The window only ever shows VD Plus itself. Outside http(s) links the user
        // clicks open in the default browser; any other navigation is refused.
        guard let u = action.request.url else { return decisionHandler(.cancel, preferences) }
        let isApp = u.host == "127.0.0.1" && u.port == port && u.scheme == "http"
        let isInternal = ["about", "blob", "data"].contains(u.scheme ?? "")
        if isApp || (isInternal && action.targetFrame != nil) {
            decisionHandler(.allow, preferences)
        } else {
            if ["http", "https"].contains(u.scheme ?? "") { NSWorkspace.shared.open(u) }
            decisionHandler(.cancel, preferences)
        }
    }

    // target=_blank links
    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for action: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = action.request.url, ["http", "https"].contains(u.scheme ?? "") { NSWorkspace.shared.open(u) }
        return nil
    }

    func webView(_ w: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ w: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.beginSheetModal(for: window) { r in
            guard r == .OK, let url = panel.url else { return completionHandler(nil) }
            try? FileManager.default.removeItem(at: url)
            completionHandler(url)
        }
    }

    func webView(_ w: WKWebView, runOpenPanelWith params: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = params.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { loadUI() }

    // MARK: menu

    @objc func reload(_ s: Any?) { loadUI() }
    @objc func openInBrowser(_ s: Any?) { NSWorkspace.shared.open(tokenURL()) }
    @objc func showMain(_ s: Any?) { window.makeKeyAndOrderFront(nil) }
    @objc func openFolder(_ s: Any?) {
        URLSession.shared.dataTask(with: apiRequest("api/reveal", method: "POST", body: [:])).resume()
    }
    @objc func openLog(_ s: Any?) { NSWorkspace.shared.open(URL(fileURLWithPath: logPath)) }
    @objc func signInMenu(_ s: Any?) { openSignIn(start: nil) }
    @objc func pasteLink(_ s: Any?) { web.evaluateJavaScript("window.vdPasteLink && vdPasteLink()") }
    @objc func openSettings(_ s: Any?) { web.evaluateJavaScript("window.vdOpenSettings && vdOpenSettings()") }

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem(); main.addItem(appItem)
        let app = NSMenu()
        app.addItem(withTitle: "About VD Plus", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Settings…", action: #selector(openSettings(_:)), keyEquivalent: ",")
        app.addItem(.separator())
        app.addItem(withTitle: "Hide VD Plus", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let others = app.addItem(withTitle: "Hide Others", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        others.keyEquivalentModifierMask = [.command, .option]
        app.addItem(.separator())
        app.addItem(withTitle: "Quit VD Plus", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = app

        let fileItem = NSMenuItem(); main.addItem(fileItem)
        let file = NSMenu(title: "File")
        let pl = file.addItem(withTitle: "Paste Link", action: #selector(pasteLink(_:)), keyEquivalent: "v")
        pl.keyEquivalentModifierMask = [.command, .shift]
        file.addItem(withTitle: "Open Downloads Folder", action: #selector(openFolder(_:)), keyEquivalent: "o")
        file.addItem(withTitle: "Sign In to Sites…", action: #selector(signInMenu(_:)), keyEquivalent: "")
        file.addItem(.separator())
        file.addItem(withTitle: "Show Engine Log", action: #selector(openLog(_:)), keyEquivalent: "")
        file.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = file

        // Without an Edit menu, Cmd+C / Cmd+V don't work in a WKWebView.
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(reload(_:)), keyEquivalent: "r")
        view.addItem(withTitle: "Open in Browser", action: #selector(openInBrowser(_:)), keyEquivalent: "")
        view.addItem(.separator())
        let fs = view.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        fs.keyEquivalentModifierMask = [.command, .control]
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "VD Plus", action: #selector(showMain(_:)), keyEquivalent: "0")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        winItem.submenu = win
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }
}

// MARK: - Cookie export (shared by sign-in + sniffer)

/// Writes every cookie in the app's web data store to cookies.txt (Netscape format, 0600).
func exportAppCookies(_ store: WKHTTPCookieStore, _ done: @escaping (Int, Int) -> Void) {
    store.getAllCookies { cookies in
        var lines = ["# Netscape HTTP Cookie File", "# Written by VD Plus. Private: do not share."]
        var domains = Set<String>()
        for c in cookies {
            let sub = c.domain.hasPrefix(".") ? "TRUE" : "FALSE"
            let exp = Int(c.expiresDate?.timeIntervalSince1970 ?? 0)
            let prefix = c.isHTTPOnly ? "#HttpOnly_" : ""
            let value = c.value.replacingOccurrences(of: "\n", with: "").replacingOccurrences(of: "\t", with: "")
            lines.append([prefix + c.domain, sub, c.path, c.isSecure ? "TRUE" : "FALSE", String(exp), c.name, value]
                .joined(separator: "\t"))
            domains.insert(c.domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")))
        }
        let fm = FileManager.default
        try? fm.createDirectory(atPath: supportDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        fm.createFile(atPath: cookiePath, contents: (lines.joined(separator: "\n") + "\n").data(using: .utf8),
                      attributes: [.posixPermissions: 0o600])
        try? fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: cookiePath)
        DispatchQueue.main.async { done(cookies.count, domains.count) }
    }
}

// MARK: - Sign-in browser

/// A small browser window for logging in to video sites. Its cookies are exported
/// (Netscape format, 0600) so yt-dlp can download private / age-restricted /
/// Instagram content as the user. Nothing leaves the Mac.
final class SignInWindow: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let window: NSWindow
    let web: WKWebView
    let address = NSTextField()
    var note: NSTextField!
    let onExport: (Int) -> Void
    let onGoogle: (String) -> Void
    var autoMode = false          // opened by VD Plus for a failed download
    var targetSite: String?       // e.g. "Instagram"
    var exportedSites = Set<String>()

    /// The cookie that proves you're logged in, per site.
    static let authCookies: [(domain: String, name: String, site: String)] = [
        ("instagram.com", "sessionid", "Instagram"),
        ("facebook.com", "c_user", "Facebook"),
        ("tiktok.com", "sessionid", "TikTok"),
        ("x.com", "auth_token", "X"),
        ("twitter.com", "auth_token", "X"),
        ("vimeo.com", "vimeo", "Vimeo"),
    ]

    static func authSite(_ c: HTTPCookie) -> String? {
        let d = c.domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased()
        return authCookies.first { (d == $0.domain || d.hasSuffix("." + $0.domain)) && c.name == $0.name && !c.value.isEmpty }?.site
    }

    static func siteFor(host: String) -> String? {
        let h = host.lowercased()
        return authCookies.first { h == $0.domain || h.hasSuffix("." + $0.domain) }?.site
    }

    static let sites: [(String, String)] = [
        ("YouTube", "https://accounts.google.com/"),
        ("Instagram", "https://www.instagram.com/accounts/login/"),
        ("TikTok", "https://www.tiktok.com/login"),
        ("X", "https://x.com/login"),
        ("Facebook", "https://www.facebook.com/login"),
        ("Vimeo", "https://vimeo.com/log_in"),
    ]

    init(onGoogle: @escaping (String) -> Void, onExport: @escaping (Int) -> Void) {
        self.onExport = onExport
        self.onGoogle = onGoogle
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        web = WKWebView(frame: .zero, configuration: cfg)
        // Google refuses sign-in from embedded browsers that announce themselves; look like Safari.
        web.customUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        super.init()
        window.title = "Sign In – VD Plus"
        window.isReleasedWhenClosed = false
        window.delegate = self
        web.navigationDelegate = self
        web.uiDelegate = self

        let bar = NSStackView()
        bar.orientation = .horizontal
        bar.spacing = 6
        bar.edgeInsets = NSEdgeInsets(top: 8, left: 10, bottom: 8, right: 10)
        for (i, s) in Self.sites.enumerated() {
            let b = NSButton(title: s.0, target: self, action: #selector(site(_:)))
            b.bezelStyle = .rounded
            b.tag = i
            bar.addArrangedSubview(b)
        }
        address.placeholderString = "Or type a site address and press Return"
        address.target = self
        address.action = #selector(go(_:))
        address.setContentHuggingPriority(.defaultLow, for: .horizontal)
        bar.addArrangedSubview(address)
        let done = NSButton(title: "Done – Use These Logins", target: self, action: #selector(finish(_:)))
        done.bezelStyle = .rounded
        done.keyEquivalent = "\r"
        bar.addArrangedSubview(done)

        note = NSTextField(wrappingLabelWithString: Self.defaultNote)
        note.font = .systemFont(ofSize: 12)
        note.textColor = .secondaryLabelColor

        let stack = NSStackView(views: [bar, note, web])
        stack.orientation = .vertical
        stack.spacing = 0
        stack.alignment = .leading
        stack.setHuggingPriority(.defaultLow, for: .horizontal)
        note.translatesAutoresizingMaskIntoConstraints = false
        web.translatesAutoresizingMaskIntoConstraints = false
        bar.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = stack
        NSLayoutConstraint.activate([
            bar.widthAnchor.constraint(equalTo: stack.widthAnchor),
            web.widthAnchor.constraint(equalTo: stack.widthAnchor),
            note.leadingAnchor.constraint(equalTo: stack.leadingAnchor, constant: 12),
            note.trailingAnchor.constraint(equalTo: stack.trailingAnchor, constant: -12),
        ])
        window.center()
    }

    static let defaultNote = "Log in to the sites you want to download from. When you click Done, VD Plus saves the logins privately on this Mac and uses them for downloads."

    func show(start: String?, reason: String? = nil) {
        autoMode = (reason?.isEmpty == false)
        targetSite = start.flatMap { URL(string: $0)?.host }.flatMap { Self.siteFor(host: $0) }
        if let r = reason, !r.isEmpty {
            note.stringValue = r
            note.font = .systemFont(ofSize: 13, weight: .semibold)
            note.textColor = NSColor(red: 0.761, green: 0.255, blue: 0.047, alpha: 1)
        } else {
            note.stringValue = Self.defaultNote
            note.font = .systemFont(ofSize: 12)
            note.textColor = .secondaryLabelColor
        }
        if let s = start, let u = URL(string: s), ["http", "https"].contains(u.scheme ?? "") {
            appLog("signin: open \(u.host ?? "")\(u.path)")
            if (u.host ?? "").hasSuffix("google.com") { googleRequested = true }  // YouTube login asked for by VD Plus
            web.load(URLRequest(url: u))
        } else if web.url == nil {
            web.loadHTMLString("""
                <html><head><meta name="color-scheme" content="light dark"><style>
                body{font:15px -apple-system,system-ui;display:flex;align-items:center;justify-content:center;height:90vh;margin:0;text-align:center;color:#77706a}
                h2{color:#c2410c;font-weight:650} p{max-width:520px;line-height:1.5}</style></head>
                <body><div><h2>Sign in to the sites you download from</h2>
                <p>Pick a site above and log in normally. Click <b>Done</b> when finished.</p>
                <p>YouTube: Google blocks signing in inside apps, so VD Plus will offer to use your browser's YouTube login instead.</p></div></body></html>
                """, baseURL: nil)
        }
        // macOS won't let an app steal focus, so make sure this window is at least
        // visible on the current desktop, in front, and the Dock icon bounces.
        window.collectionBehavior.insert(.moveToActiveSpace)
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.orderFrontRegardless()
        window.makeKey()
        NSApp.activate(ignoringOtherApps: true)
        if reason != nil { NSApp.requestUserAttention(.criticalRequest) }
    }

    var googleRequested = false

    @objc func site(_ b: NSButton) {
        if Self.sites[b.tag].0 == "YouTube" { googleRequested = true }
        web.load(URLRequest(url: URL(string: Self.sites[b.tag].1)!))
    }

    @objc func go(_ f: NSTextField) {
        var s = f.stringValue.trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty else { return }
        if !s.hasPrefix("http") { s = "https://" + s }
        if let u = URL(string: s) { web.load(URLRequest(url: u)) }
    }

    func webView(_ w: WKWebView, didCommit nav: WKNavigation!) {
        address.stringValue = w.url?.absoluteString ?? ""
        appLog("signin: at \(w.url?.host ?? "")\(w.url?.path ?? "")")
    }

    // After each page load: if a new site is logged in, save it right away and retry
    // downloads. No need to click Done. If we were opened for a failed download and that
    // site is now logged in (or already was), close ourselves.
    func webView(_ w: WKWebView, didFinish nav: WKNavigation!) {
        let store = w.configuration.websiteDataStore.httpCookieStore
        store.getAllCookies { cs in
            let sites = Set(cs.compactMap { Self.authSite($0) })
            let fresh = sites.subtracting(self.exportedSites)
            let targetDone = self.targetSite.map { sites.contains($0) } ?? false
            guard !fresh.isEmpty || (self.autoMode && targetDone) else { return }
            exportAppCookies(store) { _, domains in
                self.exportedSites.formUnion(sites)
                appLog("signin: logged in to \(sites.sorted().joined(separator: ", ")), saved")
                self.onExport(domains)
                if self.autoMode && targetDone {
                    self.autoMode = false
                    self.note.stringValue = "✓ You're signed in to \(self.targetSite ?? "the site"). Retrying your download… This window will close."
                    self.note.textColor = NSColor(red: 0.08, green: 0.5, blue: 0.24, alpha: 1)
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { self.window.orderOut(nil) }
                }
            }
        }
    }

    // Google blocks sign-in inside embedded browsers (it hangs on "This may take a moment").
    // Instead of letting the user get stuck, offer to use their Google login from a real browser.
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let host = action.request.url?.host ?? ""
        if action.targetFrame?.isMainFrame != false,
           host == "accounts.google.com" || host.hasSuffix(".accounts.google.com") || host == "accounts.youtube.com" {
            decisionHandler(.cancel)
            // Only ask when the user clicked their way here (a link, form, or our YouTube button).
            // Pages like Vimeo's login redirect to Google on their own for "Continue with Google".
            if action.navigationType == .linkActivated || action.navigationType == .formSubmitted || googleRequested {
                googleRequested = false
                askGoogleBrowser()
            } else {
                appLog("signin: ignored automatic redirect to \(host)")
            }
            return
        }
        decisionHandler(.allow)
    }

    static let browsers: [(id: String, name: String, path: String)] = [
        ("chrome", "Chrome", "/Applications/Google Chrome.app"),
        ("safari", "Safari", "/Applications/Safari.app"),
        ("arc", "Arc", "/Applications/Arc.app"),
        ("brave", "Brave", "/Applications/Brave Browser.app"),
        ("edge", "Edge", "/Applications/Microsoft Edge.app"),
        ("firefox", "Firefox", "/Applications/Firefox.app"),
    ]

    func askGoogleBrowser() {
        let installed = Self.browsers.filter { $0.id == "safari" || FileManager.default.fileExists(atPath: $0.path) }
        let a = NSAlert()
        a.messageText = "Google doesn't allow signing in inside apps"
        a.informativeText = """
            VD Plus can use the YouTube / Google login from your web browser instead.             Pick the browser where you're signed in to YouTube.

            Chrome, Arc, Brave, Edge: macOS will ask to let VD Plus use the browser's keychain item. Click Always Allow.
            Safari: needs Full Disk Access for VD Plus (System Settings › Privacy & Security).

            Most YouTube videos download without any login. You only need this for age-restricted, private or members-only videos.
            """
        for b in installed.prefix(4) { a.addButton(withTitle: b.name) }
        a.addButton(withTitle: "Cancel").keyEquivalent = "\u{1b}"
        a.buttons.first?.keyEquivalent = ""  // Return must not silently pick a browser
        a.beginSheetModal(for: window) { r in
            let i = r.rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
            guard i >= 0, i < min(installed.count, 4) else { return }
            self.onGoogle(installed[i].id)
            self.web.load(URLRequest(url: URL(string: "https://www.youtube.com/")!))
        }
    }

    // Popup logins (e.g. "Continue with Google") open in the same view.
    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for action: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        w.load(action.request)
        return nil
    }

    @objc func finish(_ s: Any?) {
        exportCookies { self.window.close() }
    }

    func windowWillClose(_ n: Notification) { exportCookies {} }

    func exportCookies(_ done: @escaping () -> Void) {
        exportAppCookies(web.configuration.websiteDataStore.httpCookieStore) { n, sites in
            if n > 0 { self.onExport(sites) }
            done()
        }
    }
}

// MARK: - Find video on page (stream sniffer)

/// Opens a page like a normal browser and watches what media it loads (HLS/DASH manifests,
/// direct MP4/WebM), including inside embedded players and iframes. The user picks a stream
/// and it's handed to the engine with the page as referer and the app's cookies.
final class SnifferWindow: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    let window: NSWindow
    var web: WKWebView!
    let page: URL
    let list = NSStackView()
    let status = NSTextField(labelWithString: "Loading page… If the video doesn't start by itself, press play in the page.")
    var found: [(url: String, kind: String, title: String, frame: String)] = []
    let onClose: (SnifferWindow) -> Void

    static let script = """
    (() => {
      if (window.__vdSniff) return; window.__vdSniff = true;
      const seen = new Set();
      const MEDIA = /\\.(m3u8|mpd|mp4|m4v|webm|mov|mkv|flv|f4m|m4a|mp3)(\\?|#|$)|\\/manifest(\\.mpd)?(\\?|$)|[?&](format|type)=(m3u8|mpd|hls|dash)/i;
      const SEGMENT = /\\.(ts|m4s|aac|key)(\\?|$)|[?&](range|bytestart|byteend)=|\\/seg(ment)?[-_]?\\d+|init\\.mp4/i;
      const post = (u, how) => {
        try { u = new URL(u, location.href).href; } catch (e) { return; }
        if (!/^https?:/.test(u) || seen.has(u) || !MEDIA.test(u) || SEGMENT.test(u)) return;
        seen.add(u);
        try { window.webkit.messageHandlers.sniff.postMessage({ url: u, how, page: location.href, title: (top === self ? document.title : "") }); } catch (e) {}
      };
      const of = window.fetch;
      if (of) window.fetch = function (i, ...r) { try { post(typeof i === "string" ? i : i.url, "fetch"); } catch (e) {} return of.call(this, i, ...r); };
      const oo = XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.open = function (m, u, ...r) { try { post(u, "xhr"); } catch (e) {} return oo.call(this, m, u, ...r); };
      const scan = () => {
        document.querySelectorAll("video, audio, source").forEach(v => { const s = v.currentSrc || v.src; if (s && !s.startsWith("blob:")) post(s, "tag"); });
        try { performance.getEntriesByType("resource").forEach(e => post(e.name, "net")); } catch (e) {}
      };
      setInterval(scan, 1200); document.addEventListener("DOMContentLoaded", scan);
    })();
    """

    init(page: URL, onClose: @escaping (SnifferWindow) -> Void) {
        self.page = page
        self.onClose = onClose
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1040, height: 780),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        super.init()
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()  // same logins as the sign-in window
        cfg.mediaTypesRequiringUserActionForPlayback = []
        cfg.userContentController.addUserScript(WKUserScript(source: Self.script, injectionTime: .atDocumentStart, forMainFrameOnly: false))
        cfg.userContentController.add(self, name: "sniff")
        web = WKWebView(frame: .zero, configuration: cfg)
        web.customUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
        web.navigationDelegate = self
        web.uiDelegate = self
        window.title = "Find video – \(page.host ?? "page")"
        window.isReleasedWhenClosed = false
        window.delegate = self

        status.font = .systemFont(ofSize: 12)
        status.textColor = .secondaryLabelColor
        list.orientation = .vertical
        list.alignment = .leading
        list.spacing = 4
        let bottom = NSStackView(views: [status, list])
        bottom.orientation = .vertical
        bottom.alignment = .leading
        bottom.spacing = 6
        bottom.edgeInsets = NSEdgeInsets(top: 10, left: 12, bottom: 12, right: 12)
        let split = NSStackView(views: [web, bottom])
        split.orientation = .vertical
        split.spacing = 0
        web.translatesAutoresizingMaskIntoConstraints = false
        bottom.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = split
        NSLayoutConstraint.activate([
            web.widthAnchor.constraint(equalTo: split.widthAnchor),
            bottom.widthAnchor.constraint(equalTo: split.widthAnchor),
            bottom.heightAnchor.constraint(greaterThanOrEqualToConstant: 70),
        ])
        window.center()
    }

    func show() {
        web.load(URLRequest(url: page))
        window.makeKeyAndOrderFront(nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 25) { [weak self] in
            guard let self, self.found.isEmpty else { return }
            self.status.stringValue = "No video stream yet. Press play in the page. If it still finds nothing, the video may be DRM-protected or need a login (globe button)."
        }
    }

    func userContentController(_ ucc: WKUserContentController, didReceive msg: WKScriptMessage) {
        guard let b = msg.body as? [String: Any], let u = b["url"] as? String, !found.contains(where: { $0.url == u }) else { return }
        let lower = u.lowercased()
        let kind = lower.contains(".m3u8") || lower.contains("hls") ? "HLS stream"
            : lower.contains(".mpd") || lower.contains("dash") || lower.contains("/manifest") ? "DASH stream"
            : lower.contains(".mp3") || lower.contains(".m4a") ? "Audio file" : "Video file"
        let title = (b["title"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? web.title ?? page.host ?? "Video"
        found.append((u, kind, title, b["page"] as? String ?? page.absoluteString))
        // Best first: master playlists, then manifests, then files.
        found.sort { rank($0) < rank($1) }
        rebuild()
    }

    func rank(_ f: (url: String, kind: String, title: String, frame: String)) -> Int {
        let l = f.url.lowercased()
        if f.kind == "HLS stream" { return l.contains("master") || l.contains("playlist") ? 0 : 1 }
        if f.kind == "DASH stream" { return 2 }
        return f.kind == "Video file" ? 3 : 4
    }

    func rebuild() {
        list.arrangedSubviews.forEach { $0.removeFromSuperview() }
        status.stringValue = "Found \(found.count) stream\(found.count == 1 ? "" : "s"). The first one is usually the best quality."
        for (i, f) in found.prefix(6).enumerated() {
            let host = URL(string: f.url)?.host ?? ""
            let name = URL(string: f.url)?.lastPathComponent ?? ""
            let label = NSTextField(labelWithString: "\(f.kind) · \(host) · \(name)")
            label.lineBreakMode = .byTruncatingMiddle
            label.font = .systemFont(ofSize: 12)
            label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            let b = NSButton(title: i == 0 ? "Download (best)" : "Download", target: self, action: #selector(download(_:)))
            b.bezelStyle = .rounded
            b.tag = i
            if i == 0 { b.keyEquivalent = "\r" }
            let row = NSStackView(views: [b, label])
            row.spacing = 8
            list.addArrangedSubview(row)
        }
    }

    @objc func download(_ b: NSButton) {
        let f = found[b.tag]
        b.isEnabled = false
        b.title = "Sending…"
        exportAppCookies(web.configuration.websiteDataStore.httpCookieStore) { _, _ in
            let body: [String: Any] = [
                "url_list": [f.url],
                "overrides": ["referer": f.frame, "name_hint": f.title, "cookies": "app", "playlist": false],
            ]
            URLSession.shared.dataTask(with: apiRequest("api/download", method: "POST", timeout: 10, body: body)) { _, resp, _ in
                DispatchQueue.main.async {
                    let ok = (resp as? HTTPURLResponse)?.statusCode == 200
                    b.title = ok ? "Added to VD Plus ✓" : "Failed"
                    if ok { self.status.stringValue = "Downloading in VD Plus. You can close this window." }
                }
            }.resume()
        }
    }

    // Keep the whole page in this window (players that open popups).
    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for action: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if action.targetFrame == nil { w.load(action.request) }
        return nil
    }

    func windowWillClose(_ n: Notification) {
        web.configuration.userContentController.removeScriptMessageHandler(forName: "sniff")
        web.stopLoading()
        onClose(self)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
