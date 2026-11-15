"""Admin tools for access users and Telegram invite links."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from fastapi_amis_admin.admin.admin import AdminAction, ModelAdmin
from fastapi_amis_admin.admin.extensions.admin import ReadOnlyModelAdmin
from fastapi_amis_admin.amis.components import ActionType

from app.storage.models import (
    AccessInvite,
    AccessInviteUse,
    AllowedTelegramUser,
)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 1000
TELEGRAM_USERNAME = re.compile(r"[a-z0-9_]{1,32}")
BOT_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")


def _normalize_username(value: Any) -> str | None:
    if value is None:
        return None
    username = str(value).strip().lstrip("@").lower()
    if not username:
        return None
    if not TELEGRAM_USERNAME.fullmatch(username):
        raise ValueError("Invalid Telegram username")
    return username


def _parse_telegram_id(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Telegram ID must be a positive integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("Telegram ID must be a positive integer")
        value = int(value)
    try:
        telegram_id = int(str(value).strip())
    except ValueError as error:
        raise ValueError("Telegram ID must be a positive integer") from error
    if not 0 < telegram_id < 2**63:
        raise ValueError("Telegram ID must be a positive integer")
    return telegram_id


def _read_rows(filename: str, content: bytes) -> list[tuple[int, list[Any]]]:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix == "csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("CSV must be encoded as UTF-8") from error
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = []
        for number, row in enumerate(csv.reader(io.StringIO(text), dialect), 1):
            if number > MAX_IMPORT_ROWS + 1:
                raise ValueError(f"Upload at most {MAX_IMPORT_ROWS} rows at a time")
            rows.append((number, row))
        return rows
    if suffix == "xlsx":
        try:
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            try:
                sheet = workbook.active
                rows = []
                for number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                    if number > MAX_IMPORT_ROWS + 1:
                        raise ValueError(f"Upload at most {MAX_IMPORT_ROWS} rows at a time")
                    rows.append((number, list(row)))
                return rows
            finally:
                workbook.close()
        except Exception as error:
            raise ValueError(f"Could not read XLSX: {error}") from error
    raise ValueError("Use an .xlsx or .csv file")


def parse_user_import(filename: str, content: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    rows = _read_rows(filename, content)
    if not rows:
        raise ValueError("The file has no rows")

    headers = [str(value or "").strip().lower().replace(" ", "_") for value in rows[0][1]]
    username_names = {"username", "telegram_username", "telegram_user", "user_name"}
    id_names = {"telegram_id", "user_id", "telegram_user_id"}
    has_header = any(header in username_names | id_names for header in headers)
    if has_header:
        username_column = next((i for i, header in enumerate(headers) if header in username_names), None)
        id_column = next((i for i, header in enumerate(headers) if header in id_names), None)
        data_rows = rows[1:]
    else:
        username_column, id_column, data_rows = 0, 1, rows

    if username_column is None and id_column is None:
        raise ValueError("Add a username or telegram_id column")

    parsed: list[dict[str, Any]] = []
    errors: list[str] = []
    if len(data_rows) > MAX_IMPORT_ROWS:
        raise ValueError(f"Upload at most {MAX_IMPORT_ROWS} rows at a time")
    for row_number, row in data_rows:
        if not row or not any(value is not None and str(value).strip() for value in row):
            continue
        try:
            username = _normalize_username(
                row[username_column] if username_column is not None and username_column < len(row) else None
            )
            telegram_id = _parse_telegram_id(
                row[id_column] if id_column is not None and id_column < len(row) else None
            )
            if username is None and telegram_id is None:
                raise ValueError("Provide a username, telegram_id, or both")
            parsed.append({"username": username, "telegram_id": telegram_id})
        except ValueError as error:
            errors.append(f"Row {row_number}: {error}")
    return parsed, errors


def _page(title: str, body: str, *, message: str = "") -> HTMLResponse:
    notice = f'<div class="notice">{html.escape(message)}</div>' if message else ""
    return HTMLResponse(
        """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>"""
        + html.escape(title)
        + """ · Bot Admin</title><style>
