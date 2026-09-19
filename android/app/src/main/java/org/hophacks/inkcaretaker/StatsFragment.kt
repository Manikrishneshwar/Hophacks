package org.hophacks.inkcaretaker

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.fragment.app.Fragment
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import org.hophacks.inkcaretaker.databinding.FragmentStatsBinding
import org.hophacks.inkcaretaker.databinding.ItemStatBinding
import java.time.LocalDate
import java.time.format.DateTimeFormatter

class StatsFragment : Fragment() {
    private var binding: FragmentStatsBinding? = null
    private var stop: (() -> Unit)? = null
    private val adapter = StatsAdapter()
    private val dateFmt = DateTimeFormatter.ofPattern("EEE, MMM d")

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        val b = FragmentStatsBinding.inflate(inflater, container, false)
        binding = b
        b.list.layoutManager = LinearLayoutManager(requireContext())
        b.list.adapter = adapter
        render()
        stop = StatsStore.listen { render() }
        return b.root
    }

    private fun render() {
        val b = binding ?: return
        val stats = StatsStore.current
        val day = try {
            LocalDate.parse(stats.date.ifBlank { LocalDate.now().toString() }).format(dateFmt)
        } catch (_: Exception) {
            stats.date.ifBlank { "Today" }
        }
        b.heading.text = getString(R.string.stats_heading)
        b.subhead.text = if (stats.total == 0) {
            "$day · nothing confirmed yet"
        } else {
            "$day · ${stats.total} confirmed"
        }
        adapter.submit(stats.items)
        b.empty.visibility = if (stats.items.isEmpty()) View.VISIBLE else View.GONE
        b.list.visibility = if (stats.items.isEmpty()) View.GONE else View.VISIBLE
    }

    override fun onDestroyView() {
        stop?.invoke()
        stop = null
        binding = null
        super.onDestroyView()
    }
}

private class StatsAdapter : RecyclerView.Adapter<StatsAdapter.Holder>() {
    private val items = mutableListOf<Pair<String, Int>>()

    fun submit(next: List<Pair<String, Int>>) {
        items.clear()
        items.addAll(next)
        notifyDataSetChanged()
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
        val binding = ItemStatBinding.inflate(LayoutInflater.from(parent.context), parent, false)
        return Holder(binding)
    }

    override fun getItemCount(): Int = items.size

    override fun onBindViewHolder(holder: Holder, position: Int) {
        val (label, count) = items[position]
        holder.label.text = label.replaceFirstChar { if (it.isLowerCase()) it.titlecase() else it.toString() }
        holder.count.text = count.toString()
    }

    class Holder(binding: ItemStatBinding) : RecyclerView.ViewHolder(binding.root) {
        val label: TextView = binding.label
        val count: TextView = binding.count
    }
}
