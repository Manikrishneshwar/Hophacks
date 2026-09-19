package org.hophacks.inkcaretaker

import android.content.Intent
import org.json.JSONArray
import org.json.JSONObject

data class CaretakerEvent(
    val id: String,
    val kind: String,
    val createdAt: String,
    val imageUrl: String,
    val text: String,
    val answer: String,
    val tag: String,
    val detail: String,
    val prompts: List<String>,
    val emergency: Boolean,
    val callName: String,
) {
    fun headline(): String {
        if (emergency) {
            return if (callName.isNotBlank()) "HELP · $callName" else "HELP"
        }
        return text.ifBlank { "Drawing finished" }
    }

    fun badge(): String = when {
        emergency -> "HELP"
        answer == "yes" -> "YES"
        answer == "no" -> "NO"
        else -> "—"
    }

    fun extras(intent: Intent) {
        intent.putExtra("event_id", id)
        intent.putExtra("image_url", imageUrl)
        intent.putExtra("text", headline())
        intent.putExtra("call_name", callName)
        intent.putExtra("patient", "")
    }

    companion object {
        fun from(msg: JSONObject, baseUrl: String): CaretakerEvent {
            val image = msg.optString("image")
            val imageUrl = when {
                image.startsWith("http") -> image
                image.startsWith("/") -> baseUrl + image
                image.isBlank() -> ""
                else -> "$baseUrl/$image"
            }
            val call = msg.optJSONObject("call")
            val kind = msg.optString("kind")
            val priority = msg.optString("priority")
            val prompts = mutableListOf<String>()
            val arr: JSONArray? = msg.optJSONArray("prompts")
            if (arr != null) {
                for (i in 0 until arr.length()) prompts.add(arr.optString(i))
            }
            val text = msg.optString("text")
            if (text.isNotBlank() && text !in prompts) prompts.add(0, text)
            return CaretakerEvent(
                id = msg.optString("id"),
                kind = kind,
                createdAt = msg.optString("created_at"),
                imageUrl = imageUrl,
                text = text,
                answer = msg.optString("answer"),
                tag = msg.optString("tag"),
                detail = msg.optString("detail"),
                prompts = prompts,
                emergency = kind == "emergency" || kind == "call" || priority == "emergency",
                callName = call?.optString("name").orEmpty(),
            )
        }
    }
}
