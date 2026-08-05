package org.nexusai.mobile

import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException

data class MobileUser(val id: String, val email: String, val role: String)
data class MobileSession(val user: MobileUser)

class NexusApiClient(private val store: InstanceStore) {
    private val cookies = mutableListOf<Cookie>()
    private val client = OkHttpClient.Builder()
        .cookieJar(object : CookieJar {
            override fun loadForRequest(url: HttpUrl): List<Cookie> = synchronized(cookies) {
                cookies.filter { it.matches(url) }
            }

            override fun saveFromResponse(url: HttpUrl, incoming: List<Cookie>) {
                synchronized(cookies) {
                    cookies.removeAll { existing -> incoming.any { it.name == existing.name && it.domain == existing.domain } }
                    cookies.addAll(incoming)
                    store.saveCookies(cookies)
                }
            }
        })
        .build()

    init {
        store.instanceUrl()?.let { cookies.addAll(store.loadCookies(it)) }
    }

    fun hasConfiguredInstance(): Boolean = store.instanceUrl() != null

    @Throws(IOException::class)
    fun signIn(email: String, password: String): MobileSession {
        val base = requireInstanceUrl()
        val request = Request.Builder()
            .url(base.newBuilder().addPathSegments("api/auth/login").build())
            .post(
                JSONObject()
                    .put("email", email.trim())
                    .put("password", password)
                    .toString()
                    .toRequestBody("application/json".toMediaType()),
            )
            .build()
        return client.newCall(request).execute().use { response -> response.toSession() }
    }

    @Throws(IOException::class)
    fun restoreSession(): MobileSession? {
        val base = store.instanceUrl() ?: return null
        val request = Request.Builder().url(base.newBuilder().addPathSegments("api/auth/session").build()).build()
        return client.newCall(request).execute().use { response ->
            if (response.code == 401) return null
            response.toSession()
        }
    }

    fun clearSession() {
        synchronized(cookies) { cookies.clear() }
        store.clear()
    }

    private fun requireInstanceUrl(): HttpUrl = store.instanceUrl()
        ?: throw IllegalStateException("Configure a NexusAI instance first.")

    private fun Response.toSession(): MobileSession {
        val body = body?.string().orEmpty()
        if (!isSuccessful) {
            val message = runCatching { JSONObject(body).optString("error") }.getOrNull().orEmpty()
            throw IOException(message.ifBlank { "NexusAI sign-in failed ($code)." })
        }
        val user = JSONObject(body).getJSONObject("user")
        return MobileSession(
            MobileUser(
                id = user.optString("id"),
                email = user.optString("email"),
                role = user.optString("role"),
            ),
        )
    }
}
