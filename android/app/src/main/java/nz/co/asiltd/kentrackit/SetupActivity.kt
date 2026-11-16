package nz.co.asiltd.kentrackit

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.net.HttpURLConnection
import java.net.URL

/** First run (or "Change server"): where is the KenTrackIt box? */
class SetupActivity : Activity() {
    private lateinit var input: EditText
    private lateinit var status: TextView
    private lateinit var go: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val dp = resources.displayMetrics.density
        fun px(v: Int) = (v * dp).toInt()
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(px(28), px(48), px(28), px(32))
        }
        col.addView(ImageView(this).apply {
            setImageResource(R.drawable.logo)
            adjustViewBounds = true
            layoutParams = LinearLayout.LayoutParams(px(96), px(96))
        })
        col.addView(TextView(this).apply {
            text = "KenTrackIt"
            textSize = 28f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
            setPadding(0, px(12), 0, px(4))
        })
        col.addView(TextView(this).apply {
            text = "Enter the address you use to open KenTrackIt in a browser."
            textSize = 15f
            setTextColor(Color.rgb(203, 213, 225))
            gravity = Gravity.CENTER
            setPadding(0, 0, 0, px(20))
        })
        input = EditText(this).apply {
            hint = "e.g. kentrackit.example.co.nz or 192.168.1.50:8080"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            imeOptions = EditorInfo.IME_ACTION_GO
            setSingleLine()
            setTextColor(Color.WHITE)
            setHintTextColor(Color.rgb(100, 116, 139))
            setText(Prefs(this@SetupActivity).server)
            setOnEditorActionListener { _, id, _ -> if (id == EditorInfo.IME_ACTION_GO) { connect(); true } else false }
        }
        col.addView(input, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        go = Button(this).apply {
            text = "Connect"
            setOnClickListener { connect() }
        }
        col.addView(go, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, px(56)).apply { topMargin = px(16) })
        status = TextView(this).apply {
            textSize = 14f
            setTextColor(Color.rgb(252, 165, 165))
            gravity = Gravity.CENTER
            setPadding(0, px(16), 0, 0)
        }
        col.addView(status)
        setContentView(ScrollView(this).apply {
            setBackgroundColor(Color.rgb(11, 27, 51))
            fitsSystemWindows = true
            isFillViewport = true
            addView(col)
        })
    }

    private fun connect() {
        val url = Urls.normalise(input.text.toString())
        if (url == null) {
            status.text = "That doesn't look like an address."
            return
        }
        go.isEnabled = false
        status.setTextColor(Color.rgb(203, 213, 225))
        status.text = "Checking $url …"
        Thread {
            val err = probe(url)
            runOnUiThread {
                go.isEnabled = true
                if (err == null) {
                    val p = Prefs(this)
                    if (!Urls.sameHost(p.server, url)) {
                        // a different box: its own update numbering and sharing choice
                        p.offeredUpdate = 0
                        p.sharing = false
                    }
                    p.server = url
                    startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TASK or Intent.FLAG_ACTIVITY_NEW_TASK))
                    finish()
                } else {
                    status.setTextColor(Color.rgb(252, 165, 165))
                    status.text = err
                }
            }
        }.start()
    }

    /** null when [base] answers like a KenTrackIt box. */
    private fun probe(base: String): String? = try {
        val c = URL("$base/api/auth-status").openConnection() as HttpURLConnection
        c.connectTimeout = 8000
        c.readTimeout = 8000
        c.setRequestProperty("User-Agent", Api.userAgent())
        val code = c.responseCode
        val body = (if (code < 400) c.inputStream else c.errorStream)?.bufferedReader()?.use { it.readText() } ?: ""
        c.disconnect()
        if (code == 200 && body.trimStart().startsWith("{")) null
        else "That address answered, but it isn't a KenTrackIt system (HTTP $code)."
    } catch (e: javax.net.ssl.SSLException) {
        "Couldn't make a secure connection: ${e.message}. If the box uses its own certificate, use its Cloudflare address."
    } catch (e: Exception) {
        "Couldn't reach $base (${e.javaClass.simpleName}). Check the address and that this phone is on the right network."
    }
}
