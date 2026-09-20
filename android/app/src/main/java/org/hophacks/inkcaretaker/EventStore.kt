package org.hophacks.inkcaretaker

import android.os.Handler
import android.os.Looper

object EventStore {
    @Volatile var appForeground: Boolean = false
    private val events = mutableListOf<CaretakerEvent>()
    private val listeners = mutableListOf<() -> Unit>()
    private val main = Handler(Looper.getMainLooper())

    fun all(): List<CaretakerEvent> = synchronized(events) { events.toList() }

    fun upsert(event: CaretakerEvent) {
        if (event.id.isBlank()) return
        synchronized(events) {
            val index = events.indexOfFirst { it.id == event.id }
            if (index >= 0) events[index] = event else events.add(0, event)
        }
        notifyListeners()
    }

    fun replaceAll(next: List<CaretakerEvent>) {
        synchronized(events) {
            events.clear()
            events.addAll(next)
        }
        notifyListeners()
    }

    fun listen(listener: () -> Unit): () -> Unit {
        synchronized(listeners) { listeners.add(listener) }
        return {
            synchronized(listeners) { listeners.remove(listener) }
        }
    }

    private fun notifyListeners() {
        main.post {
            val copy: List<() -> Unit>
            synchronized(listeners) { copy = listeners.toList() }
            copy.forEach { it() }
        }
    }
}
