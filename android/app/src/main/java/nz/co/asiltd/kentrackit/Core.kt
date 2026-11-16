package nz.co.asiltd.kentrackit

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.media.AudioAttributes
import android.os.Build
import android.webkit.CookieManager
import java.net.HttpURLConnection
import java.net.URL

/** Sets up the notification channels once, when the app process starts. */
class KtApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Channels.create(this)
    }
}

object Channels {
    const val SERVICE = "service"
    const val EMERGENCY = "emergency_v1"
    const val ALERTS = "alerts"
    const val UPDATES = "updates"
    const val ACCOUNT = "account"

    fun create(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        val emergency = NotificationChannel(EMERGENCY, "Emergencies", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "A unit's emergency button. Sounds an alarm until you silence it."
            // The app plays its own looping alarm, so the channel itself stays silent.
            setSound(null, null)
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 800, 400, 800, 400, 800)
            setBypassDnd(true)
            lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
        }
        val alerts = NotificationChannel(ALERTS, "Other alerts", NotificationManager.IMPORTANCE_DEFAULT).apply {
            description = "Overdue units, speeding, zones and other warnings."
        }
        val service = NotificationChannel(SERVICE, "Running in the background", NotificationManager.IMPORTANCE_LOW).apply {
            description = "Shown while KenTrackIt watches for emergencies or shares this phone's location."
            setShowBadge(false)
        }
        val updates = NotificationChannel(UPDATES, "App updates", NotificationManager.IMPORTANCE_DEFAULT)
        val account = NotificationChannel(ACCOUNT, "Sign-in", NotificationManager.IMPORTANCE_DEFAULT)
        nm.createNotificationChannels(listOf(emergency, alerts, service, updates, account))
    }
}

/** The app's settings, kept on the phone. */
class Prefs(context: Context) {
    private val p = context.applicationContext.getSharedPreferences("kentrackit", Context.MODE_PRIVATE)

    /** The KenTrackIt box, e.g. https://kentrackit.example.co.nz (no trailing slash). */
    var server: String
        get() = p.getString("server", "") ?: ""
        set(v) = p.edit().putString("server", v).apply()

    var sharing: Boolean
        get() = p.getBoolean("sharing", false)
        set(v) = p.edit().putBoolean("sharing", v).apply()

    /** Keep watching for emergencies while the app is closed. */
    var watchAlerts: Boolean
        get() = p.getBoolean("watch", true)
        set(v) = p.edit().putBoolean("watch", v).apply()

    /** Also notify for non-emergency warnings (overdue, speeding...). */
    var otherAlerts: Boolean
        get() = p.getBoolean("other_alerts", true)
        set(v) = p.edit().putBoolean("other_alerts", v).apply()

    /** Obey Talk-key broadcasts from PoC radios and rugged phones. */
    var hardwarePtt: Boolean
        get() = p.getBoolean("hardware_ptt", false)
        set(v) = p.edit().putBoolean("hardware_ptt", v).apply()

    /** A key chosen as the Talk key while the app is on screen. 0 = none. */
    var talkKey: Int
        get() = p.getInt("talk_key", 0)
        set(v) = p.edit().putInt("talk_key", v).apply()

    var intervalS: Int
        get() = p.getInt("interval_s", 15)
        set(v) = p.edit().putInt("interval_s", v).apply()

    /** The newest app build already offered, so the same update isn't offered over and over. */
    var offeredUpdate: Int
        get() = p.getInt("offered_update", 0)
        set(v) = p.edit().putInt("offered_update", v).apply()
}

/** Turning what someone types into a server address. */
object Urls {
    private val hostRe = Regex("^[A-Za-z0-9.-]+(:\\d{1,5})?$")

    /** "kentrackit.example.co.nz" -> "https://kentrackit.example.co.nz"; null if it can't be an address. */
    fun normalise(input: String): String? {
        var s = input.trim().trimEnd('/')
        if (s.isEmpty()) return null
        if (!s.contains("://")) {
            // a bare IP address or hostname with a port is most likely on the LAN, over http
            s = if (Regex("^\\d{1,3}(\\.\\d{1,3}){3}(:\\d+)?$").matches(s) || s.contains(':')) "http://$s" else "https://$s"
        }
        val scheme = s.substringBefore("://").lowercase()
        if (scheme != "http" && scheme != "https") return null
        val rest = s.substringAfter("://")
        val host = rest.substringBefore('/')
        if (!hostRe.matches(host)) return null
        val path = rest.substring(host.length).trimEnd('/')
        return "$scheme://$host$path"
    }

    fun sameHost(a: String, b: String): Boolean {
        fun host(u: String) = u.substringAfter("://").substringBefore('/').substringBefore(':').lowercase()
        return host(a) == host(b)
    }
}

/** Small HTTP calls to the box, signed in with the WebView's session cookie. */
object Api {
    data class Resp(val code: Int, val body: String)

    fun userAgent(): String = "KenTrackItApp/${BuildConfig.VERSION_NAME} (Android ${Build.VERSION.RELEASE})"

    fun cookie(base: String): String? = try {
        CookieManager.getInstance().getCookie(base)
    } catch (e: Exception) {
        null
    }

    fun request(ctx: Context, method: String, path: String, json: String? = null, timeoutMs: Int = 15000): Resp {
        val base = Prefs(ctx).server
        val c = URL(base + path).openConnection() as HttpURLConnection
        try {
            c.requestMethod = method
            c.connectTimeout = timeoutMs
            c.readTimeout = timeoutMs
            c.setRequestProperty("User-Agent", userAgent())
            cookie(base)?.let { c.setRequestProperty("Cookie", it) }
            if (json != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(json.toByteArray()) }
            }
            val code = c.responseCode
            val stream = if (code < 400) c.inputStream else c.errorStream
            val body = stream?.bufferedReader()?.use { it.readText() } ?: ""
            return Resp(code, body)
        } finally {
            c.disconnect()
        }
    }
}

/** Server-sent events (text/event-stream), one line at a time. */
class SseParser(private val onEvent: (event: String, data: String) -> Unit) {
    private var event = "message"
    private val data = StringBuilder()

    fun line(raw: String) {
        val l = raw.trimEnd('\r')
        if (l.isEmpty()) {
            if (data.isNotEmpty()) onEvent(event, data.toString())
            event = "message"
            data.setLength(0)
            return
        }
        if (l.startsWith(":")) return // a keep-alive comment
        val i = l.indexOf(':')
        val field = if (i < 0) l else l.substring(0, i)
        var value = if (i < 0) "" else l.substring(i + 1)
        if (value.startsWith(" ")) value = value.substring(1)
        when (field) {
            "event" -> event = value
            "data" -> {
                if (data.isNotEmpty()) data.append('\n')
                data.append(value)
            }
        }
    }
}
