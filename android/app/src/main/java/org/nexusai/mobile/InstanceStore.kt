package org.nexusai.mobile

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import okhttp3.Cookie
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

class InstanceStore(context: Context) {
    private val preferences = EncryptedSharedPreferences.create(
        context,
        "nexusai_mobile_session",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    fun instanceUrl(): HttpUrl? = preferences.getString(KEY_INSTANCE_URL, null)?.toHttpUrlOrNull()

    fun saveInstanceUrl(value: String): Result<HttpUrl> {
        val normalized = value.trim().trimEnd('/')
        val url = normalized.toHttpUrlOrNull()
            ?: return Result.failure(IllegalArgumentException("Enter a valid HTTPS NexusAI URL."))
        if (url.scheme != "https") {
            return Result.failure(IllegalArgumentException("NexusAI mobile connections require HTTPS."))
        }
        preferences.edit().putString(KEY_INSTANCE_URL, url.toString().trimEnd('/')).apply()
        return Result.success(url)
    }

    fun clear() {
        preferences.edit().clear().apply()
    }

    fun loadCookies(url: HttpUrl): List<Cookie> = preferences.getString(KEY_COOKIES, "").orEmpty()
        .lineSequence()
        .mapNotNull { Cookie.parse(url, it) }
        .toList()

    fun saveCookies(cookies: List<Cookie>) {
        preferences.edit().putString(KEY_COOKIES, cookies.joinToString("\n") { it.toString() }).apply()
    }

    private companion object {
        const val KEY_INSTANCE_URL = "instance_url"
        const val KEY_COOKIES = "session_cookies"
    }
}
