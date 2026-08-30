-- A message may carry what it did not type.
--
-- Every payload this schema has stored for a room is text a participant wrote
-- or a fact the server derived from text. A file is neither: it is bytes a
-- member uploads before they know which message, if any, will claim it, and
-- the bytes themselves must never become model input the way message content
-- already screened for that is. attachments is its own table rather than a
-- blob column on messages so that an upload can exist unbound (message_id
-- NULL) between the POST that stores it and the send that claims it, and so
-- that a room's own scoping (room_id) and a reader's own capability check
-- (which the GET route re-derives from room_id, never from the row alone)
-- apply to it the same way they apply to everything else in the room.
--
-- sha256 is recorded at upload, not computed on read, so a later dispute over
-- what was actually served has a fixed answer. content_type is stored exactly
-- as the uploader sent it and is never trusted at serve time: what gets served
-- with its stored type and what gets served as application/octet-stream is a
-- decision the serving code makes from an allowlist, not a fact this row
-- asserts about itself.
CREATE TABLE attachments (
    attachment_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    uploader_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    message_id TEXT REFERENCES messages(message_id),
    data BLOB NOT NULL
);

CREATE INDEX idx_attachments_room ON attachments(room_id);
-- Binding looks an unbound upload up by id within a room; a bound one is
-- looked up by the message that claimed it, to render the message's metadata.
CREATE INDEX idx_attachments_message ON attachments(message_id);
