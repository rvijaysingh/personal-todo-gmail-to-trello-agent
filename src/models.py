"""Shared data structures for the Gmail-to-Trello agent."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EmailRecord:
    """Represents a starred Gmail email extracted for processing."""

    gmail_message_id: str
    subject: str
    sender: str
    email_date: str
    body: str


@dataclass
class CardPayload:
    """Data needed to create a Trello card."""

    name: str
    description: str
    card_name_source: str  # 'llm' or 'fallback'


@dataclass
class ProcessingResult:
    """Outcome of processing a single email."""

    gmail_message_id: str
    status: str
    trello_card_id: Optional[str] = None
    trello_card_url: Optional[str] = None
    error_message: Optional[str] = None