body{margin:0;padding:32px;background:#f5f7fb;color:#172033;font:15px/1.5 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:900px;margin:auto;background:white;padding:32px;border:1px solid #e5eaf2;border-radius:14px;box-shadow:0 12px 40px #25385c10}
h1{font-size:24px;margin:0 0 8px}p,small{color:#68748a}.notice{margin:18px 0;padding:12px 14px;background:#edf7f0;border:1px solid #cce8d2;border-radius:8px;color:#24643b;overflow-wrap:anywhere}
form{margin:22px 0;padding:18px;background:#f8f9fc;border:1px solid #e5eaf2;border-radius:10px}
label{display:block;margin:12px 0 6px;font-weight:600}input,button{font:inherit}input[type=file],input[type=number]{display:block;margin:6px 0 14px;padding:10px;border:1px solid #d9e0eb;border-radius:7px;background:white}
button{border:0;border-radius:8px;background:#315efb;color:#fff;padding:10px 16px;font-weight:650;cursor:pointer}button.danger{background:#b83c46;padding:6px 10px}a{color:#315efb}table{width:100%;border-collapse:collapse;margin-top:20px}th,td{text-align:left;padding:10px;border-bottom:1px solid #e7ebf2}code{overflow-wrap:anywhere}
.errors{color:#a33}.errors li{margin:4px 0}.actions{display:flex;gap:12px;align-items:center}
</style></head><body><main><h1>"""
        + html.escape(title)
        + "</h1>"
        + notice
        + body
        + "</main></body></html>",
        headers={"Cache-Control": "no-store"},
    )


def register_access_routes(
    app: FastAPI,
    *,
    sessions: async_sessionmaker,
    admin_username: str,
    bot_username: str,
) -> None:
    @app.get("/access-users/import", response_class=HTMLResponse)
    async def import_users_page():
        body = """<p>Upload a CSV or XLSX spreadsheet. Use columns <code>username</code> and optionally <code>telegram_id</code>; one-column username files also work. Usernames may include @. Imports add new access entries and skip identities already present.</p>
<p><a href="/access-users/template.csv">Download CSV template</a> · Maximum 5 MiB and 1,000 rows per upload.</p>
<form method="post" action="/access-users/import" enctype="multipart/form-data"><label for="spreadsheet">Spreadsheet</label><input id="spreadsheet" name="spreadsheet" type="file" accept=".csv,.xlsx" required><button type="submit">Import users</button></form>
<p><a href="/admin/">Back to administration</a></p>"""
        return _page("Import allowed users", body)

    @app.get("/access-users/template.csv")
    async def import_users_template():
        return Response(
            "username,telegram_id\n",
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="allowed-users-template.csv"'},
        )

    @app.post("/access-users/import", response_class=HTMLResponse)
    async def import_users(spreadsheet: UploadFile = File(...)):
        filename = spreadsheet.filename or ""
        content = await spreadsheet.read(MAX_UPLOAD_BYTES + 1)
        await spreadsheet.close()
        if len(content) > MAX_UPLOAD_BYTES:
            return _page("Import allowed users", "<p class=errors>File exceeds the 5 MiB limit.</p>")
        try:
            entries, errors = parse_user_import(filename, content)
        except ValueError as error:
            return _page("Import allowed users", f"<p class=errors>{html.escape(str(error))}</p>")
        if not entries and errors:
            items = "".join(f"<li>{html.escape(error)}</li>" for error in errors[:30])
            return _page("Import allowed users", f"<p>No users were imported.</p><ul class=errors>{items}</ul>")

        names = {entry["username"] for entry in entries if entry["username"]}
        ids = {entry["telegram_id"] for entry in entries if entry["telegram_id"]}
        predicates = []
        if names:
            predicates.append(AllowedTelegramUser.username.in_(names))
        if ids:
            predicates.append(AllowedTelegramUser.telegram_id.in_(ids))
        added = skipped = 0
        try:
            async with sessions() as session, session.begin():
                existing = (await session.scalars(select(AllowedTelegramUser).where(or_(*predicates)))).all() if predicates else []
                existing_names = {row.username for row in existing if row.username}
                existing_ids = {row.telegram_id for row in existing if row.telegram_id}
                seen_names: set[str] = set()
                seen_ids: set[int] = set()
                for entry in entries:
                    name, telegram_id = entry["username"], entry["telegram_id"]
                    if (
                        (name and (name in existing_names or name in seen_names))
                        or (telegram_id and (telegram_id in existing_ids or telegram_id in seen_ids))
                    ):
                        skipped += 1
                        continue
                    session.add(AllowedTelegramUser(username=name, telegram_id=telegram_id))
                    if name:
                        seen_names.add(name)
                    if telegram_id:
                        seen_ids.add(telegram_id)
                    added += 1
        except IntegrityError:
            return _page(
                "Import allowed users",
                "<p class=errors>Import conflicted with a simultaneous change. No rows were added; please retry.</p>",
            )

        summary = f"Added {added} user(s); skipped {skipped} duplicate(s); {len(errors)} invalid row(s)."
        error_list = "".join(f"<li>{html.escape(error)}</li>" for error in errors[:30])
        body = (
            '<p><a href="/access-users/import">Import another file</a> · '
            '<a href="/admin/">Back to administration</a></p>'
            + (f"<ul class=errors>{error_list}</ul>" if error_list else "")
        )
        return _page("Import allowed users", body, message=summary)

    @app.get("/access-invites", response_class=HTMLResponse)
    async def invite_page():
        async with sessions() as session:
            invites = (await session.scalars(select(AccessInvite).order_by(AccessInvite.created_at.desc()).limit(100))).all()
        rows = []
        now = datetime.now(timezone.utc)
        for invite in invites:
            expiry = invite.expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if invite.revoked_at:
                state = "revoked"
            elif expiry <= now:
                state = "expired"
            elif invite.use_count >= invite.max_uses:
                state = "fully used"
            else:
                state = "active"
            revoke = ""
            if state == "active":
                revoke = (
                    f'<form method="post" action="/access-invites/{invite.id}/revoke">'
                    '<button class="danger" type="submit">Revoke</button></form>'
                )
            rows.append(
                "<tr>"
                f"<td>{invite.id}</td><td>{html.escape(invite.created_by)}</td>"
                f"<td>{expiry.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</td>"
                f"<td>{invite.use_count} / {invite.max_uses}</td><td>{state}</td><td>{revoke}</td></tr>"
            )
        table = (
            "<table><thead><tr><th>ID</th><th>Created by</th><th>Expires</th>"
            "<th>Uses</th><th>Status</th><th></th></tr></thead><tbody>"
            + ("".join(rows) or '<tr><td colspan="6">No invitations yet.</td></tr>')
            + "</tbody></table>"
        )
        bot_ok = bool(BOT_USERNAME.fullmatch(bot_username))
        form = (
            "<p>Create a deep link and send it to the person. When they open it in Telegram, "
            "the bot validates the token, adds their account to the allowlist and records the activation.</p>"
        )
        if bot_ok:
            form += """<form method="post" action="/access-invites"><label for="hours">Expires in (hours, 1–720)</label><input id="hours" name="expires_hours" type="number" min="1" max="720" value="24" required><label for="uses">Maximum activations (1–1,000)</label><input id="uses" name="max_uses" type="number" min="1" max="1000" value="1" required><button type="submit">Create invite link</button></form>"""
        else:
            form += '<p class="errors">Set TELEGRAM_BOT_USERNAME to enable invite creation.</p>'
        form += '<p><a href="/admin/">Back to administration</a></p>' + table
        return _page("Telegram invitations", form)

    @app.post("/access-invites", response_class=HTMLResponse)
    async def create_invite(
        expires_hours: int = Form(24),
        max_uses: int = Form(1),
    ):
        if not BOT_USERNAME.fullmatch(bot_username):
            raise HTTPException(status_code=503, detail="TELEGRAM_BOT_USERNAME is not configured")
        if not 1 <= expires_hours <= 720 or not 1 <= max_uses <= 1000:
            raise HTTPException(status_code=422, detail="Invite expiry or use limit is out of range")
        token = secrets.token_urlsafe(24)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
        async with sessions() as session, session.begin():
            session.add(
                AccessInvite(
                    token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    created_by=admin_username,
                    expires_at=expires_at,
                    max_uses=max_uses,
                )
            )
        invite_url = f"https://t.me/{bot_username}?start=invite_{token}"
        body = (
            "<p>Send this link to the user. It is shown only once; the database stores only its hash.</p>"
            f'<div class="notice"><a href="{html.escape(invite_url, quote=True)}">Open in Telegram</a> '
            f'<code>{html.escape(invite_url)}</code> '
            f'<button type="button" data-link="{html.escape(invite_url, quote=True)}" '
            "onclick=\"navigator.clipboard.writeText(this.dataset.link)\">Copy link</button></div>"
            f'<p>Expires {expires_at.strftime("%Y-%m-%d %H:%M UTC")}; '
            f"up to {max_uses} activation(s).</p>"
            '<p><a href="/access-invites">Back to invitations</a></p>'
        )
        return _page("Invite link created", body)

    @app.post("/access-invites/{invite_id}/revoke")
    async def revoke_invite(invite_id: int):
        async with sessions() as session, session.begin():
            invite = await session.get(AccessInvite, invite_id)
            if invite is not None and invite.revoked_at is None:
                invite.revoked_at = datetime.now(timezone.utc)
        return RedirectResponse("/access-invites", status_code=303)


def register_access_admins(site, engine) -> None:
    access_engine = engine

    @site.register_admin
    class AllowedTelegramUserAdmin(ModelAdmin):
        page_schema = "Allowed Telegram users"
        model = AllowedTelegramUser
        engine = access_engine
        create_fields = [
            AllowedTelegramUser.username,
            AllowedTelegramUser.telegram_id,
            AllowedTelegramUser.is_active,
        ]
        update_fields = create_fields
        create_exclude = {"id", "created_at"}
        update_exclude = {"id", "created_at"}
        list_display = [
            AllowedTelegramUser.id,
            AllowedTelegramUser.username,
            AllowedTelegramUser.telegram_id,
            AllowedTelegramUser.is_active,
            AllowedTelegramUser.created_at,
        ]
        search_fields = [AllowedTelegramUser.username]
        admin_action_maker = [
            lambda admin: AdminAction(
                admin,
                name="import_users",
                action=ActionType.Url(
                    label="Import spreadsheet",
                    url="/access-users/import",
                    blank=True,
                ),
                flags=["toolbar"],
            ),
            lambda admin: AdminAction(
                admin,
                name="create_invite",
                action=ActionType.Url(
                    label="Create invite link",
                    url="/access-invites",
                    blank=True,
                ),
                flags=["toolbar"],
            ),
        ]

    @site.register_admin
    class AccessInviteAdmin(ReadOnlyModelAdmin):
        page_schema = "Invitations"
        model = AccessInvite
        engine = access_engine
        list_display = [
            AccessInvite.id,
            AccessInvite.created_by,
            AccessInvite.expires_at,
            AccessInvite.max_uses,
            AccessInvite.use_count,
            AccessInvite.revoked_at,
            AccessInvite.created_at,
        ]
        admin_action_maker = [
            lambda admin: AdminAction(
                admin,
                name="create_invite",
                action=ActionType.Url(
                    label="Create invitation",
                    url="/access-invites",
                    blank=False,
                ),
                flags=["toolbar"],
            )
        ]

    @site.register_admin
    class AccessInviteUseAdmin(ReadOnlyModelAdmin):
        page_schema = "Invite activations"
        model = AccessInviteUse
        engine = access_engine
        list_display = [
            AccessInviteUse.id,
            AccessInviteUse.invite_id,
            AccessInviteUse.telegram_id,
            AccessInviteUse.username,
            AccessInviteUse.used_at,
        ]
