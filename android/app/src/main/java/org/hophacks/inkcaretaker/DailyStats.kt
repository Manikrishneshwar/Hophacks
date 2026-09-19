package org.hophacks.inkcaretaker

import org.json.JSONObject

data class DailyStats(
    val date: String,
    val total: Int,
    val items: List<Pair<String, Int>>,
) {
    companion object {
        fun from(json: JSONObject): DailyStats {
            val arr = json.optJSONArray("items")
            val items = mutableListOf<Pair<String, Int>>()
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    val row = arr.optJSONObject(i) ?: continue
                    val label = row.optString("label")
                    if (label.isBlank()) continue
                    items.add(label to row.optInt("count"))
                }
            }
            return DailyStats(
                date = json.optString("date"),
                total = json.optInt("total"),
                items = items,
            )
        }

        val empty = DailyStats("", 0, emptyList())
    }
}
