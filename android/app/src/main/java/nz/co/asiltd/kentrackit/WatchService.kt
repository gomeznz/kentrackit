package nz.co.asiltd.kentrackit

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * Runs while the app is closed, for two jobs the web pages can't do on their own:
 *  - watching KenTrackIt's live alerts, so an emergency sounds an alarm on the phone, and
 *  - sharing this phone's location, so it shows on the map like a radio.
 */
class WatchService : Service() {
    companion object {
        private const val NOTE_SERVICE = 1
        const val NOTE_SIGNIN = 2
        const val NOTE_UPDATE = 3
        private const val NOTE_TRACKING_OFF = 4

        /** Start, update or stop the service to match the settings. */
        fun sync(ctx: Context) {
            val p = Prefs(ctx)
            val want = p.server.isNotEmpty() && (p.watchAlerts || p.sharing)
            val i = Intent(ctx, WatchService::class.java)
            if (!want) {
                ctx.stopService(i)
                return
            }
            try {
                ContextCompat.startForegroundService(ctx, i)
            } catch (e: Exception) {
                // e.g. not allowed to start from the background right now: it starts when the app opens
            }
        }

        fun hasLocation(ctx: Context) =
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED ||
                ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED
    }

    private lateinit var prefs: Prefs
    @Volatile private var running = false
    private var sseThread: Thread? = null
    private var streamConn: HttpURLConnection? = null
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var lm: LocationManager? = null
    private var tracking = false
    private var lastSent = 0L
    private var intervalMs = 15_000L
    private var signInShown = false

