"""Operator-side user and credential management against a MultiAI database.

The server never mints a credential; an operator does, at the machine holding
the database file. A token is printed exactly once at mint time — only its
digest is stored, so a lost token is revoked, never recovered.

    python -m multiplayer.manage app.db user add alice --email alice@example.com
    python -m multiplayer.manage app.db token mint alice --label laptop
    python -m multiplayer.manage app.db token revoke <token-or-hash>
    python -m multiplayer.manage app.db token list
    python -m multiplayer.manage app.db audit verify
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
from typing import Any

from .db.connection import Database, serialize_datetime
from .db.repositories import UserRepo
from .domain.models import User, utcnow
from .realtime.hub import RealtimeHub
from .security.audit import verify_event_chain
from .security.auth import hash_token
from .services.service import MultiplayerService


async def open_database(db_path: str) -> Database:
    """Open the database with the same migration path the server uses."""
    db = Database(db_path)
    await db.connect()
    await MultiplayerService(db, RealtimeHub(), known_users=frozenset()).initialize()
    return db


async def add_user(db: Database, user_id: str, email: str, display_name: str | None) -> User:
    existing = await db.fetch_one("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if existing is not None:
        raise ValueError(f"user {user_id!r} already exists")
    user = User(user_id=user_id, display_name=display_name or user_id, email=email)
    return await UserRepo(db).create(user)


async def mint_token(db: Database, user_id: str, label: str | None) -> str:
    user = await db.fetch_one("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if user is None:
        raise ValueError(f"user {user_id!r} does not exist; create it with 'user add' first")
    token = "mai_" + secrets.token_urlsafe(32)
    await db.execute(
        "INSERT INTO user_tokens(token_hash, user_id, label, created_at) VALUES (?, ?, ?, ?)",
        (hash_token(token), user_id, label, serialize_datetime(utcnow())),
    )
    return token


async def revoke_token(db: Database, token_or_hash: str) -> bool:
    revoked_at = serialize_datetime(utcnow())
    for candidate in (hash_token(token_or_hash), token_or_hash):
        cursor = await db.execute(
            "UPDATE user_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (revoked_at, candidate),
        )
        if cursor.rowcount:
            return True
    return False


async def list_tokens(db: Database) -> list[dict[str, Any]]:
    return await db.fetch_all(
        "SELECT token_hash, user_id, label, created_at, revoked_at "
        "FROM user_tokens ORDER BY created_at"
    )


async def _run(args: argparse.Namespace) -> int:
    db = await open_database(args.db_path)
    try:
        if args.command == "user" and args.user_command == "add":
            user = await add_user(db, args.user_id, args.email, args.display_name)
            print(f"created {user.user_id} <{user.email}>")
        elif args.command == "token" and args.token_command == "mint":
            token = await mint_token(db, args.user_id, args.label)
            print(token)
        elif args.command == "token" and args.token_command == "revoke":
            if await revoke_token(db, args.token_or_hash):
                print("revoked")
            else:
                print("no live credential matched")
                return 1
        elif args.command == "token" and args.token_command == "list":
            for row in await list_tokens(db):
                state = "revoked" if row["revoked_at"] else "live"
                label = row["label"] or "-"
                print(f"{row['token_hash']}  {row['user_id']}  {label}  {state}")
        elif args.command == "audit" and args.audit_command == "verify":
            verified, breaks = await verify_event_chain(db)
            for chain_break in breaks:
                print(
                    f"{chain_break.room_id} seq {chain_break.sequence} "
                    f"{chain_break.event_id}: {chain_break.reason}"
                )
            print(f"{verified} events verified")
            if breaks:
                return 1
    except ValueError as exc:
        print(str(exc))
        return 1
    finally:
        await db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m multiplayer.manage", description=__doc__)
    parser.add_argument("db_path", help="path to the server's SQLite database")
    commands = parser.add_subparsers(dest="command", required=True)

    user = commands.add_parser("user").add_subparsers(dest="user_command", required=True)
    user_add = user.add_parser("add")
    user_add.add_argument("user_id")
    user_add.add_argument("--email", required=True)
    user_add.add_argument("--display-name")

    token = commands.add_parser("token").add_subparsers(dest="token_command", required=True)
    token_mint = token.add_parser("mint")
    token_mint.add_argument("user_id")
    token_mint.add_argument("--label")
    token.add_parser("revoke").add_argument("token_or_hash")
    token.add_parser("list")

    audit = commands.add_parser("audit").add_subparsers(dest="audit_command", required=True)
    audit.add_parser("verify")

    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
