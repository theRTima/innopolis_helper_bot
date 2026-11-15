"""Provider-neutral ticket creation and status lookup.

HTTP calls live behind a transport so provider contracts can be tested without
credentials. The caller owns retry policy and persistent deduplication. A
create request with an uncertain outcome must be reconciled before retrying.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid5

TicketKind = Literal["support", "improvement"]


@dataclass(frozen=True)
class TicketPayload:
    title: str
    description: str
    kind: TicketKind
    local_key: str

    def __post_init__(self) -> None:
        if self.kind not in {"support", "improvement"}:
            raise ValueError("Unknown ticket kind")
        if not self.title.strip() or not self.description.strip():
            raise ValueError("Ticket title and description are required")
        if not self.local_key.strip():
            raise ValueError("A stable local ticket key is required")


@dataclass(frozen=True)
class TicketResult:
    external_id: str
    url: str
    status: str
    closed: bool = False


class TicketProviderError(RuntimeError):
    """An API failure without sensitive response bodies or credentials.

    ``uncertain`` means a create request might have succeeded remotely. The
    worker must not retry it automatically without checking the remote state.
    """

    def __init__(
        self, message: str, *, retryable: bool = False, uncertain: bool = False
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain


class TicketProvider(Protocol):
    name: str

    async def create(self, payload: TicketPayload) -> TicketResult: ...

    async def get_status(self, external_id: str) -> TicketResult: ...


class TicketTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class UrllibTicketTransport:
    """Small async wrapper around Python's built-in HTTP client."""

    def __init__(self, timeout: float = 10.0) -> None:
        if timeout <= 0:
            raise ValueError("HTTP timeout must be positive")
        self.timeout = timeout

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        return await asyncio.to_thread(
            self._request, method, url, headers, json_body, form_body
        )

    def _request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        form_body: Mapping[str, str] | None,
    ) -> tuple[int, dict[str, Any]]:
        if json_body is not None and form_body is not None:
            raise ValueError("Choose JSON or form data")
        request_headers = dict(headers)
        data = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif form_body is not None:
            data = urlencode(form_body).encode("utf-8")
            request_headers["Content-Type"] = (
                "application/x-www-form-urlencoded"
            )
        request = Request(
            url, data=data, headers=request_headers, method=method
        )
        opener = build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                status, body = response.status, response.read()
        except HTTPError as error:
            status, body = error.code, error.read()
        except (URLError, TimeoutError, OSError) as error:
            raise TicketProviderError(
                "Ticket provider network request failed",
                retryable=True,
                uncertain=method.upper() != "GET",
            ) from error
        try:
            parsed = json.loads(body) if body else {}
        except (ValueError, UnicodeDecodeError) as error:
            raise TicketProviderError(
                "Ticket provider returned invalid JSON",
                retryable=True,
                uncertain=method.upper() != "GET",
            ) from error
        if not isinstance(parsed, dict):
            raise TicketProviderError(
                "Ticket provider returned a non-object response",
                uncertain=method.upper() != "GET",
            )
        return status, parsed


def _base_url(value: str) -> str:
    result = value.rstrip("/")
    parts = urlsplit(result)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Ticket provider base URL must be HTTP(S)")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(
            "Ticket provider base URL must not contain credentials"
        )
    return result


def _text(value: Any, field: str, *, uncertain: bool = False) -> str:
    if value is None or not str(value).strip():
        raise TicketProviderError(
            f"Ticket provider response is missing {field}", uncertain=uncertain
        )
    return str(value).strip()


class _RemoteProvider:
    name = "remote"

    def __init__(
        self, base_url: str, transport: TicketTransport | None = None
    ) -> None:
        self.base_url = _base_url(base_url)
        self.transport = transport or UrllibTicketTransport()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
        creating: bool = False,
    ) -> dict[str, Any]:
        url = self.base_url + path
        try:
            status, body = await self.transport.request(
                method,
                url,
                headers=headers,
                json_body=json_body,
                form_body=form_body,
            )
        except TicketProviderError as error:
            if creating and not error.uncertain:
                raise TicketProviderError(
                    str(error),
                    retryable=error.retryable,
                    uncertain=True,
                ) from error
            if not creating and error.uncertain:
                raise TicketProviderError(
                    str(error), retryable=error.retryable
                ) from error
            raise
        except Exception as error:
            raise TicketProviderError(
                "Ticket provider network request failed",
                retryable=True,
                uncertain=creating,
            ) from error
        if not 200 <= status < 300:
            raise TicketProviderError(
                f"Ticket provider returned HTTP {status}",
                retryable=status == 429 or status >= 500,
                uncertain=creating and (status == 429 or status >= 500),
            )
        return body


