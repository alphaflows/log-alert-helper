from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.db.models import EmailNotification, EmailRecipient, LogRecord
from app.db.session import AsyncSessionLocal, get_session
from app.schemas import (
    EmailNotificationOut,
    EmailNotificationList,
    EmailRecipientCreate,
    EmailRecipientOut,
    LogCreate,
    LogDetail,
    LogList,
    LogOut,
)
from app.services.email import EmailService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/api/recipients", response_model=List[EmailRecipientOut])
async def list_recipients(
    session: AsyncSession = Depends(get_session),
    include_inactive: bool = Query(default=False),
) -> List[EmailRecipientOut]:
    statement = select(EmailRecipient)
    if not include_inactive:
        statement = statement.where(EmailRecipient.is_active.is_(True))
    result = await session.execute(statement.order_by(EmailRecipient.created_at.desc()))
    return result.scalars().all()


@router.post("/api/recipients", response_model=List[EmailRecipientOut])
async def add_recipients(
    payload: EmailRecipientCreate,
    session: AsyncSession = Depends(get_session),
) -> List[EmailRecipientOut]:
    emails = _parse_emails(payload.emails)
    if not emails:
        raise HTTPException(status_code=400, detail="No email addresses provided.")

    existing_result = await session.execute(
        select(EmailRecipient).where(EmailRecipient.email.in_(emails))
    )
    existing = existing_result.scalars().all()
    existing_map = {recipient.email: recipient for recipient in existing}
    created: List[EmailRecipient] = []

    for email in emails:
        recipient = existing_map.get(email)
        if recipient:
            if not recipient.is_active:
                recipient.is_active = True
            created.append(recipient)
            continue
        new_recipient = EmailRecipient(email=email)
        session.add(new_recipient)
        created.append(new_recipient)

    await session.commit()

    for recipient in created:
        await session.refresh(recipient)

    return created


