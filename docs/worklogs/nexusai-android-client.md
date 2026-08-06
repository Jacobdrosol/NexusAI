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

## Chat Completion Scope

The Android chat client must reach a usable, safe subset of the web chat before operational screens are started:

- project, unscoped, and bridged chat selection; active and archived conversation lifecycle;
- create, search, archive, restore, and delete conversations with explicit confirmation for destructive actions;
- streaming responses with recoverable reconnect/refresh behavior;
- normal text, image, and document attachments within the dashboard limits;
- readable Markdown, code blocks, copy actions, math-capable rendering, and attachment previews;
- per-conversation chat settings for bot, model, memory, and display preferences; and
- native loading, empty, offline, expired-session, and server-error states.

Worker execution, repository actions, deployment controls, and operational dashboards remain out of scope until this checklist is completed and device-tested.

## Progress

- Discovery complete: NexusAI has no existing Android client. Dashboard authentication is session-cookie based through `/api/auth/login`, and chat/work APIs already provide the initial read/write surface.
- Android project scaffold complete in `android/`, including the Gradle wrapper, native Kotlin/Compose shell, HTTPS-only instance onboarding, encrypted instance/session storage, cookie-backed dashboard login, and launch-time session restoration.
- Dashboard mobile contract added: `/api/mobile/bootstrap` returns versioned, public Android update metadata, and authenticated `/api/auth/csrf` issues a session-bound token for mobile mutations without exempting chat APIs from CSRF protections.
- Android update delivery added: the client checks the instance contract after session restoration and uses Android's package installer to apply a user-approved replacement APK. Same package ID and signing key preserve local app data across updates. Deployments publish a versioned signed APK to the instance's own `/releases/nexusai.apk` endpoint and write `data/mobile-release.json`; the dashboard reads that manifest for the public bootstrap contract.
- First chat UI complete: project and unscoped scope picker, active conversation list, new scoped chat creation, message-history reader, refresh, and normal text composer. Native mutations use a session-bound CSRF token rather than weakening the dashboard's browser protections.
- App settings added: user-owned instance address, persistent system/dark/light theme, installed version/build identity, and advertised update action. Conversation settings now select a direct-chat-enabled bot only; its configured backend model is authoritative.
- Chat actions are consolidated into a compact overflow menu: new chat, active-chat refresh and settings, app settings, and sign-out. Release-signed in-place upgrades preserve the encrypted instance URL, appearance preference, and session cookie; a server-expired or revoked session still requires normal authentication.
- Per-message actions now use the same compact `...` entry point as the web chat. Copy is implemented locally through Android's clipboard; Re-run and Send to Vault stay web-only until the mobile client has explicit, verified mutation contracts for them.
- In progress: mobile chat configuration bootstrap and conversation lifecycle client methods. These provide the native settings and archive/delete work without exposing worker controls.
- Verification: dashboard auth/mobile-contract tests pass, including manifest-backed release metadata. Server publication uses a read-only signing-key mount and explicitly exports the signing secrets into the Android SDK container; the wrapper is invoked through `sh` so publishing does not depend on its executable bit. The current workstation shell does not have `ANDROID_HOME` or Docker Desktop available, so the final containerized release build is verified on the server.

## Risks and Blockers

- Compact work monitoring, attachments, streamed responses, chat settings, and notifications remain after the first chat release.
- Push notifications, offline queues, file uploads, and mutation controls are intentionally deferred until the read/send client is proven.
- The first debug-to-release cutover requires one uninstall/install because Android will not replace a debug-signed package with the persistent release-signed package. Subsequent release-signed updates preserve application data and install in place after Android's standard confirmation.
