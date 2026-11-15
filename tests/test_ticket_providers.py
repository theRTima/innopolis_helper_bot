"""Provider contracts use fake transport; no external API is contacted."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import pytest

from app.tickets.providers import (
    DemoTicketProvider,
    JiraServiceManagementProvider,
    TicketPayload,
    TicketProviderError,
    UsedeskProvider,
    ZendeskProvider,
    get_provider_from_env,
)


@dataclass
class FakeTransport:
    responses: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json_body,
                "form": form_body,
            }
        )
        if self.error:
            raise self.error
        return self.responses.pop(0)


PAYLOAD = TicketPayload(
    title="Проблема с фильтром",
    description="Фильтр не принимает оператор.",
    kind="support",
    local_key="support:42",
)


@pytest.mark.asyncio
async def test_jira_create_and_status_contract():
    transport = FakeTransport(
        responses=[
            (
                201,
                {
                    "issueKey": "HELP-42",
                    "_links": {"web": "https://jira.example/portal/HELP-42"},
                    "currentStatus": {"status": "Waiting for Support"},
                },
            ),
            (
                200,
                {
                    "issueKey": "HELP-42",
                    "currentStatus": {
                        "status": "Done",
                        "statusCategory": "DONE",
                    },
                },
            ),
        ]
    )
    provider = JiraServiceManagementProvider(
        "https://jira.example",
        "secret",
        "10",
        "25",
        transport=transport,
    )
    created = await provider.create(PAYLOAD)
    updated = await provider.get_status("HELP-42")
    assert (created.external_id, created.status) == (
        "HELP-42",
        "Waiting for Support",
    )
    assert updated.status == "Done"
    assert updated.closed
    assert transport.calls[0]["url"].endswith("/rest/servicedeskapi/request")
    description = transport.calls[0]["json"]["requestFieldValues"]["description"]
    assert description.endswith("Ref: support:42")
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert transport.calls[1]["method"] == "GET"


@pytest.mark.asyncio
async def test_jira_reconciles_only_exact_local_reference():
    transport = FakeTransport(responses=[(200, {
        "isLastPage": True,
        "values": [
            {"issueKey": "HELP-1", "requestFieldValues": [
                {"fieldId": "description", "value": "Ref: support:420"}]},
            {"issueKey": "HELP-42", "currentStatus": {"status": "Open"},
             "requestFieldValues": [
                 {"fieldId": "description", "value": "Issue details\n\nRef: support:42"}]},
        ],
    })])
    provider = JiraServiceManagementProvider(
        "https://jira.example", "secret", "10", "25", transport=transport
    )
    found = await provider.find_by_local_key("support:42")
    assert found is not None and found.external_id == "HELP-42"
    assert transport.calls[0]["method"] == "GET"
    assert "serviceDeskId=10" in transport.calls[0]["url"]


@pytest.mark.asyncio
async def test_zendesk_create_and_status_contract():
    transport = FakeTransport(
        responses=[
            (201, {"ticket": {"id": 281, "status": "new"}}),
            (200, {"ticket": {"id": 281, "status": "solved"}}),
        ]
    )
    provider = ZendeskProvider(
        "https://help.example.zendesk.com",
        "secret",
        email="agent@example.com",
        transport=transport,
    )
    created = await provider.create(PAYLOAD)
    updated = await provider.get_status(created.external_id)
    assert created.external_id == "281"
    assert created.url.endswith("/agent/tickets/281")
    assert updated.status == "solved"
    assert updated.closed
    body = transport.calls[0]["json"]["ticket"]
    assert body["external_id"] == PAYLOAD.local_key
    assert body["type"] == "incident"
    assert body["comment"]["public"] is False
    assert transport.calls[0]["headers"]["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_usedesk_create_and_status_contract():
    transport = FakeTransport(
        responses=[
            (200, {"status": "success", "ticket_id": 30745888}),
            (200, {"ticket": {"id": "30745888", "status_id": "2"}}),
        ]
    )
    provider = UsedeskProvider(
        "https://secure.usedesk.com/uapi",
        "secret",
        channel_id="20054",
        transport=transport,
    )
    created = await provider.create(PAYLOAD)
    updated = await provider.get_status(created.external_id)
    assert created.external_id == "30745888"
    assert created.url == "https://secure.usedesk.com/tickets/30745888"
    assert updated.status == "completed"
    assert updated.closed
    form = transport.calls[0]["form"]
    assert form["api_token"] == "secret"
    assert form["additional_id"] == PAYLOAD.local_key
    assert form["channel_id"] == "20054"
    assert transport.calls[1]["form"]["ticket_id"] == "30745888"


@pytest.mark.asyncio
async def test_demo_deduplicates_stable_local_key_and_updates_status():
    provider = DemoTicketProvider()
    first = await provider.create(PAYLOAD)
    second = await provider.create(PAYLOAD)
    assert first == second
    provider.set_status(first.external_id, "closed")
    assert (await provider.get_status(first.external_id)).status == "closed"
    assert (
        await DemoTicketProvider().create(PAYLOAD)
    ).external_id == first.external_id


@pytest.mark.asyncio
async def test_ambiguous_create_error_is_marked_uncertain():
    transport = FakeTransport(responses=[(503, {"error": "private detail"})])
    provider = ZendeskProvider(
        "https://help.example.zendesk.com", "secret", transport=transport
    )
    with pytest.raises(TicketProviderError) as captured:
        await provider.create(PAYLOAD)
    assert captured.value.retryable
    assert captured.value.uncertain
    assert "private detail" not in str(captured.value)


@pytest.mark.asyncio
async def test_success_response_without_id_must_not_be_retried_blindly():
    transport = FakeTransport(responses=[(201, {"ticket": {"status": "new"}})])
    provider = ZendeskProvider(
        "https://help.example.zendesk.com", "secret", transport=transport
    )
    with pytest.raises(TicketProviderError) as captured:
        await provider.create(PAYLOAD)
    assert captured.value.uncertain


@pytest.mark.asyncio
async def test_usedesk_read_only_status_failure_is_not_uncertain():
    transport = FakeTransport(
        error=TicketProviderError(
            "network request failed", retryable=True, uncertain=True
        )
    )
    provider = UsedeskProvider(
        "https://secure.usedesk.com/uapi", "secret", transport=transport
    )
    with pytest.raises(TicketProviderError) as captured:
        await provider.get_status("123")
    assert captured.value.retryable
    assert not captured.value.uncertain


def test_configuration_selects_demo_and_requires_real_credentials():
    assert get_provider_from_env({}) is None
    assert isinstance(
        get_provider_from_env({"TICKET_PROVIDER": "demo"}), DemoTicketProvider
    )
    with pytest.raises(ValueError, match="Jira token"):
        get_provider_from_env(
            {
                "TICKET_PROVIDER": "jsm",
                "TICKET_BASE_URL": "https://jira.example",
            }
        )
    with pytest.raises(ValueError, match="base URL"):
        get_provider_from_env(
            {"TICKET_PROVIDER": "zendesk", "TICKET_API_TOKEN": "secret"}
        )


def test_payload_rejects_unstable_or_empty_data():
    with pytest.raises(ValueError, match="stable local"):
        TicketPayload("Problem", "Description", "support", "")
    with pytest.raises(ValueError, match="kind"):
        TicketPayload("Problem", "Description", "unknown", "support:1")
