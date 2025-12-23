from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LogCreate(BaseModel):
    host: str
    container: str
    message: str
    severity: str = "error"
    occurred_at: Optional[datetime] = None


class EmailNotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    to_address: str
    subject: str
    body: str
    status: str
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None
    sent_at: Optional[datetime] = None
    created_at: datetime


class EmailRecipientCreate(BaseModel):
    emails: str


class EmailRecipientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    is_active: bool
    created_at: datetime


class EmailNotificationList(BaseModel):
    items: List[EmailNotificationOut]
    total: int


class LogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    host: str
    container: str
    severity: str
    message: str
    log_hash: str
    occurred_at: Optional[datetime] = None
    received_at: datetime
    is_duplicate: bool
    duplicate_of_id: Optional[UUID] = None


class LogDetail(LogOut):
    emails: List[EmailNotificationOut] = Field(default_factory=list)


class LogList(BaseModel):
    items: List[LogOut]
    total: int
