package org.hophacks.inkcaretaker

import android.graphics.BitmapFactory
import android.widget.ImageView
import java.net.URL
import java.util.concurrent.Executors

object ImageLoader {
    private val pool = Executors.newFixedThreadPool(3)

    fun load(view: ImageView, url: String) {
        if (url.isBlank()) {
            view.setImageDrawable(null)
            return
        }
        view.tag = url
        pool.execute {
            try {
                val bytes = URL(url).openStream().use { it.readBytes() }
                val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                view.post {
                    if (view.tag == url) view.setImageBitmap(bmp)
                }
            } catch (_: Exception) {
                view.post {
                    if (view.tag == url) view.setImageDrawable(null)
                }
            }
        }
    }
}
