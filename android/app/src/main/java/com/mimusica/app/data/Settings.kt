package com.mimusica.app.data

import android.content.Context
import android.os.Build

/** Connection to the Mac saved on the phone: address found on the Wi-Fi and token. */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("settings", Context.MODE_PRIVATE)

    var host: String?
        get() = prefs.getString("host", null)
        set(v) = prefs.edit().putString("host", v).apply()

    var port: Int
        get() = prefs.getInt("port", 8765)
        set(v) = prefs.edit().putInt("port", v).apply()

    var token: String?
        get() = prefs.getString("token", null)
        set(v) = prefs.edit().putString("token", v).apply()

    var macName: String?
        get() = prefs.getString("mac_name", null)
        set(v) = prefs.edit().putString("mac_name", v).apply()

    val paired get() = !token.isNullOrBlank()
    val baseUrl get() = host?.let { "http://$it:$port" }
    val deviceName: String get() = "${Build.MANUFACTURER} ${Build.MODEL}".trim()

    fun forget() = prefs.edit().remove("token").remove("mac_name").apply()
}
