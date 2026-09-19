package org.hophacks.inkcaretaker

import android.media.AudioManager
import android.media.ToneGenerator
import android.os.Build
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.view.WindowManager
import androidx.appcompat.app.AppCompatActivity
import org.hophacks.inkcaretaker.databinding.ActivityEmergencyBinding

class EmergencyActivity : AppCompatActivity() {
    private var tone: ToneGenerator? = null
    private var vibrator: Vibrator? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                or WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                or WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD,
        )
        val binding = ActivityEmergencyBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val patient = intent.getStringExtra("patient").orEmpty().ifBlank { Prefs.patientName(this) }
        val callName = intent.getStringExtra("call_name").orEmpty()
        val text = intent.getStringExtra("text").orEmpty()
        binding.patient.text = "$patient needs you"
        binding.body.text = listOf(callName.takeIf { it.isNotBlank() }?.let { "Alert sent to $it" }, text)
            .filterNotNull()
            .joinToString("\n")
        ImageLoader.load(binding.image, intent.getStringExtra("image_url").orEmpty())
        binding.ack.setOnClickListener {
            Notifier.clearEmergency(this)
            finish()
        }
        startAlarm()
    }

    private fun startAlarm() {
        try {
            tone = ToneGenerator(AudioManager.STREAM_ALARM, 90).also {
                it.startTone(ToneGenerator.TONE_CDMA_ALERT_CALL_GUARD, 2500)
            }
        } catch (_: Exception) {
        }
        vibrator = if (Build.VERSION.SDK_INT >= 31) {
            val manager = getSystemService(VibratorManager::class.java)
            manager?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            getSystemService(VIBRATOR_SERVICE) as? Vibrator
        }
        val pattern = longArrayOf(0, 400, 120, 400, 120, 800)
        if (Build.VERSION.SDK_INT >= 26) {
            vibrator?.vibrate(VibrationEffect.createWaveform(pattern, 0))
        } else {
            @Suppress("DEPRECATION")
            vibrator?.vibrate(pattern, 0)
        }
    }

    override fun onDestroy() {
        tone?.release()
        vibrator?.cancel()
        super.onDestroy()
    }
}
