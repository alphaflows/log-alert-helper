import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class LogRecord(Base):
    __tablename__ = "log_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host = Column(String(255), nullable=False)
    container = Column(String(255), nullable=False)
    severity = Column(String(50), nullable=False, default="error")
    message = Column(Text, nullable=False)
    log_hash = Column(String(64), nullable=False)
    occurred_at = Column(DateTime(timezone=True))
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    is_duplicate = Column(Boolean, nullable=False, server_default=text("false"))
    duplicate_of_id = Column(UUID(as_uuid=True), ForeignKey("log_records.id"))

    emails = relationship(
        "EmailNotification",
        back_populates="log_record",
        cascade="all, delete-orphan",
    )


Index(
    "ix_log_records_dedupe",
    LogRecord.host,
    LogRecord.container,
    LogRecord.log_hash,
    LogRecord.received_at,
)


class EmailNotification(Base):
    __tablename__ = "email_notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    log_record_id = Column(UUID(as_uuid=True), ForeignKey("log_records.id"), nullable=False)
    to_address = Column(String(320), nullable=False)
    subject = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    status = Column(String(32), nullable=False)
    provider_message_id = Column(String(255))
    error_message = Column(Text)
    sent_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    log_record = relationship("LogRecord", back_populates="emails")


class EmailRecipient(Base):
    __tablename__ = "email_recipients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(320), nullable=False, unique=True)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
