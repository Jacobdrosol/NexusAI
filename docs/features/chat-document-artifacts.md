# Chat DOCX Artifacts

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

## Formatting-Preserving DOCX Editing

Set `routing_rules.chat_profile.document_editing` to `true` for a specific bot to allow a more constrained operation: editing an attached DOCX rather than generating a new one. This capability is disabled by default and is separate from document generation.

NexusAI uses this path only when all of the following are true:

1. The selected bot has document editing enabled.
2. The current message contains a retained `.docx` attachment with its raw bytes available.
3. The user explicitly asks to edit, tailor, revise, update, or preserve the document's formatting or layout.

The model returns a bounded JSON plan containing exact existing paragraph text and replacement text. NexusAI applies matched replacements to a copy of the original DOCX. Paragraph-level formatting, margins, section structure, list numbering, headers, footers, and all untouched OOXML remain intact. The original attachment is never modified.

This is intentionally not a general-purpose Word editor. Editing a paragraph with multiple independently formatted inline runs can flatten the inline run formatting in that individual paragraph, and the edit safely fails when the model's requested target text does not exactly match the source document. The edit-plan JSON is not streamed into the chat UI; the user receives a normal confirmation and the resulting attachment.
