"""Transactional email (phase 12 follow-ups: account recovery)."""

from services.email_provider.interface import EmailMessage, EmailProvider, mask_address

__all__ = ["EmailMessage", "EmailProvider", "mask_address"]