@router.delete("/api/recipients/{recipient_id}", response_model=EmailRecipientOut)
async def deactivate_recipient(
    recipient_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> EmailRecipientOut:
    recipient = await session.get(EmailRecipient, recipient_id)
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")
    recipient.is_active = False
    await session.commit()
    await session.refresh(recipient)
    return recipient


def _hash_message(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _build_email_subject(record: LogRecord) -> str:
    severity = record.severity.upper()
    return f"[Log Alert] {severity} {record.container} on {record.host}"


def _build_email_body(record: LogRecord, settings: Settings) -> str:
    record_url = f"{settings.base_url.rstrip('/')}/api/logs/{record.id}"
    occurred = record.occurred_at.isoformat() if record.occurred_at else "unknown"
    received = record.received_at.isoformat() if record.received_at else "unknown"
    lines = [
        "Log alert received.",
        "",
        f"Host: {record.host}",
        f"Container: {record.container}",
        f"Severity: {record.severity}",
        f"Occurred at: {occurred}",
        f"Received at: {received}",
        f"Record URL: {record_url}",
        "",
        "Message:",
        record.message,
    ]
    return "\n".join(lines)


async def _record_email_history(
    session: AsyncSession,
    record: LogRecord,
    to_address: str,
    subject: str,
    body: str,
    status: str,
    provider_message_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> EmailNotification:
    notification = EmailNotification(
        log_record_id=record.id,
        to_address=to_address,
        subject=subject,
        body=body,
        status=status,
        provider_message_id=provider_message_id,
        error_message=error_message,
        sent_at=datetime.now(timezone.utc) if status == "sent" else None,
    )
    session.add(notification)
    await session.commit()
    await session.refresh(notification)
    return notification


async def _get_recipient_emails(session: AsyncSession, settings: Settings) -> List[str]:
    statement = select(EmailRecipient).where(EmailRecipient.is_active.is_(True))
    recipients_result = await session.execute(statement)
    recipients = recipients_result.scalars().all()
    if recipients:
        return [recipient.email for recipient in recipients]
    return settings.alert_recipients()


async def _maybe_send_alerts(
    session: AsyncSession,
    record: LogRecord,
    settings: Settings,
) -> List[EmailNotification]:
    if record.is_duplicate:
        logger.info("Suppressing duplicate alert for log %s", record.id)
        return [
            await _record_email_history(
                session,
                record,
                "",
                "",
                "",
                status="skipped",
                error_message="duplicate suppressed",
            )
        ]

    recipients = await _get_recipient_emails(session, settings)
    if not settings.alert_email_enabled or not recipients:
        logger.info("Email alerts disabled or no recipients configured.")
        return [
            await _record_email_history(
                session,
                record,
                "",
                "",
                "",
                status="skipped",
                error_message="alerts disabled or missing recipients",
            )
        ]

    email_service = EmailService(settings)
    subject = _build_email_subject(record)
    body = _build_email_body(record, settings)
    notifications: List[EmailNotification] = []

    for to_address in recipients:
        try:
            response = await email_service.send_email(to_address, subject, body)
            message_id = response.get("MessageId") if isinstance(response, dict) else None
            notifications.append(
                await _record_email_history(
                    session,
                    record,
                    to_address,
                    subject,
                    body,
                    status="sent",
                    provider_message_id=message_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to send email to %s", to_address)
            notifications.append(
                await _record_email_history(
                    session,
                    record,
                    to_address,
                    subject,
                    body,
                    status="failed",
                    error_message=str(exc),
                )
            )

    return notifications


def _parse_emails(raw: str) -> List[str]:
    if not raw:
        return []
    emails = [email.strip().lower() for email in raw.split(",")]
    cleaned = [email for email in emails if email]
    invalid = [email for email in cleaned if "@" not in email or "." not in email]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid email(s): {', '.join(sorted(set(invalid)))}",
        )
    unique: List[str] = []
    seen = set()
    for email in cleaned:
        if email in seen:
            continue
        seen.add(email)
        unique.append(email)
    return unique


async def _find_duplicate(
    session: AsyncSession,
    host: str,
    container: str,
    log_hash: str,
    window_seconds: int,
) -> Optional[LogRecord]:
    if window_seconds <= 0:
        return None

    window_start = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    statement = (
        select(LogRecord)
        .where(
            LogRecord.host == host,
            LogRecord.container == container,
            LogRecord.log_hash == log_hash,
            LogRecord.received_at >= window_start,
        )
        .order_by(LogRecord.received_at.desc())
        .limit(1)
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def _send_alerts_background(log_id: UUID) -> None:
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        record = await session.get(LogRecord, log_id)
        if not record:
            logger.warning("Log record %s not found for alert dispatch.", log_id)
            return
        await _maybe_send_alerts(session, record, settings)


@router.post("/api/logs", response_model=LogOut)
async def create_log(
    payload: LogCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> LogOut:
    log_hash = _hash_message(payload.message)
    duplicate = await _find_duplicate(
        session, payload.host, payload.container, log_hash, settings.dedupe_window_seconds
    )

    record = LogRecord(
        host=payload.host,
        container=payload.container,
        severity=payload.severity,
        message=payload.message,
        log_hash=log_hash,
        occurred_at=payload.occurred_at,
        is_duplicate=duplicate is not None,
        duplicate_of_id=duplicate.id if duplicate else None,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    background_tasks.add_task(_send_alerts_background, record.id)

    return record


@router.get("/api/logs", response_model=LogList)
async def list_logs(
    session: AsyncSession = Depends(get_session),
    host: Optional[str] = Query(default=None),
    container: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> LogList:
    statement = select(LogRecord)
    if host:
        statement = statement.where(LogRecord.host == host)
    if container:
        statement = statement.where(LogRecord.container == container)

    count_statement = select(func.count()).select_from(statement.subquery())
    total_result = await session.execute(count_statement)
    total = total_result.scalar_one()

    items_result = await session.execute(
        statement.order_by(LogRecord.received_at.desc()).limit(limit).offset(offset)
    )
    items = items_result.scalars().all()
    return LogList(items=items, total=total)


@router.get("/api/logs/{log_id}", response_model=LogDetail)
async def get_log(
    log_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> LogDetail:
    statement = (
        select(LogRecord)
        .where(LogRecord.id == log_id)
        .options(selectinload(LogRecord.emails))
    )
    record_result = await session.execute(statement)
    record = record_result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Log record not found")
    return record


@router.get("/api/logs/{log_id}/emails", response_model=List[EmailNotificationOut])
async def list_log_emails(
    log_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> List[EmailNotificationOut]:
    statement = select(EmailNotification).where(EmailNotification.log_record_id == log_id)
    result = await session.execute(statement)
    return result.scalars().all()


@router.get("/api/emails", response_model=EmailNotificationList)
async def list_email_history(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> EmailNotificationList:
    statement = select(EmailNotification)
    count_statement = select(func.count()).select_from(statement.subquery())
    total_result = await session.execute(count_statement)
    total = total_result.scalar_one()

    items_result = await session.execute(
        statement.order_by(EmailNotification.created_at.desc()).limit(limit).offset(offset)
    )
    items = items_result.scalars().all()
    return EmailNotificationList(items=items, total=total)