    private val listener = LocationListener { loc -> onLocation(loc) }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        intervalMs = prefs.intervalS * 1000L
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        goForeground()
        if (prefs.watchAlerts) startStream() else stopStream()
        if (prefs.sharing && hasLocation(this)) startTracking() else stopTracking()
        if (!prefs.watchAlerts && !(prefs.sharing && hasLocation(this))) stopSelf()
        return START_STICKY
    }

    override fun onDestroy() {
        stopStream()
        stopTracking()
        io.shutdownNow()
        super.onDestroy()
    }

    // ---- foreground notification ----------------------------------------------------------------
    private fun goForeground() {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val what = listOfNotNull(
            if (prefs.watchAlerts) "watching for emergencies" else null,
            if (prefs.sharing && hasLocation(this)) "sharing this phone's location" else null,
        ).joinToString(" · ").replaceFirstChar { it.uppercase() }
        val n: Notification = NotificationCompat.Builder(this, Channels.SERVICE)
            .setSmallIcon(R.drawable.ic_stat_kt)
            .setContentTitle("KenTrackIt")
            .setContentText(what.ifEmpty { "Starting" })
            .setContentIntent(open)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
        var types = 0
        if (Build.VERSION.SDK_INT >= 34) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_REMOTE_MESSAGING
        if (prefs.sharing && hasLocation(this) && Build.VERSION.SDK_INT >= 29) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        try {
            ServiceCompat.startForeground(this, NOTE_SERVICE, n, types)
        } catch (e: Exception) {
            // location type refused (e.g. started from the background without "Allow all the time"): carry on without it
            val fallback = if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_REMOTE_MESSAGING else 0
            try { ServiceCompat.startForeground(this, NOTE_SERVICE, n, fallback) } catch (e2: Exception) { stopSelf() }
            tracking = false
        }
    }

    // ---- live alerts ------------------------------------------------------------------------------
    private fun startStream() {
        if (sseThread?.isAlive == true) return
        running = true
        sseThread = Thread({ streamLoop() }, "kt-alerts").apply { isDaemon = true; start() }
    }

    private fun stopStream() {
        running = false
        try { streamConn?.disconnect() } catch (e: Exception) { }
        sseThread?.interrupt()
        sseThread = null
    }

    private fun streamLoop() {
        var backoff = 2_000L
        while (running) {
            try {
                val base = prefs.server
                val c = URL("$base/api/stream?events=alert,alert_ack,signed_out,app_update").openConnection() as HttpURLConnection
                streamConn = c
                c.connectTimeout = 15_000
                c.readTimeout = 45_000 // the box sends a keep-alive every 15 s
                c.setRequestProperty("Accept", "text/event-stream")
                c.setRequestProperty("User-Agent", Api.userAgent())
                Api.cookie(base)?.let { c.setRequestProperty("Cookie", it) }
                val code = c.responseCode
                if (code == 401) {
                    showSignIn()
                    sleep(60_000)
                    continue
                }
                if (code != 200) throw java.io.IOException("HTTP $code")
                backoff = 2_000L
                clearSignIn()
                val parser = SseParser { ev, data -> onEvent(ev, data) }
                c.inputStream.bufferedReader().use { r ->
                    while (running) {
                        val line = r.readLine() ?: break
                        parser.line(line)
                    }
                }
            } catch (e: Exception) {
                // dropped or unreachable: try again shortly
            }
            if (!running) break
            sleep(backoff)
            backoff = minOf(backoff * 2, 60_000L)
        }
    }

    private fun sleep(ms: Long) {
        try { Thread.sleep(ms) } catch (e: InterruptedException) { }
    }

    private fun onEvent(event: String, data: String) {
        try {
            when (event) {
                "alert" -> {
                    val a = JSONObject(data)
                    val id = a.optInt("id")
                    val detail = a.optString("detail")
                    when (a.optString("severity")) {
                        "critical" -> Alarm.raise(this, id, "Emergency", detail)
                        "warn" -> if (prefs.otherAlerts) notifyAlert(id, a.optString("kind"), detail)
                    }
                }
                "alert_ack" -> {
                    val ids = JSONObject(data).optJSONArray("ids") ?: return
                    for (i in 0 until ids.length()) {
                        Alarm.acknowledged(this, ids.getInt(i))
                    }
                }
                "signed_out" -> showSignIn()
                "app_update" -> Updater.notify(this, JSONObject(data))
            }
        } catch (e: Exception) {
            // a message we don't understand: ignore it
        }
    }

    @SuppressLint("MissingPermission")
    private fun notifyAlert(id: Int, kind: String, detail: String) {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val title = when (kind) {
            "overdue" -> "Unit not reporting"
            "speed" -> "Speeding"
            "status" -> "Status change"
            "channel" -> "Channel problem"
            else -> "KenTrackIt alert"
        }
        val n = NotificationCompat.Builder(this, Channels.ALERTS)
            .setSmallIcon(R.drawable.ic_stat_kt)
            .setContentTitle(title)
            .setContentText(detail)
            .setStyle(NotificationCompat.BigTextStyle().bigText(detail))
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        if (notificationsAllowed(this)) NotificationManagerCompat.from(this).notify(Alarm.noteId(id), n)
    }

    @SuppressLint("MissingPermission")
    private fun showSignIn() {
        if (signInShown) return
        signInShown = true
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(this, Channels.ACCOUNT)
            .setSmallIcon(R.drawable.ic_stat_kt)
            .setContentTitle("Sign in to KenTrackIt")
            .setContentText("You've been signed out, so emergencies can't reach this phone. Tap to sign in.")
            .setStyle(NotificationCompat.BigTextStyle().bigText("You've been signed out, so emergencies can't reach this phone. Tap to sign in."))
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        if (notificationsAllowed(this)) NotificationManagerCompat.from(this).notify(NOTE_SIGNIN, n)
    }

    private fun clearSignIn() {
        if (!signInShown) return
        signInShown = false
        NotificationManagerCompat.from(this).cancel(NOTE_SIGNIN)
    }

    // ---- sharing this phone's location ----------------------------------------------------------
    @SuppressLint("MissingPermission")
    private fun startTracking() {
        if (tracking) return
        val m = getSystemService(LocationManager::class.java) ?: return
        lm = m
        val providers = if (Build.VERSION.SDK_INT >= 31 && m.hasProvider(LocationManager.FUSED_PROVIDER)) {
            listOf(LocationManager.FUSED_PROVIDER)
        } else {
            listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER).filter { m.allProviders.contains(it) }
        }
        try {
            for (p in providers) m.requestLocationUpdates(p, intervalMs, 0f, listener, Looper.getMainLooper())
            tracking = true
        } catch (e: SecurityException) {
            tracking = false
        }
    }

    private fun stopTracking() {
        try { lm?.removeUpdates(listener) } catch (e: Exception) { }
        tracking = false
    }

    private fun onLocation(loc: Location) {
        val now = System.currentTimeMillis()
        if (now - lastSent < intervalMs - 1_000) return
        lastSent = now
        val body = JSONObject().apply {
            put("lat", loc.latitude)
            put("lon", loc.longitude)
            if (loc.hasSpeed()) put("speed", loc.speed.toDouble())
            if (loc.hasBearing()) put("course", loc.bearing.toDouble())
            if (loc.hasAccuracy()) put("accuracy", loc.accuracy.toDouble())
            put("time", loc.time)
        }.toString()
        io.execute {
            try {
                val r = Api.request(this, "POST", "/api/mobile/position", body)
                when (r.code) {
                    200 -> {
                        val s = JSONObject(r.body).optInt("interval_s", prefs.intervalS)
                        if (s > 0 && s != prefs.intervalS) main.post { changeInterval(s) }
                    }
                    401 -> showSignIn()
                    403 -> main.post { trackingTurnedOff() }
                }
            } catch (e: Exception) {
                // offline: the next fix tries again
            }
        }
    }

    private fun changeInterval(s: Int) {
        prefs.intervalS = s
        intervalMs = s * 1000L
        stopTracking()
        startTracking()
    }

    @SuppressLint("MissingPermission")
    private fun trackingTurnedOff() {
        prefs.sharing = false
        stopTracking()
        val n = NotificationCompat.Builder(this, Channels.ACCOUNT)
            .setSmallIcon(R.drawable.ic_stat_kt)
            .setContentTitle("Location sharing stopped")
            .setContentText("Phone tracking has been turned off on the KenTrackIt system.")
            .setAutoCancel(true)
            .build()
        if (notificationsAllowed(this)) NotificationManagerCompat.from(this).notify(NOTE_TRACKING_OFF, n)
        goForeground()
        if (!prefs.watchAlerts) stopSelf()
    }
}
