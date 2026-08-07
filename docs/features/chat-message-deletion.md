# Chat Message-Pair Deletion

## Purpose

Chat users can permanently remove an accidental normal user/assistant turn without deleting the entire conversation. The transcript retains one `Message deleted` placeholder for each side so ordering remains understandable.

## Behavior

- Selecting `Delete pair` from either a normal user message or its assistant response deletes the full turn.
- The action requires an explicit destructive confirmation and cannot be restored.
- Original message content, attachments, and local conversation-memory vectors are removed from the active chat context.
- Automatic project chat-vault records for the deleted message IDs are removed from every project scoped to that conversation.
- Existing personal memory-profile records are intentionally retained. Memory has its own lifecycle and is managed from the Memory page.
- Assignment and PM workflow messages are excluded from this action to avoid invalidating orchestration records.
- A failed user delivery has no assistant response. It is shown as a failed turn with `Retry` and `Delete failed message`; deleting it leaves one placeholder and removes it from retrieval/context.

## Data Flow

`DELETE /v1/chat/conversations/{conversation_id}/messages/{message_id}` invokes `ChatManager.delete_message_pair`.

The manager finds the normal user turn and its contiguous normal assistant response or response variants, replaces each row with a placeholder and deletion metadata, removes `chat_message_memory` rows, and deletes matching vault items by their automatic `chat-message:{message_id}` source reference. A delivery failure is handled as a one-message turn. Model payload construction excludes messages whose metadata has `deleted: true` or `delivery_failed: true`.

## Failure Behavior

The chat transcript deletion succeeds even if optional vault cleanup has a transient error; the error is logged. The deleted content remains excluded from future model context and local chat semantic retrieval. Operators should resolve the logged vault failure if project retrieval storage needs reconciliation.
