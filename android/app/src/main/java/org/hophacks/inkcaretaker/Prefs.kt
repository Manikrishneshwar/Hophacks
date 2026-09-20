package org.hophacks.inkcaretaker

import android.content.Context

object Prefs {
    private const val FILE = "ink"

    fun host(ctx: Context): String = prefs(ctx).getString("host", "") ?: ""
    fun port(ctx: Context): Int = prefs(ctx).getInt("port", 8000)
    fun configured(ctx: Context): Boolean = host(ctx).isNotBlank()
    fun patientName(ctx: Context): String =
        prefs(ctx).getString("patient", "the patient") ?: "the patient"

    fun setServer(ctx: Context, host: String, port: Int) {
        prefs(ctx).edit().putString("host", host.trim()).putInt("port", port).apply()
    }

    fun setPatientName(ctx: Context, name: String) {
        if (name.isNotBlank()) prefs(ctx).edit().putString("patient", name).apply()
    }

    fun baseUrl(ctx: Context): String = "http://${host(ctx)}:${port(ctx)}"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)
}
