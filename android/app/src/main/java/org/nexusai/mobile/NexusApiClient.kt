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
import org.json.JSONArray
import java.io.IOException

data class MobileUser(val id: String, val email: String, val role: String)
data class MobileSession(val user: MobileUser)
data class AndroidUpdate(
    val minimumVersionCode: Int,
    val latestVersionCode: Int,
    val releaseUrl: String,
)
data class MobileBootstrap(val apiVersion: Int, val androidUpdate: AndroidUpdate)
data class ChatConversation(val id: String, val title: String, val updatedAt: String)
data class ChatMessage(val id: String, val role: String, val content: String, val createdAt: String)

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

    @Throws(IOException::class)
    fun fetchBootstrap(): MobileBootstrap {
        val request = Request.Builder()
            .url(requireInstanceUrl().newBuilder().addPathSegments("api/mobile/bootstrap").build())
            .build()
        return client.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw IOException("NexusAI update check failed (${response.code}).")
            val root = JSONObject(body)
            val android = root.getJSONObject("android")
            MobileBootstrap(
                apiVersion = root.optInt("api_version", 0),
                androidUpdate = AndroidUpdate(
                    minimumVersionCode = android.optInt("minimum_version_code", 1),
                    latestVersionCode = android.optInt("latest_version_code", 1),
                    releaseUrl = android.optString("release_url"),
                ),
            )
        }
    }

    @Throws(IOException::class)
    fun csrfToken(): String {
        val request = Request.Builder()
            .url(requireInstanceUrl().newBuilder().addPathSegments("api/auth/csrf").build())
            .build()
        return client.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw IOException("NexusAI session needs to be renewed.")
            JSONObject(body).optString("csrf_token").takeIf { it.isNotBlank() }
                ?: throw IOException("NexusAI did not issue a CSRF token.")
        }
    }

    @Throws(IOException::class)
    fun listConversations(): List<ChatConversation> = getJsonArray("api/chat/conversations").toChatConversations()

    @Throws(IOException::class)
    fun listMessages(conversationId: String): List<ChatMessage> = getJsonArray(
        "api/chat/conversations/$conversationId/messages",
    ).toChatMessages()

    @Throws(IOException::class)
    fun sendMessage(conversationId: String, content: String): List<ChatMessage> {
        val csrf = csrfToken()
        val request = Request.Builder()
            .url(requireInstanceUrl().newBuilder().addPathSegments("api/chat/messages").build())
            .header("X-CSRFToken", csrf)
            .post(
                JSONObject().put("conversation_id", conversationId).put("content", content).toString()
                    .toRequestBody("application/json".toMediaType()),
            )
            .build()
        return client.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw IOException(jsonError(body, "Message failed (${response.code})."))
            val payload = JSONObject(body)
            listOfNotNull(payload.optJSONObject("user_message"), payload.optJSONObject("assistant_message"))
                .map { it.toChatMessage() }
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

    private fun getJsonArray(path: String): JSONArray {
        val request = Request.Builder().url(requireInstanceUrl().newBuilder().addPathSegments(path).build()).build()
        return client.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw IOException(jsonError(body, "NexusAI request failed (${response.code})."))
            JSONArray(body)
        }
    }

    private fun jsonError(body: String, fallback: String): String = runCatching {
        JSONObject(body).optString("error").ifBlank { fallback }
    }.getOrDefault(fallback)
}

private fun JSONArray.toChatConversations(): List<ChatConversation> = buildList {
    for (index in 0 until length()) {
        val row = optJSONObject(index) ?: continue
        add(ChatConversation(row.optString("id"), row.optString("title", "Untitled chat"), row.optString("updated_at")))
    }
}

private fun JSONArray.toChatMessages(): List<ChatMessage> = buildList {
    for (index in 0 until length()) {
        val row = optJSONObject(index) ?: continue
        add(row.toChatMessage())
    }
}

private fun JSONObject.toChatMessage() = ChatMessage(
    id = optString("id"),
    role = optString("role", "assistant"),
    content = optString("content"),
    createdAt = optString("created_at"),
)
