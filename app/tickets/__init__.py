"""Ticket-system adapters used by the background integration worker."""

from app.tickets.providers import (
    DemoTicketProvider,
    get_provider_from_env,
    JiraServiceManagementProvider,
    TicketPayload,
    TicketProvider,
    TicketProviderError,
    TicketResult,
    UsedeskProvider,
    ZendeskProvider,
    provider_from_env,
)

__all__ = [
    "DemoTicketProvider",
    "get_provider_from_env",
    "JiraServiceManagementProvider",
    "TicketPayload",
    "TicketProvider",
    "TicketProviderError",
    "TicketResult",
    "UsedeskProvider",
    "ZendeskProvider",
    "provider_from_env",
]
