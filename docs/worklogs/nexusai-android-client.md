# NexusAI Android Client

## Objective

Provide a native Android client that connects to a user-owned NexusAI instance for chat and operational monitoring without introducing a shared hosted backend.

## First Release Scope

- Connect to a configured HTTPS NexusAI dashboard URL.
- Authenticate against the existing dashboard session API.
- Store the instance URL and session cookie in Android encrypted storage.
- Browse conversations, read messages, send normal chat messages, and view the compact work brief.
- Show an explicit client version and update-required state from the instance API contract when available.

## Non-Goals

- No control-plane token, provider key, or server secret is stored in the app.
- No autonomous worker activation, repository edit, deployment, or destructive control is included in the first release.
- No chat-history migration or memory-profile changes are included in this work item.

## Architecture

The Android app is a Kotlin/Jetpack Compose client in `android/`. It communicates only with the public dashboard origin over HTTPS and relies on the existing `/api/auth/login` session cookie. The instance URL is user-configured, so every user can connect the app to their own NexusAI deployment.

The client uses a versioned API surface in the dashboard. The initial implementation targets the existing authenticated chat and work endpoints; a small mobile bootstrap endpoint will be added only where the app needs a stable, minimal contract.

## Security Constraints

- HTTPS is required for normal connections; cleartext URLs are rejected.
- Session cookies and the configured instance URL are stored through AndroidX Security encrypted preferences.
- No connection value is logged with credentials or cookies.
- Certificate pinning is deferred until the instance enrollment and certificate-rotation UX are designed; the app relies on standard Android TLS validation in the first release.

## Completion Criteria

- A debug Android build installs on an emulator or device.
- A user can configure a NexusAI instance, sign in, close/reopen the app, and retain the session securely.
- A user can read and send messages in a conversation and inspect the current work brief.
- Instance errors, expired sessions, and unavailable servers produce actionable UI states.
- Build instructions and update behavior are documented.

## Progress

- Discovery complete: NexusAI has no existing Android client. Dashboard authentication is session-cookie based through `/api/auth/login`, and chat/work APIs already provide the initial read/write surface.
- Android project scaffold complete in `android/`, including the Gradle wrapper, native Kotlin/Compose shell, HTTPS-only instance onboarding, encrypted instance/session storage, cookie-backed dashboard login, and launch-time session restoration.
- Dashboard mobile contract added: `/api/mobile/bootstrap` returns versioned, public Android update metadata, and authenticated `/api/auth/csrf` issues a session-bound token for mobile mutations without exempting chat APIs from CSRF protections.
- Android update delivery added: the client checks the instance contract after session restoration and uses Android's package installer to apply a user-approved replacement APK. Same package ID and signing key preserve local app data across updates.
- First chat UI complete: project and unscoped scope picker, active conversation list, new scoped chat creation, message-history reader, refresh, and normal text composer. Native mutations use a session-bound CSRF token rather than weakening the dashboard's browser protections.
- Verification: `ANDROID_HOME=%LOCALAPPDATA%\\Android\\Sdk .\\gradlew.bat :app:assembleDebug` completed successfully. The debug APK is at `android/app/build/outputs/apk/debug/app-debug.apk`.

## Risks and Blockers

- Compact work monitoring, attachments, streamed responses, chat settings, and notifications remain after the first chat release.
- Push notifications, offline queues, file uploads, and mutation controls are intentionally deferred until the read/send client is proven.