class JiraServiceManagementProvider(_RemoteProvider):
    """Jira Service Management Cloud customer request API."""

    name = "jsm"

    def __init__(
        self,
        base_url: str,
        token: str,
        service_desk_id: str,
        request_type_id: str,
        *,
        email: str = "",
        transport: TicketTransport | None = None,
    ) -> None:
        super().__init__(base_url, transport)
        if not all((token, service_desk_id, request_type_id)):
            raise ValueError(
                "Jira token, desk ID and request type ID are required"
            )
        self.service_desk_id = service_desk_id
        self.request_type_id = request_type_id
        if email:
            secret = base64.b64encode(f"{email}:{token}".encode()).decode()
            self.headers = {"Authorization": f"Basic {secret}"}
        else:
            self.headers = {"Authorization": f"Bearer {token}"}
        self.headers["Accept"] = "application/json"

    def _result(
        self, data: dict[str, Any], *, creating: bool = False
    ) -> TicketResult:
        key = _text(data.get("issueKey") or data.get("issueId"),
                    "issueKey", uncertain=creating)
        links = data.get("_links") or {}
        url = links.get("web") if isinstance(links, dict) else None
        current = data.get("currentStatus") or {}
        status = current.get("status") if isinstance(current, dict) else None
        category = (
            current.get("statusCategory")
            if isinstance(current, dict)
            else None
        )
        label = str(status or "created")
        return TicketResult(
            key,
            str(url or f"{self.base_url}/browse/{quote(key, safe='')}"),
            label,
            str(category or "").upper() == "DONE"
            or label.casefold() in {"done", "closed", "resolved"},
        )

    async def create(self, payload: TicketPayload) -> TicketResult:
        description = f"{payload.description}\n\nRef: {payload.local_key}"
        data = await self._request(
            "POST",
            "/rest/servicedeskapi/request",
            headers=self.headers,
            json_body={
                "serviceDeskId": self.service_desk_id,
                "requestTypeId": self.request_type_id,
                "requestFieldValues": {
                    "summary": payload.title[:255],
                    "description": description,
                },
            },
            creating=True,
        )
        return self._result(data, creating=True)

    async def get_status(self, external_id: str) -> TicketResult:
        data = await self._request(
            "GET",
            f"/rest/servicedeskapi/request/{quote(external_id, safe='')}",
            headers=self.headers,
        )
        return self._result(data)

    async def find_by_local_key(self, local_key: str) -> TicketResult | None:
        """Find a request after an ambiguous create response without creating a duplicate."""
        marker = f"Ref: {local_key}"
        for start in range(0, 200, 50):
            params = urlencode({"serviceDeskId": self.service_desk_id,
                                "start": start, "limit": 50})
            data = await self._request(
                "GET", f"/rest/servicedeskapi/request?{params}",
                headers=self.headers,
            )
            values = data.get("values", [])
            if not isinstance(values, list):
                return None
            for item in values:
                if not isinstance(item, dict):
                    continue
                fields = item.get("requestFieldValues", [])
                descriptions = [field.get("value", "") for field in fields
                                if isinstance(field, dict) and field.get("fieldId") == "description"]
                if any(isinstance(value, str) and marker in value.splitlines()
                       for value in descriptions):
                    return self._result(item)
            if data.get("isLastPage", True) or not values:
                break
        return None


class ZendeskProvider(_RemoteProvider):
    """Zendesk Support tickets API, using an agent or OAuth token."""

    name = "zendesk"

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        email: str = "",
        transport: TicketTransport | None = None,
    ) -> None:
        super().__init__(base_url, transport)
        if not token:
            raise ValueError("Zendesk token is required")
        if email:
            secret = base64.b64encode(
                f"{email}/token:{token}".encode()
            ).decode()
            self.headers = {"Authorization": f"Basic {secret}"}
        else:
            self.headers = {"Authorization": f"Bearer {token}"}
        self.headers["Accept"] = "application/json"

    def _result(
        self, data: dict[str, Any], *, creating: bool = False
    ) -> TicketResult:
        ticket = data.get("ticket")
        if not isinstance(ticket, dict):
            raise TicketProviderError(
                "Zendesk response is missing ticket", uncertain=creating
            )
        ticket_id = _text(ticket.get("id"), "ticket.id", uncertain=creating)
        status = str(ticket.get("status") or "new")
        return TicketResult(
            ticket_id,
            f"{self.base_url}/agent/tickets/{quote(ticket_id, safe='')}",
            status,
            status in {"solved", "closed"},
        )

    async def create(self, payload: TicketPayload) -> TicketResult:
        data = await self._request(
            "POST",
            "/api/v2/tickets",
            headers=self.headers,
            json_body={
                "ticket": {
                    "subject": payload.title[:255],
                    "comment": {"body": payload.description, "public": False},
                    "type": (
                        "incident" if payload.kind == "support" else "problem"
                    ),
                    "external_id": payload.local_key,
                    "tags": ["iskra_bot", payload.kind],
                }
            },
            creating=True,
        )
        return self._result(data, creating=True)

    async def get_status(self, external_id: str) -> TicketResult:
        data = await self._request(
            "GET",
            f"/api/v2/tickets/{quote(external_id, safe='')}",
            headers=self.headers,
        )
        return self._result(data)


