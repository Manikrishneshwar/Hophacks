package org.hophacks.inkcaretaker

import android.os.Handler
import android.os.Looper

object StatsStore {
    @Volatile var current: DailyStats = DailyStats.empty
        private set
    private val listeners = mutableListOf<() -> Unit>()
    private val main = Handler(Looper.getMainLooper())

    fun set(stats: DailyStats) {
        current = stats
        main.post {
            val copy: List<() -> Unit>
            synchronized(listeners) { copy = listeners.toList() }
            copy.forEach { it() }
        }
    }

    fun listen(listener: () -> Unit): () -> Unit {
        synchronized(listeners) { listeners.add(listener) }
        return {
            synchronized(listeners) { listeners.remove(listener) }
        }
    }
}
