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
data class ChatConversation(
    val id: String,
    val title: String,
    val projectId: String?,
    val memoryEnabled: Boolean,
    val defaultBotId: String?,
    val defaultModelId: String?,
    val updatedAt: String,
)
data class ChatMessage(val id: String, val role: String, val content: String, val createdAt: String)
data class ChatProject(val id: String, val name: String)
data class ChatRouteOption(val id: String, val name: String)
data class ChatBootstrap(val bots: List<ChatRouteOption>, val models: List<ChatRouteOption>)

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
    fun listConversations(projectId: String? = null, unscoped: Boolean = false): List<ChatConversation> {
        val builder = requireInstanceUrl().newBuilder().addPathSegments("api/chat/conversations")
        if (!projectId.isNullOrBlank()) builder.addQueryParameter("project_id", projectId)
        if (unscoped) builder.addQueryParameter("project_id", "__unscoped__")
        return getJsonArray(builder.build()).toChatConversations()
    }

    @Throws(IOException::class)
    fun listProjects(): List<ChatProject> = getJsonArray(
        requireInstanceUrl().newBuilder().addPathSegments("api/projects").build(),
    ).toChatProjects()

    @Throws(IOException::class)
    fun createConversation(title: String, projectId: String?): ChatConversation {
        val csrf = csrfToken()
        val payload = JSONObject().put("title", title).put("memory_profiles_enabled", true)
        if (!projectId.isNullOrBlank()) payload.put("project_id", projectId)
        val request = Request.Builder()
            .url(requireInstanceUrl().newBuilder().addPathSegments("api/chat/conversations").build())
            .header("X-CSRFToken", csrf)
            .post(payload.toString().toRequestBody("application/json".toMediaType()))
            .build()
        return client.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw IOException(jsonError(body, "Could not create the chat."))
            JSONObject(body).toChatConversation()
        }
    }

    @Throws(IOException::class)
    fun chatBootstrap(): ChatBootstrap {
        val request = Request.Builder().url(
            requireInstanceUrl().newBuilder().addPathSegments("api/chat/bootstrap").build(),
        ).build()
        return client.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw IOException(jsonError(body, "Chat configuration is unavailable."))
            val payload = JSONObject(body)
            ChatBootstrap(
                bots = payload.optJSONArray("bots").toRouteOptions(),
                models = payload.optJSONArray("models").toRouteOptions(),
            )
        }
    }

    @Throws(IOException::class)
    fun archiveConversation(conversationId: String): ChatConversation {
        val request = Request.Builder()
            .url(requireInstanceUrl().newBuilder().addPathSegments("api/chat/conversations/$conversationId/archive").build())
            .header("X-CSRFToken", csrfToken())
            .post(JSONObject().toString().toRequestBody("application/json".toMediaType()))
            .build()
        return client.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw IOException(jsonError(body, "Could not archive chat."))
            JSONObject(body).toChatConversation()
        }
    }

    @Throws(IOException::class)
    fun deleteConversation(conversationId: String) {
        val request = Request.Builder()
            .url(requireInstanceUrl().newBuilder().addPathSegments("api/chat/conversations/$conversationId").build())
            .header("X-CSRFToken", csrfToken())
            .delete()
            .build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw IOException("Could not delete chat (${response.code}).")
        }
    }

    @Throws(IOException::class)
    fun updateMemory(conversationId: String, enabled: Boolean) = mutateConversation(
        conversationId, "memory-profile", JSONObject().put("enabled", enabled).put("profile_id", "default"),
    )

    @Throws(IOException::class)
    fun updateRoute(conversationId: String, botId: String?) = mutateConversation(
        conversationId, "route-defaults", JSONObject().put("default_bot_id", botId ?: "").put("default_model_id", JSONObject.NULL),
    )

    @Throws(IOException::class)
    fun listMessages(conversationId: String): List<ChatMessage> = getJsonArray(
        requireInstanceUrl().newBuilder().addPathSegments("api/chat/conversations/$conversationId/messages").build(),
    ).toChatMessages()

    @Throws(IOException::class)
    fun sendMessage(conversationId: String, content: String, botId: String?): List<ChatMessage> {
        val csrf = csrfToken()
        val payload = JSONObject().put("conversation_id", conversationId).put("content", content)
        if (!botId.isNullOrBlank()) payload.put("bot_id", botId)
        val request = Request.Builder()
            .url(requireInstanceUrl().newBuilder().addPathSegments("api/chat/messages").build())
            .header("X-CSRFToken", csrf)
            .post(
                payload.toString().toRequestBody("application/json".toMediaType()),
            )
            .build()
        return client.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (response.code == 401 || response.code == 403) {
                throw IOException("Your NexusAI session expired. Sign in again.")
            }
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

    private fun getJsonArray(url: HttpUrl): JSONArray {
        val request = Request.Builder().url(url).build()
        return client.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw IOException(jsonError(body, "NexusAI request failed (${response.code})."))
            JSONArray(body)
        }
    }

    private fun mutateConversation(conversationId: String, action: String, payload: JSONObject): ChatConversation {
        val request = Request.Builder()
            .url(requireInstanceUrl().newBuilder().addPathSegments("api/chat/conversations/$conversationId/$action").build())
            .header("X-CSRFToken", csrfToken())
            .put(payload.toString().toRequestBody("application/json".toMediaType()))
            .build()
        return client.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw IOException(jsonError(body, "Chat settings update failed."))
            JSONObject(body).toChatConversation()
        }
    }

    private fun jsonError(body: String, fallback: String): String = runCatching {
        JSONObject(body).optString("error").ifBlank { fallback }
    }.getOrDefault(fallback)
}

private fun JSONArray.toChatConversations(): List<ChatConversation> = buildList {
    for (index in 0 until length()) {
        val row = optJSONObject(index) ?: continue
        add(row.toChatConversation())
    }
}

private fun JSONArray.toChatMessages(): List<ChatMessage> = buildList {
    for (index in 0 until length()) {
        val row = optJSONObject(index) ?: continue
        add(row.toChatMessage())
    }
}

private fun JSONArray.toChatProjects(): List<ChatProject> = buildList {
    for (index in 0 until length()) {
        val row = optJSONObject(index) ?: continue
        add(ChatProject(row.optString("id"), row.optString("name", row.optString("id"))))
    }
}

private fun JSONObject.toChatConversation() = ChatConversation(
    id = optString("id"),
    title = optString("title", "Untitled chat"),
    projectId = optString("project_id").takeIf { it.isNotBlank() },
    memoryEnabled = optBoolean("memory_profiles_enabled", true),
    defaultBotId = optString("default_bot_id").takeIf { it.isNotBlank() },
    defaultModelId = optString("default_model_id").takeIf { it.isNotBlank() },
    updatedAt = optString("updated_at"),
)

private fun JSONObject.toChatMessage() = ChatMessage(
    id = optString("id"),
    role = optString("role", "assistant"),
    content = optString("content"),
    createdAt = optString("created_at"),
)

private fun JSONArray?.toRouteOptions(): List<ChatRouteOption> = buildList {
    if (this@toRouteOptions == null) return@buildList
    for (index in 0 until this@toRouteOptions.length()) {
        val row = this@toRouteOptions.optJSONObject(index) ?: continue
        val id = row.optString("id")
        if (id.isNotBlank()) add(ChatRouteOption(id, row.optString("name", id)))
    }
}