class UsedeskProvider(_RemoteProvider):
    """Usedesk UAPI ticket creation and lookup."""

    name = "usedesk"
    STATUSES = {
        "1": "open",
        "2": "completed",
        "3": "closed",
        "4": "deleted",
        "5": "on_hold",
        "6": "pending",
        "7": "spam",
        "8": "new",
        "9": "mailing",
    }

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        channel_id: str = "",
        web_base_url: str = "",
        transport: TicketTransport | None = None,
    ) -> None:
        super().__init__(base_url, transport)
        if not token:
            raise ValueError("Usedesk channel API token is required")
        self.token = token
        self.channel_id = channel_id
        self.web_base_url = (
            _base_url(web_base_url)
            if web_base_url
            else self.base_url.removesuffix("/uapi")
        )

    def _url(self, ticket_id: str) -> str:
        return f"{self.web_base_url}/tickets/{quote(ticket_id, safe='')}"

    async def create(self, payload: TicketPayload) -> TicketResult:
        form = {
            "api_token": self.token,
            "subject": payload.title[:255],
            "message": payload.description,
            "additional_id": payload.local_key[:255],
            "type": "question" if payload.kind == "support" else "problem",
            "private_comment": "true",
        }
        if self.channel_id:
            form["channel_id"] = self.channel_id
        data = await self._request(
            "POST",
            "/create/ticket",
            headers={"Accept": "application/json"},
            form_body=form,
            creating=True,
        )
        if data.get("status") != "success":
            raise TicketProviderError(
                "Usedesk did not confirm ticket creation", uncertain=True
            )
        ticket_id = _text(data.get("ticket_id"), "ticket_id", uncertain=True)
        return TicketResult(ticket_id, self._url(ticket_id), "new")

    async def get_status(self, external_id: str) -> TicketResult:
        data = await self._request(
            "POST",
            "/ticket",
            headers={"Accept": "application/json"},
            form_body={"api_token": self.token, "ticket_id": external_id},
        )
        ticket = data.get("ticket")
        if not isinstance(ticket, dict):
            raise TicketProviderError("Usedesk response is missing ticket")
        ticket_id = _text(ticket.get("id"), "ticket.id")
        raw_status = _text(ticket.get("status_id"), "ticket.status_id")
        status = self.STATUSES.get(raw_status, raw_status)
        return TicketResult(
            ticket_id,
            self._url(ticket_id),
            status,
            status in {"completed", "closed", "deleted"},
        )


class DemoTicketProvider:
    """Deterministic local provider for a demo without credentials."""

    name = "demo"

    def __init__(self) -> None:
        self._statuses: dict[str, str] = {}

    @staticmethod
    def _url(ticket_id: str) -> str:
        return f"demo://tickets/{quote(ticket_id, safe='')}"

    async def create(self, payload: TicketPayload) -> TicketResult:
        digest = uuid5(NAMESPACE_URL, payload.local_key).hex[:10].upper()
        ticket_id = "DEMO-" + digest
        self._statuses.setdefault(ticket_id, "new")
        return TicketResult(
            ticket_id,
            self._url(ticket_id),
            self._statuses[ticket_id],
            self._statuses[ticket_id] in {"closed", "solved", "done"},
        )

    async def get_status(self, external_id: str) -> TicketResult:
        if not external_id.startswith("DEMO-"):
            raise TicketProviderError("Unknown demo ticket")
        return TicketResult(
            external_id,
            self._url(external_id),
            self._statuses.get(external_id, "new"),
            self._statuses.get(external_id) in {"closed", "solved", "done"},
        )

    def set_status(self, external_id: str, status: str) -> None:
        if not external_id.startswith("DEMO-"):
            raise ValueError("Unknown demo ticket")
        self._statuses[external_id] = status


def get_provider_from_env(
    env: Mapping[str, str] | None = None,
    *,
    transport: TicketTransport | None = None,
) -> TicketProvider | None:
    """Select a real provider only when its credentials are configured."""

    values = os.environ if env is None else env
    name = values.get("TICKET_PROVIDER", "").strip().lower()
    base = values.get("TICKET_BASE_URL", "").strip()
    token = values.get("TICKET_API_TOKEN", "").strip()
    if not name:
        return None
    if name == "demo":
        return DemoTicketProvider()
    if name == "jsm":
        return JiraServiceManagementProvider(
            base,
            token,
            values.get("JIRA_SERVICE_DESK_ID", "").strip(),
            values.get("JIRA_REQUEST_TYPE_ID", "").strip(),
            email=values.get("TICKET_EMAIL", "").strip(),
            transport=transport,
        )
    if name == "zendesk":
        return ZendeskProvider(
            base,
            token,
            email=values.get("TICKET_EMAIL", "").strip(),
            transport=transport,
        )
    if name == "usedesk":
        return UsedeskProvider(
            base,
            token,
            channel_id=values.get("USEDESK_CHANNEL_ID", "").strip(),
            web_base_url=values.get("TICKET_WEB_BASE_URL", "").strip(),
            transport=transport,
        )
    raise ValueError("TICKET_PROVIDER must be demo, jsm, zendesk or usedesk")


provider_from_env = get_provider_from_env
