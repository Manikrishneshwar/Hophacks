package org.hophacks.inkcaretaker

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.app.ServiceCompat
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

class HubService : Service() {
    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(25, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()
    private var socket: WebSocket? = null
    private val handler = Handler(Looper.getMainLooper())
    private var backoffMs = 500L
    private var lastEmergencyId: String? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        Notifier.ensureChannels(this)
        val listening = Notifier.listening(this, getString(R.string.listening))
        if (Build.VERSION.SDK_INT >= 29) {
            ServiceCompat.startForeground(
                this, Notifier.FOREGROUND_ID, listening,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(Notifier.FOREGROUND_ID, listening)
        }
        connect()
        refreshHistory()
        refreshStats()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_RECONNECT) {
            backoffMs = 500
            connect()
            refreshHistory()
            refreshStats()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        socket?.cancel()
        super.onDestroy()
    }

    private fun connect() {
        socket?.cancel()
        if (!Prefs.configured(this)) return
        val url = "ws://${Prefs.host(this)}:${Prefs.port(this)}/ws?role=caretaker"
        val request = Request.Builder().url(url).build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                backoffMs = 500
                Notifier.updateListening(
                    this@HubService,
                    "Listening for ${Prefs.patientName(this@HubService)}",
                )
                refreshHistory()
                refreshStats()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handle(text)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                scheduleReconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                scheduleReconnect()
            }
        })
    }

    private fun handle(text: String) {
        val msg = try {
            JSONObject(text)
        } catch (_: Exception) {
            return
        }
        when (msg.optString("type")) {
            "welcome" -> {
                val name = msg.optJSONObject("patient")?.optString("name").orEmpty()
                Prefs.setPatientName(this, name)
                Notifier.updateListening(this, "Listening for ${Prefs.patientName(this)}")
                val stats = msg.optJSONObject("stats")
                if (stats != null) StatsStore.set(DailyStats.from(stats))
            }
            "caretaker" -> onEvent(CaretakerEvent.from(msg, Prefs.baseUrl(this)))
            "stats" -> StatsStore.set(DailyStats.from(msg))
        }
    }

    private fun onEvent(event: CaretakerEvent) {
        EventStore.upsert(event)
        if (event.emergency) {
            if (lastEmergencyId == event.id) return
            lastEmergencyId = event.id
            Notifier.emergency(this, event)
            if (EventStore.appForeground) {
                val intent = Intent(this, EmergencyActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
                event.extras(intent)
                intent.putExtra("patient", Prefs.patientName(this))
                startActivity(intent)
            }
        } else {
            Notifier.event(this, event)
        }
    }

    private fun scheduleReconnect() {
        handler.postDelayed({
            backoffMs = (backoffMs * 2).coerceAtMost(10_000)
            connect()
        }, backoffMs)
    }

    private fun refreshHistory() {
        if (!Prefs.configured(this)) return
        val url = Prefs.baseUrl(this) + "/api/caretaker/events"
        thread {
            try {
                val body = client.newCall(Request.Builder().url(url).build()).execute().use { it.body?.string() }
                    ?: return@thread
                val json = JSONObject(body)
                val name = json.optJSONObject("patient")?.optString("name").orEmpty()
                if (name.isNotBlank()) Prefs.setPatientName(this, name)
                val arr = json.optJSONArray("events") ?: JSONArray()
                val events = mutableListOf<CaretakerEvent>()
                for (i in 0 until arr.length()) {
                    events.add(CaretakerEvent.from(arr.getJSONObject(i), Prefs.baseUrl(this)))
                }
                EventStore.replaceAll(events)
            } catch (_: Exception) {
            }
        }
    }

    private fun refreshStats() {
        if (!Prefs.configured(this)) return
        val url = Prefs.baseUrl(this) + "/api/caretaker/stats"
        thread {
            try {
                val body = client.newCall(Request.Builder().url(url).build()).execute().use { it.body?.string() }
                    ?: return@thread
                StatsStore.set(DailyStats.from(JSONObject(body)))
            } catch (_: Exception) {
            }
        }
    }

    companion object {
        const val ACTION_RECONNECT = "org.hophacks.inkcaretaker.RECONNECT"

        fun start(ctx: Context) {
            val intent = Intent(ctx, HubService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(intent)
            } else {
                ctx.startService(intent)
            }
        }

        fun reconnect(ctx: Context) {
            val intent = Intent(ctx, HubService::class.java).setAction(ACTION_RECONNECT)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(intent)
            } else {
                ctx.startService(intent)
            }
        }
    }
}
