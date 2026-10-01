package com.example.akali

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity

/**
 * 阿卡莉 WebView 壳。
 *
 * 直接在壳里加载云服务器上的 http://<公网IP>:<端口>/ 页面：
 *  - 明文 http 与相对路径 fetch 全部照常（manifest 已放行明文）；
 *  - 站内链接在 WebView 内打开，站外链接交给系统浏览器；
 *  - 返回键优先回退网页历史，到顶再退出。
 *
 * 服务器地址只需改 res/values/strings.xml 里的 server_url，无需改本文件。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val serverUrl: String = getString(R.string.server_url)
        val serverHost: String? = try { Uri.parse(serverUrl).host } catch (e: Exception) { null }

        webView = findViewById(R.id.webview)

        webView.settings.apply {
            javaScriptEnabled = true          // 前端完全依赖 JS
            domStorageEnabled = true          // localStorage 存登录态/token，必须开
            databaseEnabled = true
            useWideViewPort = true
            loadWithOverviewMode = true
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_DEFAULT
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            userAgentString = userAgentString + " AkaliApp/1.0"
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val host = request.url.host
                return if (host == null || host == serverHost) {
                    false // 站内（或无法判定）→ WebView 内打开
                } else {
                    // 站外链接（如手动打开的攻略站）→ 交给系统浏览器
                    try {
                        startActivity(Intent(Intent.ACTION_VIEW, request.url))
                    } catch (e: Exception) {
                        // 没有可处理的应用时忽略，保持在 WebView 内
                    }
                    true
                }
            }
        }
        webView.webChromeClient = WebChromeClient()

        if (savedInstanceState == null) {
            webView.loadUrl(serverUrl)
        } else {
            webView.restoreState(savedInstanceState)
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) {
                    webView.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView.saveState(outState)
    }
}
