# Chat Document Artifacts

## Purpose

An explicitly enabled chat bot can return a generated DOCX as an attachment to its final response. This is for normal chat deliverables such as resumes, letters, reports, and drafts. It does not grant repository access, shell access, or arbitrary filesystem writes.

## Bot Gate

Set `routing_rules.chat_profile.document_generation` to `true` for the individual bot. The Bot Detail page exposes this as **Document generation (.docx)**. It is disabled by default for every bot.

NexusAI creates a document only when both conditions are true:

1. The selected bot has the capability enabled.
2. The user explicitly requests a DOCX, a Word document, or Microsoft Word output in the current message.

## Flow

1. The model receives an instruction to provide the complete deliverable content as Markdown-like text.
2. NexusAI deterministically converts the final response to DOCX with headings, bullet lists, numbered lists, paragraphs, and code blocks preserved in an appropriate basic form.
3. The generated DOCX is stored in the assistant message metadata as a `document` attachment and appears in the existing attachment viewer/download control.

Generated DOCX files follow the normal 25 MB retained-attachment limit. The model does not receive an unrestricted file-writing tool. The first version intentionally supports DOCX only; other output formats require separate, explicit artifact handlers.
