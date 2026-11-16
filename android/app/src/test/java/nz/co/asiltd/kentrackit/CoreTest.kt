package nz.co.asiltd.kentrackit

import android.view.KeyEvent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CoreTest {
    @Test fun addresses() {
        assertEquals("https://kentrackit.example.co.nz", Urls.normalise(" kentrackit.example.co.nz/ "))
        assertEquals("http://192.168.1.50:8080", Urls.normalise("192.168.1.50:8080"))
        assertEquals("http://192.168.1.50", Urls.normalise("192.168.1.50"))
        assertEquals("http://avl-pi:8080", Urls.normalise("avl-pi:8080"))
        assertEquals("https://box.example.com/kt", Urls.normalise("https://box.example.com/kt/"))
        assertNull(Urls.normalise(""))
        assertNull(Urls.normalise("ftp://box"))
        assertNull(Urls.normalise("not an address"))
        assertTrue(Urls.sameHost("https://Box.example.com/console", "https://box.example.com"))
        assertFalse(Urls.sameHost("https://evil.example.com", "https://box.example.com"))
    }

    @Test fun serverSentEvents() {
        val got = mutableListOf<Pair<String, String>>()
        val p = SseParser { e, d -> got += e to d }
        listOf(": ping", "", "event: alert", "data: {\"id\":1}", "", "data: a", "data:b", "\r", "event: x", "")
            .forEach(p::line)
        assertEquals(listOf("alert" to "{\"id\":1}", "message" to "a\nb"), got)
    }

    @Test fun talkButtons() {
        assertEquals(HardwareButtons.Event.PTT_DOWN, HardwareButtons.classify("com.zello.ptt.down", null))
        assertEquals(HardwareButtons.Event.PTT_UP, HardwareButtons.classify("android.intent.action.PTT.up", null))
        assertEquals(HardwareButtons.Event.PTT_DOWN,
            HardwareButtons.classify("com.kyocera.android.intent.action.PTT_BUTTON", KeyEvent.ACTION_DOWN))
        assertNull(HardwareButtons.classify("com.kyocera.android.intent.action.PTT_BUTTON", null))
        assertNull(HardwareButtons.classify("something.else", null))
        assertFalse(HardwareButtons.canBeTalkKey(KeyEvent.KEYCODE_BACK))
        assertTrue(HardwareButtons.canBeTalkKey(KeyEvent.KEYCODE_F5))
    }

    @Test fun updates() {
        assertTrue(Updater.isNewer(12, 11))
        assertFalse(Updater.isNewer(11, 11))
        assertFalse(Updater.isNewer(0, 11))
    }
}
