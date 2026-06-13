"""
Pydantic models for the Meta Cloud API webhook payload.

Covers both incoming WhatsApp message webhooks and status-update webhooks
(delivery/read receipts). Use WhatsAppWebhookPayload as the root model.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TextContent(BaseModel):
    """The text body of a WhatsApp text message."""

    body: str


class ButtonReply(BaseModel):
    """The id/title pair returned when a customer taps a reply button."""

    id: str
    title: str


class InteractiveContent(BaseModel):
    """
    Payload for an interactive message (button_reply or list_reply).

    Meta sends type="interactive" when the customer taps a button or
    selects an item from a list message.
    """

    type: str  # "button_reply" | "list_reply"
    button_reply: Optional[ButtonReply] = None
    list_reply: Optional[ButtonReply] = None  # same shape as button_reply


class ImageContent(BaseModel):
    """The image metadata from a WhatsApp image message."""

    id: str
    mime_type: Optional[str] = None
    sha256: Optional[str] = None


class AudioContent(BaseModel):
    """The audio metadata from a WhatsApp audio/voice message."""

    id: str
    mime_type: Optional[str] = None


class WhatsAppMessage(BaseModel):
    """A single message object from Meta's webhook payload."""

    id: str
    from_: str = Field(alias="from")
    timestamp: str
    type: str
    text: Optional[TextContent] = None
    image: Optional[ImageContent] = None
    audio: Optional[AudioContent] = None
    interactive: Optional[InteractiveContent] = None

    model_config = ConfigDict(populate_by_name=True)


class ContactProfile(BaseModel):
    """Sender display name from Meta contacts array."""

    name: str


class Contact(BaseModel):
    """Contact entry in the webhook value."""

    profile: Optional[ContactProfile] = None
    wa_id: str


class WebhookMetadata(BaseModel):
    """Phone number metadata included in every webhook value."""

    display_phone_number: str
    phone_number_id: str


class WebhookValue(BaseModel):
    """
    The 'value' object inside each change entry.

    messages may be None for status update webhooks (delivery/read receipts).
    """

    messaging_product: str
    metadata: WebhookMetadata
    contacts: Optional[list[Contact]] = None
    messages: Optional[list[WhatsAppMessage]] = None


class WebhookChange(BaseModel):
    """A single change entry inside an 'entry' object."""

    value: WebhookValue
    field: str


class WebhookEntry(BaseModel):
    """Top-level entry in the Meta webhook payload."""

    id: str
    changes: list[WebhookChange]


class WhatsAppWebhookPayload(BaseModel):
    """
    Root Pydantic model for the Meta Cloud API webhook POST body.

    Covers both message webhooks and status-update webhooks.
    """

    object: str
    entry: list[WebhookEntry]

    def get_first_message(self) -> Optional[WhatsAppMessage]:
        """
        Safely traverse the nested structure to return the first message.

        Returns:
            WhatsAppMessage if a message exists, else None.
        """
        try:
            messages = self.entry[0].changes[0].value.messages
            if messages:
                return messages[0]
        except (IndexError, AttributeError):
            pass
        return None

    def get_sender_phone(self) -> Optional[str]:
        """
        Return the sender's phone number from the first message.

        Returns:
            Phone number string (E.164 without '+') or None.
        """
        msg = self.get_first_message()
        return msg.from_ if msg else None
