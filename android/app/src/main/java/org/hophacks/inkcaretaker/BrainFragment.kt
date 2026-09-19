package org.hophacks.inkcaretaker

import android.annotation.SuppressLint
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.fragment.app.Fragment
import org.hophacks.inkcaretaker.databinding.FragmentBrainBinding

class BrainFragment : Fragment() {
    private var binding: FragmentBrainBinding? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        val b = FragmentBrainBinding.inflate(inflater, container, false)
        binding = b
        b.web.webViewClient = WebViewClient()
        b.web.settings.javaScriptEnabled = true
        b.web.settings.domStorageEnabled = true
        b.web.settings.cacheMode = WebSettings.LOAD_NO_CACHE
        b.web.settings.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        b.web.settings.setSupportZoom(false)
        b.web.settings.builtInZoomControls = false
        b.web.settings.displayZoomControls = false
        // Let the graph canvas own pinch-zoom; don't let the WebView steal it.
        b.web.isNestedScrollingEnabled = false
        load()
        return b.root
    }

    override fun onResume() {
        super.onResume()
        load()
    }

    private fun load() {
        val web: WebView = binding?.web ?: return
        if (!Prefs.configured(requireContext())) {
            web.loadData(
                "<html><body style='background:#0e0f13;color:#8b93a3;font-family:sans-serif;padding:24px'>Connect to the laptop first.</body></html>",
                "text/html",
                "utf-8",
            )
            return
        }
        web.loadUrl(Prefs.baseUrl(requireContext()) + "/brain?embed=1")
    }

    override fun onDestroyView() {
        binding?.web?.destroy()
        binding = null
        super.onDestroyView()
    }
}
