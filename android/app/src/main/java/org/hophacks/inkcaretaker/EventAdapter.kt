package org.hophacks.inkcaretaker

import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.recyclerview.widget.RecyclerView
import org.hophacks.inkcaretaker.databinding.ItemEventBinding
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter

class EventAdapter : RecyclerView.Adapter<EventAdapter.Holder>() {
    private val items = mutableListOf<CaretakerEvent>()
    private val timeFmt = DateTimeFormatter.ofPattern("h:mm a")

    fun submit(next: List<CaretakerEvent>) {
        items.clear()
        items.addAll(next)
        notifyDataSetChanged()
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
        val binding = ItemEventBinding.inflate(LayoutInflater.from(parent.context), parent, false)
        return Holder(binding)
    }

    override fun getItemCount(): Int = items.size

    override fun onBindViewHolder(holder: Holder, position: Int) {
        val event = items[position]
        holder.binding.headline.text = event.headline()
        holder.binding.detail.text = event.prompts.distinct().joinToString("\n")
        holder.binding.badge.text = event.badge()
        holder.binding.whenText.text = formatWhen(event.createdAt)
        val color = when {
            event.emergency -> holder.itemView.context.getColor(R.color.emergency)
            event.answer == "yes" -> holder.itemView.context.getColor(R.color.good)
            event.answer == "no" -> holder.itemView.context.getColor(R.color.bad)
            else -> holder.itemView.context.getColor(R.color.muted)
        }
        holder.binding.badge.setTextColor(color)
        ImageLoader.load(holder.binding.image, event.imageUrl)
    }

    private fun formatWhen(raw: String): String {
        return try {
            OffsetDateTime.parse(raw).format(timeFmt)
        } catch (_: Exception) {
            raw.take(16)
        }
    }

    class Holder(val binding: ItemEventBinding) : RecyclerView.ViewHolder(binding.root)
}
