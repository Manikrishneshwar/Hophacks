package org.hophacks.inkcaretaker

import android.app.Application

class InkApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Notifier.ensureChannels(this)
    }
}
