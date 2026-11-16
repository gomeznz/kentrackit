package nz.co.asiltd.kentrackit

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.KeyEvent
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import org.json.JSONObject

/** The phone's own settings: what the web pages can't control. */
@SuppressLint("UseSwitchCompatOrMaterialCode")
class SettingsActivity : Activity() {
    companion object {
        private const val RC_LOCATION = 20
        private const val RC_BACKGROUND = 21
    }

    private lateinit var prefs: Prefs
    private lateinit var col: LinearLayout
    private lateinit var shareSwitch: Switch
    private lateinit var shareNote: TextView
    private lateinit var keyLabel: TextView
    private var learning: AlertDialog? = null
    private var dp = 1f

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        dp = resources.displayMetrics.density
        col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(px(20), px(16), px(20), px(32))
        }
        setContentView(ScrollView(this).apply {
            setBackgroundColor(Color.rgb(11, 27, 51))
            fitsSystemWindows = true
            addView(col)
        })
        build()
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun px(v: Int) = (v * dp).toInt()

    private fun heading(s: String) = col.addView(TextView(this).apply {
        text = s
        textSize = 13f
        typeface = Typeface.DEFAULT_BOLD
        setTextColor(Color.rgb(45, 212, 191))
        setPadding(0, px(22), 0, px(6))
        isAllCaps = true
    })

    private fun note(s: String) = TextView(this).apply {
        text = s
        textSize = 13f
        setTextColor(Color.rgb(148, 163, 184))
        setPadding(0, 0, 0, px(8))
    }.also { col.addView(it) }

    private fun switch(label: String, checked: Boolean, onChange: (Switch, Boolean) -> Unit) = Switch(this).apply {
        text = label
        textSize = 16f
        setTextColor(Color.WHITE)
        isChecked = checked
        setPadding(0, px(8), 0, px(8))
        setOnCheckedChangeListener { v, on -> onChange(v as Switch, on) }
    }.also { col.addView(it) }

    private fun button(label: String, onClick: () -> Unit) = Button(this).apply {
        text = label
        isAllCaps = false
        setOnClickListener { onClick() }
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
            topMargin = px(4)
        }
    }.also { col.addView(it) }

    private fun build() {
        col.addView(TextView(this).apply {
            text = "Phone settings"
            textSize = 24f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.WHITE)
        })

        heading("Location")
        shareSwitch = switch("Share this phone's location", prefs.sharing) { v, on ->
            if (on) startSharing(v) else setSharing(false)
        }
        shareNote = note("")

        heading("Alerts")
        switch("Emergency alarms", prefs.watchAlerts) { _, on ->
            prefs.watchAlerts = on
            WatchService.sync(this)
        }
        note("A loud alarm over the lock screen when a unit presses its emergency button, even while the app is closed.")
        switch("Other alerts", prefs.otherAlerts) { _, on -> prefs.otherAlerts = on }
        note("Notifications for overdue units, speeding, zones and other warnings.")
        if (!fullScreenAllowed()) {
            button("Allow full-screen emergency alarms") {
                tryStart(Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT, Uri.parse("package:$packageName")))
            }
        }
        button("Test the alarm") {
            Alarm.raise(this, 0, "Test alarm", "This is how an emergency looks on this phone. Tap Silence to stop it.")
        }

        heading("Talk")
        switch("Use the radio's PTT button", prefs.hardwarePtt) { _, on -> prefs.hardwarePtt = on }
        note("For PoC radios and rugged phones with a PTT key (Inrico, Hytera, Sonim, Kyocera…). Works while KenTrackIt is on screen.")
        keyLabel = note("")
        button("Choose a Talk key") { learnKey() }

        heading("Keep running")
        button("Don't let battery saving stop KenTrackIt") { askBattery() }
        note("Without this, some phones stop the emergency watch and location sharing a while after the screen goes off.")

        heading("App")
        button("Check for updates") { Updater.check(this, manual = true) }
        button("Change server") {
            startActivity(Intent(this, SetupActivity::class.java))
        }
        note("Version ${BuildConfig.VERSION_NAME} · ${prefs.server}")
    }

    private fun refresh() {
        if (!::shareSwitch.isInitialized) return
        val noBackground = Build.VERSION.SDK_INT >= 29 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_BACKGROUND_LOCATION) != PackageManager.PERMISSION_GRANTED
        shareSwitch.setOnCheckedChangeListener(null)
        shareSwitch.isChecked = prefs.sharing
        shareSwitch.setOnCheckedChangeListener { v, on -> if (on) startSharing(v as Switch) else setSharing(false) }
        shareNote.text = when {
            !prefs.sharing -> "Show this phone on the KenTrackIt map as its own unit, with your name."
            !WatchService.hasLocation(this) -> "Location permission is off: nothing is being sent."
            noBackground -> "Only sent while KenTrackIt is open. For all the time, set Location to \"Allow all the time\" in the app's permissions."
            else -> "On. Your position is sent every ${prefs.intervalS} s, also while the app is closed."
        }
        val key = prefs.talkKey
        keyLabel.text = if (key == 0) "Talk key: none" else "Talk key: ${HardwareButtons.keyName(key)} (hold to talk on the Dispatch page)"
    }

    // --- location sharing ---

    private fun startSharing(v: Switch) {
        if (!WatchService.hasLocation(this)) {
            v.isChecked = false
            requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION), RC_LOCATION)
            return
        }
        setSharing(true)
        askBackground()
    }

    private fun askBackground() {
        if (Build.VERSION.SDK_INT < 29) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_BACKGROUND_LOCATION) == PackageManager.PERMISSION_GRANTED) return
        AlertDialog.Builder(this)
            .setTitle("Share while the app is closed?")
            .setMessage("To keep sending this phone's position with the screen off, choose \"Allow all the time\" on the next screen.")
            .setPositiveButton("Continue") { _, _ ->
                requestPermissions(arrayOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION), RC_BACKGROUND)
            }
            .setNegativeButton("Only while open", null)
            .show()
    }

    private fun setSharing(on: Boolean) {
        Thread {
            val r = try {
                Api.request(this, "POST", "/api/mobile/sharing", JSONObject().put("on", on).toString())
            } catch (e: Exception) {
                null
            }
            runOnUiThread {
                when {
                    r == null && on -> fail("Couldn't reach the KenTrackIt box.")
                    r != null && r.code == 403 -> fail("Phone tracking is turned off on this KenTrackIt system.")
                    r != null && r.code == 401 -> fail("Sign in first, then turn this on.")
                    else -> {
                        prefs.sharing = on
                        WatchService.sync(this)
                    }
                }
                refresh()
            }
        }.start()
    }

    private fun fail(msg: String) {
        prefs.sharing = false
        WatchService.sync(this)
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == RC_LOCATION) {
            if (grantResults.any { it == PackageManager.PERMISSION_GRANTED }) {
                setSharing(true)
                askBackground()
            } else {
                Toast.makeText(this, "Location permission is needed to share this phone's position.", Toast.LENGTH_LONG).show()
            }
        }
        if (requestCode == RC_BACKGROUND) {
            WatchService.sync(this) // picks up the new permission
            refresh()
        }
    }

    // --- the Talk key ---

    private fun learnKey() {
        val msg = TextView(this).apply {
            text = "Press the button you want to use as the Talk key.\n\nBack cancels."
            textSize = 16f
            gravity = Gravity.CENTER
            setPadding(px(24), px(24), px(24), px(24))
        }
        learning = AlertDialog.Builder(this)
            .setTitle("Choose a Talk key")
            .setView(msg)
            .setNeutralButton("No Talk key") { _, _ ->
                prefs.talkKey = 0
                refresh()
            }
            .setNegativeButton("Cancel", null)
            .setOnDismissListener { learning = null }
            .create().apply {
                setOnKeyListener { d, code, ev ->
                    if (code == KeyEvent.KEYCODE_BACK) return@setOnKeyListener false
                    if (ev.action != KeyEvent.ACTION_UP) return@setOnKeyListener true
                    if (!HardwareButtons.canBeTalkKey(code)) {
                        Toast.makeText(this@SettingsActivity, "${HardwareButtons.keyName(code)} can't be the Talk key.", Toast.LENGTH_SHORT).show()
                        return@setOnKeyListener true
                    }
                    prefs.talkKey = code
                    refresh()
                    d.dismiss()
                    true
                }
            }
        learning?.show()
    }

    // --- battery ---

    @SuppressLint("BatteryLife")
    private fun askBattery() {
        val pm = getSystemService(PowerManager::class.java)
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            Toast.makeText(this, "Already done: battery saving won't stop KenTrackIt.", Toast.LENGTH_SHORT).show()
            return
        }
        if (!tryStart(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))) {
            tryStart(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    private fun tryStart(i: Intent): Boolean = try {
        startActivity(i)
        true
    } catch (e: Exception) {
        false
    }

    private fun fullScreenAllowed(): Boolean =
        Build.VERSION.SDK_INT < 34 || getSystemService(NotificationManager::class.java).canUseFullScreenIntent()
}
