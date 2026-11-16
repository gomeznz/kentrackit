package nz.co.asiltd.kentrackit

import android.app.Activity
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.view.Gravity
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

/** An emergency on the phone: a full-screen alarm and a looping alarm sound until someone silences it. */
object Alarm {
    const val ACTION_SILENCE = "nz.co.asiltd.kentrackit.SILENCE"
    private const val MAX_RING_MS = 5 * 60 * 1000L

    private var player: MediaPlayer? = null
    private val active = mutableSetOf<Int>()
    private val main = Handler(Looper.getMainLooper())

    fun noteId(alertId: Int) = 1000 + alertId

    @Synchronized
    fun raise(ctx: Context, alertId: Int, title: String, text: String) {
        val app = ctx.applicationContext
        active += alertId
        val full = PendingIntent.getActivity(
            app, alertId,
            Intent(app, AlarmActivity::class.java)
                .putExtra("title", title).putExtra("text", text).putExtra("id", alertId)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val open = PendingIntent.getActivity(
            app, 0, Intent(app, MainActivity::class.java).putExtra("silence", true)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val silence = PendingIntent.getBroadcast(
            app, 1, Intent(app, AlarmReceiver::class.java).setAction(ACTION_SILENCE),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val n = NotificationCompat.Builder(app, Channels.EMERGENCY)
            .setSmallIcon(R.drawable.ic_stat_kt)
            .setColor(0xFFDC2626.toInt())
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setFullScreenIntent(full, true)
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(0, "Silence", silence)
            .addAction(0, "Open map", open)
            .build()
        try {
            NotificationManagerCompat.from(app).notify(noteId(alertId), n)
        } catch (e: SecurityException) {
            // notifications not allowed: the full-screen screen can still show
            app.startActivity(Intent(app, AlarmActivity::class.java)
                .putExtra("title", title).putExtra("text", text).putExtra("id", alertId)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
        startSound(app)
    }

    @Synchronized
    fun startSound(ctx: Context) {
        if (player != null) return
        val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE) ?: return
        try {
            player = MediaPlayer().apply {
                setAudioAttributes(AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                setDataSource(ctx, uri)
                isLooping = true
                prepare()
                start()
            }
        } catch (e: Exception) {
            player = null
        }
        vibrator(ctx)?.let {
            val pattern = longArrayOf(0, 800, 400, 800, 400, 800, 1200)
            it.vibrate(VibrationEffect.createWaveform(pattern, 0))
        }
        main.postDelayed({ silence(ctx) }, MAX_RING_MS)
    }

    @Synchronized
    fun silence(ctx: Context) {
        player?.let {
            try { it.stop() } catch (e: Exception) { }
            it.release()
        }
        player = null
        vibrator(ctx)?.cancel()
        main.removeCallbacksAndMessages(null)
        // the test alarm (id 0) has nothing on the server to acknowledge it: silencing ends it
        NotificationManagerCompat.from(ctx).cancel(noteId(0))
        active -= 0
    }

    /** The emergency was acknowledged on KenTrackIt (by anyone): take it off this phone. */
    @Synchronized
    fun acknowledged(ctx: Context, alertId: Int) {
        NotificationManagerCompat.from(ctx).cancel(noteId(alertId))
        active -= alertId
        if (active.isEmpty()) silence(ctx)
    }

    @Suppress("DEPRECATION")
    private fun vibrator(ctx: Context): Vibrator? = ctx.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
}

class AlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Alarm.ACTION_SILENCE) Alarm.silence(context)
    }
}

/** Shown over the lock screen when an emergency comes in. */
class AlarmActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        show(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        show(intent)
    }

    private fun show(i: Intent) {
        val dp = resources.displayMetrics.density
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.rgb(153, 27, 27))
            setPadding((28 * dp).toInt(), (48 * dp).toInt(), (28 * dp).toInt(), (48 * dp).toInt())
            fitsSystemWindows = true
        }
        fun text(s: String, size: Float, bold: Boolean = false) = TextView(this).apply {
            text = s
            textSize = size
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
            if (bold) typeface = Typeface.DEFAULT_BOLD
            setPadding(0, (8 * dp).toInt(), 0, (8 * dp).toInt())
        }
        root.addView(text("EMERGENCY", 40f, true))
        root.addView(text(i.getStringExtra("title") ?: "", 24f, true))
        root.addView(text(i.getStringExtra("text") ?: "", 18f))
        fun button(label: String, onClick: () -> Unit) = Button(this).apply {
            text = label
            textSize = 18f
            setOnClickListener { onClick() }
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, (64 * dp).toInt()).apply {
                topMargin = (16 * dp).toInt()
            }
        }
        root.addView(button("Open the map") {
            Alarm.silence(this)
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            finish()
        })
        root.addView(button("Silence") {
            Alarm.silence(this)
            finish()
        })
        setContentView(root)
    }
}

/** Restarts the background watcher after the phone restarts or the app is updated. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        WatchService.sync(context)
    }
}

internal fun notificationsAllowed(ctx: Context): Boolean =
    ctx.getSystemService(NotificationManager::class.java).areNotificationsEnabled()
