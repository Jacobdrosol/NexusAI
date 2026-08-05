# NexusAI Android Client

This native Android client connects to a user-owned NexusAI dashboard deployment. It does not require or contact a shared NexusAI service.

## Current Scope

- HTTPS instance onboarding.
- Dashboard account sign-in through `/api/auth/login`.
- Encrypted local storage for the instance URL and dashboard session cookie.
- Session restoration when the app is reopened.

Chat, work monitoring, update enforcement, notifications, and file uploads are follow-up milestones.

## Build

Install Android SDK platform 36 and JDK 17. On Windows PowerShell:

```powershell
$env:ANDROID_HOME = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
.\gradlew.bat :app:assembleDebug
```

The resulting APK is `app/build/outputs/apk/debug/app-debug.apk`.

## Connection Security

The app accepts HTTPS instance URLs only. It stores the configured instance URL and dashboard session cookie using Android encrypted preferences. It never receives the control-plane token, provider credentials, or other server secrets.
