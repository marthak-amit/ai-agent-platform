"""
Pydantic models for the Instagram webhook payload.

Handles two event types:
- Direct messages  → entry[].messaging[]
- Post comments    → entry[].changes[].field == "comments"

Image DMs arrive with type="image" and an attachments list instead of text.
Story replies with images use the same messaging structure.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class InstagramAttachmentPayload(BaseModel):
    """Payload block inside an image attachment."""

    url: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class InstagramAttachment(BaseModel):
    """One item in message.attachments — covers image, video, story_mention, etc."""

    type: Optional[str] = None
    payload: Optional[InstagramAttachmentPayload] = None

    model_config = ConfigDict(extra="allow")


class InstagramMessage(BaseModel):
    """A single DM payload inside a messaging entry."""

    mid: str
    text: Optional[str] = None
    # type is "text" for text messages, "image" for image DMs
    type: Optional[str] = None
    attachments: Optional[list[InstagramAttachment]] = None

    def get_image_url(self) -> Optional[str]:
        """Return the image CDN URL from the first attachment, or None."""
        if self.attachments:
            first = self.attachments[0]
            if first.payload:
                return first.payload.url
        return None

    def get_audio_url(self) -> Optional[str]:
        """Return the audio CDN URL from the first attachment, or None."""
        if self.attachments:
            first = self.attachments[0]
            if first.type == "audio" and first.payload:
                return first.payload.url
        return None

    model_config = ConfigDict(extra="allow")


class InstagramMessaging(BaseModel):
    """One DM event inside entry.messaging."""

    sender: dict
    recipient: dict
    timestamp: int
    message: Optional[InstagramMessage] = None

    def get_sender_id(self) -> Optional[str]:
        """Return the sender IGSID, or None."""
        return self.sender.get("id")

    def get_text(self) -> Optional[str]:
        """Return the DM text, or None."""
        return self.message.text if self.message else None

    def get_message_type(self) -> str:
        """Return the message type: 'text', 'image', 'audio', or 'unknown'."""
        if self.message is None:
            return "unknown"
        if self.message.type:
            return self.message.type
        # Infer from presence of attachments when type field is absent
        if self.message.attachments:
            first = self.message.attachments[0]
            if first.type == "image":
                return "image"
            if first.type == "audio":
                return "audio"
        return "text"


class CommentFrom(BaseModel):
    """Commenter identity inside a comment change value."""

    id: str
    username: Optional[str] = None


class CommentValue(BaseModel):
    """Payload of a single comment change."""

    from_: CommentFrom = Field(alias="from")
    media: Optional[dict] = None
    id: str
    text: str

    model_config = ConfigDict(populate_by_name=True)


class CommentChange(BaseModel):
    """One item inside entry.changes for comment events."""

    field: str
    value: CommentValue


class InstagramEntry(BaseModel):
    """
    Top-level entry in the Instagram webhook payload.

    May contain either messaging (DMs) or changes (comments), not both.
    """

    id: str
    messaging: Optional[list[InstagramMessaging]] = None
    changes: Optional[list[CommentChange]] = None


class InstagramWebhookPayload(BaseModel):
    """Root model for Instagram webhook POST body."""

    object: str
    entry: list[InstagramEntry]

    def get_first_dm(self) -> Optional[InstagramMessaging]:
        """Return the first DM event, or None."""
        try:
            msgs = self.entry[0].messaging
            if msgs:
                return msgs[0]
        except (IndexError, AttributeError):
            pass
        return None

    def get_first_comment(self) -> Optional[CommentChange]:
        """Return the first comment change, or None."""
        try:
            changes = self.entry[0].changes
            if changes:
                return next(
                    (c for c in changes if c.field == "comments"), None
                )
        except (IndexError, AttributeError):
            pass
        return None

    def get_ig_user_id(self) -> Optional[str]:
        """Return the Instagram Business Account ID from the first entry."""
        try:
            return self.entry[0].id
        except IndexError:
            return None
