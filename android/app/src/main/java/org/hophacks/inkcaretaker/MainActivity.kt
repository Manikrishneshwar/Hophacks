package org.hophacks.inkcaretaker

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.viewpager2.adapter.FragmentStateAdapter
import com.google.android.material.tabs.TabLayoutMediator
import org.hophacks.inkcaretaker.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private val askNotify = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (!granted) {
            Toast.makeText(this, "Notifications are off — emergency alerts need them", Toast.LENGTH_LONG).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.pager.adapter = object : FragmentStateAdapter(this) {
            override fun getItemCount() = 3
            override fun createFragment(position: Int): Fragment = when (position) {
                0 -> EventsFragment()
                1 -> BrainFragment()
                else -> StatsFragment()
            }
        }
        TabLayoutMediator(binding.tabs, binding.pager) { tab, position ->
            tab.text = when (position) {
                0 -> getString(R.string.tab_alerts)
                1 -> getString(R.string.tab_brain)
                else -> getString(R.string.tab_stats)
            }
        }.attach()

        binding.setup.setOnClickListener { showSetup() }
        binding.status.text = if (Prefs.configured(this)) Prefs.patientName(this) else "not connected"
        binding.dot.setBackgroundResource(
            if (Prefs.configured(this)) R.drawable.dot_on else R.drawable.dot_off,
        )

        requestNotify()
        if (!Prefs.configured(this)) {
            showSetup()
        } else {
            HubService.start(this)
        }
    }

    override fun onResume() {
        super.onResume()
        EventStore.appForeground = true
        binding.status.text = if (Prefs.configured(this)) Prefs.patientName(this) else "not connected"
    }

    override fun onPause() {
        EventStore.appForeground = false
        super.onPause()
    }

    private fun requestNotify() {
        if (Build.VERSION.SDK_INT < 33) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            askNotify.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun showSetup() {
        val pad = (16 * resources.displayMetrics.density).toInt()
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, 0)
        }
        val host = EditText(this).apply {
            hint = getString(R.string.host_hint)
            setText(Prefs.host(this@MainActivity))
            inputType = InputType.TYPE_CLASS_TEXT
        }
        val port = EditText(this).apply {
            hint = getString(R.string.port_hint)
            setText(Prefs.port(this@MainActivity).toString())
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        column.addView(host)
        column.addView(port)
        AlertDialog.Builder(this)
            .setTitle(R.string.setup_title)
            .setMessage(R.string.setup_body)
            .setView(column)
            .setPositiveButton(R.string.connect) { _, _ ->
                val raw = host.text.toString().trim()
                    .removePrefix("http://")
                    .removePrefix("https://")
                    .substringBefore("/")
                val maybePort = raw.substringAfter(":", "")
                val hostname = raw.substringBefore(":")
                val portNum = maybePort.toIntOrNull()
                    ?: port.text.toString().toIntOrNull()
                    ?: 8000
                if (hostname.isBlank() || hostname == "localhost" || hostname == "127.0.0.1") {
                    Toast.makeText(this, "Use the laptop Wi-Fi address, not localhost", Toast.LENGTH_LONG).show()
                    return@setPositiveButton
                }
                Prefs.setServer(this, hostname, portNum)
                binding.status.text = "$hostname:$portNum"
                binding.dot.setBackgroundResource(R.drawable.dot_on)
                HubService.reconnect(this)
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }
}
