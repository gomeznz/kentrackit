package nz.co.asiltd.kentrackit

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.view.KeyEvent
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat

/**
 * KenTrackIt's own web pages (map, Dispatch, reports...) in a WebView, so the app always matches
 * the server, plus what a browser can't do: hardware Talk keys, background alarms, location sharing.
 */
class MainActivity : Activity() {
    companion object {
        private const val RC_MIC = 10
        private const val RC_FILE = 11
        private const val RC_NOTIFY = 12
    }

    private lateinit var prefs: Prefs
    private lateinit var web: WebView
    private var pendingMic: PermissionRequest? = null
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var talking = false
    private var receiverOn = false

    private val hardwareReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (HardwareButtons.classify(intent)) {
                HardwareButtons.Event.PTT_DOWN -> ptt(true)
                HardwareButtons.Event.PTT_UP -> ptt(false)
                else -> {}
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        if (prefs.server.isEmpty()) {
            startActivity(Intent(this, SetupActivity::class.java))
            finish()
            return
        }
        WindowCompat.getInsetsController(window, window.decorView).isAppearanceLightStatusBars = false

        val root = FrameLayout(this).apply {
            setBackgroundColor(Color.rgb(11, 27, 51))
            fitsSystemWindows = true
        }
        web = WebView(this)
        root.addView(web, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(root)

        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false)
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            userAgentString = "$userAgentString ${Api.userAgent()}"
            builtInZoomControls = false
        }
        web.addJavascriptInterface(Bridge(), "KTAndroid")
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url.toString()
                if (Urls.sameHost(url, prefs.server)) return false
                // anything else (map credits, help links) opens in the phone's browser
                try { startActivity(Intent(Intent.ACTION_VIEW, request.url)) } catch (e: Exception) { }
                return true
            }

            override fun onPageFinished(view: WebView, url: String) {
                CookieManager.getInstance().flush()
                // just signed in: the background watcher can now connect
                if (!url.contains("/login") && !url.contains("/setup")) WatchService.sync(this@MainActivity)
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread {
                    if (!Urls.sameHost(request.origin.toString(), prefs.server) ||
                        !request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)) {
                        request.deny()
                    } else if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                        request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                    } else {
                        pendingMic = request
                        requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), RC_MIC)
                    }
                }
            }

            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                return try {
                    @Suppress("DEPRECATION")
                    startActivityForResult(params.createIntent(), RC_FILE)
                    true
                } catch (e: Exception) {
                    fileCallback = null
                    false
                }
            }
        }
        web.setDownloadListener { url, ua, disposition, mime, _ ->
            // CSV exports, backups, recordings: saved to Downloads with this phone's sign-in
            val name = URLUtil.guessFileName(url, disposition, mime)
            val req = DownloadManager.Request(Uri.parse(url))
                .setMimeType(mime)
                .addRequestHeader("User-Agent", ua)
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
            Api.cookie(url)?.let { req.addRequestHeader("Cookie", it) }
            getSystemService(DownloadManager::class.java).enqueue(req)
            Toast.makeText(this, "Downloading $name", Toast.LENGTH_SHORT).show()
        }

        if (savedInstanceState != null) web.restoreState(savedInstanceState) else web.loadUrl(prefs.server + "/")
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), RC_NOTIFY)
        }
        WatchService.sync(this)
        handleIntent(intent)
        Updater.check(this, manual = false)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(i: Intent?) {
        if (i == null) return
        if (i.getBooleanExtra("silence", false)) Alarm.silence(this)
        if (i.getBooleanExtra("update", false)) Updater.check(this, manual = true)
        i.getStringExtra("path")?.let { if (it.startsWith("/")) web.loadUrl(prefs.server + it) }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        if (::web.isInitialized) web.saveState(outState)
    }

    override fun onResume() {
        super.onResume()
        if (!::web.isInitialized) return
        web.onResume()
        if (prefs.hardwarePtt && !receiverOn) {
            ContextCompat.registerReceiver(this, hardwareReceiver, HardwareButtons.filter(), ContextCompat.RECEIVER_EXPORTED)
            receiverOn = true
        }
    }

    override fun onPause() {
        if (::web.isInitialized) {
            if (talking) ptt(false)
            web.onPause()
        }
        if (receiverOn) {
            unregisterReceiver(hardwareReceiver)
            receiverOn = false
        }
        super.onPause()
    }

    override fun onDestroy() {
        if (::web.isInitialized) web.destroy()
        super.onDestroy()
    }

    /** The chosen Talk key, whatever it is (a PTT key, F-key, volume key, a Bluetooth mic's button). */
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        val key = prefs.talkKey
        if (key != 0 && event.keyCode == key && ::web.isInitialized) {
            if (event.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0) ptt(true)
            if (event.action == KeyEvent.ACTION_UP) ptt(false)
            return true
        }
        return super.dispatchKeyEvent(event)
    }

    /** Talk on the Dispatch page; from anywhere else, open it first. */
    private fun ptt(on: Boolean) {
        if (on == talking) return
        talking = on
        web.evaluateJavascript("(window.KTPtt ? KTPtt($on) : false)") { result ->
            if (on && result != "true") {
                talking = false
                web.loadUrl(prefs.server + "/console")
                Toast.makeText(this, "Dispatch opened. Press Talk again to speak.", Toast.LENGTH_SHORT).show()
            }
        }
    }

    @Deprecated("Still the simplest way to step back through the web pages")
    override fun onBackPressed() {
        if (::web.isInitialized && web.canGoBack()) web.goBack() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == RC_MIC) {
            val req = pendingMic ?: return
            pendingMic = null
            if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
                req.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
            } else {
                req.deny()
                Toast.makeText(this, "KenTrackIt needs the microphone to talk on the Dispatch page.", Toast.LENGTH_LONG).show()
            }
        }
    }

    @Deprecated("Used for the web page's file picker")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == RC_FILE) {
            fileCallback?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data))
            fileCallback = null
        }
    }

    /** What the KenTrackIt pages can ask of the app (window.KTAndroid). */
    inner class Bridge {
        @JavascriptInterface fun isApp(): Boolean = true
        @JavascriptInterface fun appVersion(): String = BuildConfig.VERSION_NAME
        @JavascriptInterface fun sharingLocation(): Boolean = prefs.sharing
        @JavascriptInterface fun openSettings() {
            runOnUiThread { startActivity(Intent(this@MainActivity, SettingsActivity::class.java)) }
        }
    }
}
