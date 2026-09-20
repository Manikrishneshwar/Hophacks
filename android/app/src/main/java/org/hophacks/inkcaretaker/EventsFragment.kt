package org.hophacks.inkcaretaker

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.fragment.app.Fragment
import androidx.recyclerview.widget.LinearLayoutManager
import org.hophacks.inkcaretaker.databinding.FragmentEventsBinding

class EventsFragment : Fragment() {
    private var binding: FragmentEventsBinding? = null
    private var stop: (() -> Unit)? = null
    private val adapter = EventAdapter()
    private var visible = PAGE

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        val b = FragmentEventsBinding.inflate(inflater, container, false)
        binding = b
        b.list.layoutManager = LinearLayoutManager(requireContext())
        b.list.adapter = adapter
        b.loadMore.setOnClickListener {
            visible += PAGE
            render()
        }
        render()
        stop = EventStore.listen { render() }
        return b.root
    }

    private fun render() {
        val b = binding ?: return
        val items = EventStore.all()
        if (visible < PAGE) visible = PAGE
        val shown = items.take(visible)
        adapter.submit(shown)
        b.empty.visibility = if (items.isEmpty()) View.VISIBLE else View.GONE
        if (items.size > shown.size) {
            b.loadMore.visibility = View.VISIBLE
            val left = items.size - shown.size
            b.loadMore.text = getString(R.string.load_more) + " (${minOf(PAGE, left)} of $left)"
            b.loadMore.isEnabled = true
        } else if (items.isNotEmpty() && items.size > PAGE) {
            b.loadMore.visibility = View.VISIBLE
            b.loadMore.text = "All loaded"
            b.loadMore.isEnabled = false
        } else {
            b.loadMore.visibility = View.GONE
        }
    }

    override fun onDestroyView() {
        stop?.invoke()
        stop = null
        binding = null
        super.onDestroyView()
    }

    companion object {
        private const val PAGE = 5
    }
}
