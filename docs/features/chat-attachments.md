# Chat Attachments

Chat accepts up to 15 attachments per message. Source and text files are delivered as bounded text context. Image files are delivered to vision-capable models. PDFs and DOCX files are retained and their readable text is extracted for the model when possible. Other file types are retained for download; the model receives their name, MIME type, and a statement that raw bytes were not inlined.

## Supported handling

- Text and source: `.txt`, `.md`, `.py`, `.cs`, `.fs`, `.cpp`, `.hpp`, `.js`, `.html`, `.css`, `.ps1`, `.sh`, and common configuration, data, and source extensions are read as text.
- Documents: `.pdf` and `.docx` are retained and text is extracted with `pypdf` or `python-docx`.
- Images: existing vision routing remains enforced.
- Other files: retained as bounded binary attachments and available from the attachment viewer as a download.

Raw data retained with a message is capped at 25 MB per file. This protects the dashboard, API, and SQLite chat store from unbounded base64 payloads. Larger files need a dedicated project-vault or object-storage upload flow rather than direct chat attachment storage.

Attachment bytes are delivered to the browser only through a Blob-backed viewer/download path. Image previews remain restricted to safe raster image data URLs.
