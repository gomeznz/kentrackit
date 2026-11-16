package nz.co.asiltd.kentrackit

import android.content.Intent
import android.content.IntentFilter
import android.view.KeyEvent

/**
 * Hardware buttons on PoC radios and rugged phones (shared with RapidPTT).
 *
 * These devices do not deliver their Talk key as an ordinary key press.
 * Their firmware sends a broadcast when the key goes down and another when
 * it comes up, so it works with the screen off. There is no standard: the
 * names below are the conventions in use.
 */
object HardwareButtons {
    enum class Event { PTT_DOWN, PTT_UP, GROUP_NEXT, GROUP_PREVIOUS }

    /** One broadcast for key down, another for key up. */
    private val downActions = setOf(
        "com.zello.ptt.down", // Zello's convention, followed by many network radios
        "android.intent.action.PTT.down", // Inrico-style radios, RugGear
        "com.sonim.intent.action.PTT_KEY_DOWN", // Sonim
    )
    private val upActions = setOf(
        "com.zello.ptt.up",
        "android.intent.action.PTT.up",
        "com.sonim.intent.action.PTT_KEY_UP",
    )

    /** One broadcast for both, carrying the KeyEvent that says which. */
    private val keyEventActions = setOf(
        "com.kyocera.android.intent.action.PTT_BUTTON", // Kyocera
        "com.kodiak.intent.action.PTT_BUTTON", // Kyocera and others, carrier PTT key
        "com.symbol.button.L1", // Zebra
    )

    /** Channel knob or channel up/down keys: used to step through talk groups. */
    private const val NEXT = "com.zello.intent.channelUp"
    private const val PREVIOUS = "com.zello.intent.channelDown"

    fun filter(): IntentFilter = IntentFilter().apply {
        (downActions + upActions + keyEventActions + NEXT + PREVIOUS).forEach(::addAction)
        priority = IntentFilter.SYSTEM_HIGH_PRIORITY - 1
    }

    fun classify(intent: Intent): Event? {
        @Suppress("DEPRECATION") // the typed overload needs API 33
        val key = intent.getParcelableExtra<KeyEvent>(Intent.EXTRA_KEY_EVENT)
        return classify(intent.action, key?.action)
    }

    /** [keyAction] is KeyEvent.ACTION_DOWN or ACTION_UP when the broadcast carried a KeyEvent. */
    fun classify(action: String?, keyAction: Int?): Event? = when (action) {
        in downActions -> Event.PTT_DOWN
        in upActions -> Event.PTT_UP
        in keyEventActions -> when (keyAction) {
            KeyEvent.ACTION_DOWN -> Event.PTT_DOWN
            KeyEvent.ACTION_UP -> Event.PTT_UP
            else -> null // without the KeyEvent there is no telling down from up
        }
        NEXT -> Event.GROUP_NEXT
        PREVIOUS -> Event.GROUP_PREVIOUS
        else -> null
    }

    /** Keys that must keep their normal job and cannot be chosen as the Talk key. */
    private val reservedKeys = setOf(
        KeyEvent.KEYCODE_BACK, KeyEvent.KEYCODE_HOME, KeyEvent.KEYCODE_APP_SWITCH,
        KeyEvent.KEYCODE_POWER, KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_UNKNOWN,
    )

    fun canBeTalkKey(keyCode: Int): Boolean = keyCode !in reservedKeys

    /** "F5", "VOLUME_DOWN", or the number for keys Android has no name for. */
    fun keyName(keyCode: Int): String {
        val name = KeyEvent.keyCodeToString(keyCode) ?: return keyCode.toString()
        return name.removePrefix("KEYCODE_").replace('_', ' ').lowercase().replaceFirstChar { it.uppercase() }
    }
}
