package nz.co.asiltd.kentrackit

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.app.DownloadManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import android.os.Environment
import android.provider.Settings
import android.widget.Toast
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject

/**
 * App updates come from the KenTrackIt box itself: an administrator uploads a new APK in
 * Settings → Phone app, and phones are told it's there.
 */
object Updater {
    const val APK_MIME = "application/vnd.android.package-archive"

    fun isNewer(remote: Int, local: Int = BuildConfig.VERSION_CODE) = remote > local

    /** Asks the box what version it holds; offers it if newer. [manual]: say so when there's nothing new. */
    fun check(act: Activity, manual: Boolean) {
        Thread {
            val info = try {
                val r = Api.request(act, "GET", "/api/app/info")
                if (r.code == 200) JSONObject(r.body) else null
            } catch (e: Exception) {
                null
            }
            act.runOnUiThread {
                if (act.isFinishing) return@runOnUiThread
                val code = info?.optInt("version_code") ?: 0
                val prefs = Prefs(act)
                if (info != null && info.optBoolean("available") && isNewer(code)) {
                    if (manual || code > prefs.offeredUpdate) {
                        prefs.offeredUpdate = code
                        offer(act, info.optString("version_name"))
                    }
                } else if (manual) {
                    Toast.makeText(act, if (info == null) "Couldn't reach the KenTrackIt box" else
                        "You have the latest version (${BuildConfig.VERSION_NAME})", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    private fun offer(act: Activity, name: String) {
        AlertDialog.Builder(act)
            .setTitle("Update available")
            .setMessage("KenTrackIt $name is ready on your KenTrackIt system. You have ${BuildConfig.VERSION_NAME}.")
            .setPositiveButton("Update now") { _, _ -> download(act) }
            .setNegativeButton("Later", null)
            .show()
    }

    /** From the background watcher, when an administrator uploads a new version. */
    @SuppressLint("MissingPermission")
    fun notify(ctx: Context, info: JSONObject) {
        if (!isNewer(info.optInt("version_code"))) return
        val open = PendingIntent.getActivity(ctx, 3, Intent(ctx, MainActivity::class.java).putExtra("update", true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(ctx, Channels.UPDATES)
            .setSmallIcon(R.drawable.ic_stat_kt)
            .setContentTitle("KenTrackIt update available")
            .setContentText("Version ${info.optString("version_name")} is ready. Tap to install.")
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        if (notificationsAllowed(ctx)) NotificationManagerCompat.from(ctx).notify(WatchService.NOTE_UPDATE, n)
    }

    private var pending = -1L

    fun download(act: Activity) {
        val prefs = Prefs(act)
        val dm = act.getSystemService(DownloadManager::class.java)
        val req = DownloadManager.Request(Uri.parse(prefs.server + "/app/kentrackit.apk"))
            .setTitle("KenTrackIt update")
            .setMimeType(APK_MIME)
            .addRequestHeader("User-Agent", Api.userAgent())
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE)
            .setDestinationInExternalFilesDir(act, Environment.DIRECTORY_DOWNLOADS, "KenTrackIt-update.apk")
        Api.cookie(prefs.server)?.let { req.addRequestHeader("Cookie", it) }
        act.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)?.resolve("KenTrackIt-update.apk")?.delete()
        pending = dm.enqueue(req)
        Toast.makeText(act, "Downloading the update…", Toast.LENGTH_SHORT).show()
        val app = act.applicationContext
        ContextCompat.registerReceiver(app, object : BroadcastReceiver() {
            override fun onReceive(c: Context, i: Intent) {
                val id = i.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1)
                if (id != pending) return
                app.unregisterReceiver(this)
                install(app, dm, id)
            }
        }, IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE), ContextCompat.RECEIVER_EXPORTED)
    }

    private fun install(ctx: Context, dm: DownloadManager, id: Long) {
        val q = dm.query(DownloadManager.Query().setFilterById(id))
        val ok = q.use { it.moveToFirst() && it.getInt(it.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS)) == DownloadManager.STATUS_SUCCESSFUL }
        if (!ok) {
            Toast.makeText(ctx, "The update didn't download. Try again from Phone settings.", Toast.LENGTH_LONG).show()
            return
        }
        if (!ctx.packageManager.canRequestPackageInstalls()) {
            Toast.makeText(ctx, "Allow KenTrackIt to install updates, then tap Check for updates again.", Toast.LENGTH_LONG).show()
            ctx.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + ctx.packageName))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return
        }
        val uri = dm.getUriForDownloadedFile(id) ?: return
        ctx.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, APK_MIME)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
    }
}
