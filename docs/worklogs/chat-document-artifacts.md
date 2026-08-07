# Chat Document Artifacts Worklog

## Objective

Allow a specifically configured chat bot to return a downloadable DOCX artifact when a user explicitly requests one.

## Scope

- Bot-scoped capability only, disabled by default.
- DOCX output from the streamed dashboard chat path.
- No repository, shell, or broad filesystem permission.

## Completion Criteria

- An enabled bot receives an explicit DOCX request and returns a valid downloadable DOCX attachment.
- A disabled bot does not create a DOCX attachment.
- Dashboard bot configuration exposes the capability.
- Focused API, profile, and dashboard tests pass.

## Current State

- Implemented for the streamed dashboard chat path.
- The existing chat attachment viewer exposes the generated file through its Blob-backed download path.
- Bot Detail persists `chat_profile.document_generation`; the capability remains disabled by default.

## Validation

- API coverage verifies an enabled bot returns a generated DOCX attachment, a disabled bot does not, and the generated attachment can be parsed back into readable DOCX text.
- Bot-profile and dashboard rendering coverage verify the configuration control and advertised capability.
- Live chat verification is pending deployment.
