"""Operator-side user and credential management against a XYZZY database.

The server never mints a credential; an operator does, at the machine holding
the database file. A token is printed exactly once at mint time — only its
digest is stored, so a lost token is revoked, never recovered.

    python -m multiplayer.manage app.db user add alice --email alice@example.com
    python -m multiplayer.manage app.db user erase alice --operator ops
    python -m multiplayer.manage app.db token mint alice --label laptop
    python -m multiplayer.manage app.db token revoke <token-or-hash>
    python -m multiplayer.manage app.db token list
    python -m multiplayer.manage app.db audit verify
    python -m multiplayer.manage app.db db backup backup.db
    python -m multiplayer.manage app.db db migrate
    python -m multiplayer.manage app.db workspace remove-member <workspace_id> <user_id>

Only the server's own startup and ``db migrate`` apply a migration. Every other
verb opens the database as it stands: a read verb (``db backup``, ``token
list``, ``audit verify``) refuses when the schema is behind this checkout
rather than silently migrating a database another process may be serving from,
and a write verb (``user add``, ``user erase``, ``token mint``, ``token
revoke``, ``workspace remove-member``) still needs the schema it writes to.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
from pathlib import Path
from typing import Any

from .db.connection import Database, serialize_datetime
from .db.repositories import RoomMemberRepo, RoomRepo, UserRepo, WorkspaceRepo
from .domain.models import User, utcnow
from .realtime.hub import RealtimeHub
from .security.audit import verify_event_chain
from .security.auth import hash_token
from .services.erasure import ERASURE_OPERATOR_ID
from .services.service import MultiplayerService

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def open_database(db_path: str) -> Database:
    """Open the database, applying every pending migration, the same path the
    server's own boot uses.

    A migration failure must not leave the connection open: aiosqlite's worker
    thread is non-daemon, so a caller that raised out of this function used to
    hang forever instead of exiting with the error already printed.
    """
    db = Database(db_path)
    await db.connect()
    try:
        await MultiplayerService(db, RealtimeHub(), known_users=frozenset()).initialize()
    except BaseException:
        await db.close()
        raise
    return db


async def _pending_migrations(db: Database) -> list[str]:
    """Names of shipped migrations this database has not applied, without
    applying any of them."""
    table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    )
    applied: set[str] = set()
    if table is not None:
        rows = await db.fetch_all("SELECT name FROM schema_migrations")
        applied = {str(row["name"]) for row in rows}
    shipped = {f.name for f in _MIGRATIONS_DIR.glob("*.sql")}
    unknown = sorted(applied - shipped)
    if unknown:
        raise RuntimeError(
            f"database was migrated by a newer build: this checkout does not ship {unknown}"
        )
    return sorted(shipped - applied)


async def open_database_readonly(db_path: str) -> Database:
    """Open the database exactly as it stands, refusing when its schema lags.

    A verb that only reads or appends an audit-adjacent record has no reason
    to run the startup migration pass first: doing so anyway is how a `db
    backup` taken from a newer checkout migrates the live database on the way
    to snapshotting it, and a pre-upgrade backup and the file being backed up
    are supposed to be the same thing that started the process.
    """
    db = Database(db_path)
    await db.connect()
    try:
        pending = await _pending_migrations(db)
    except BaseException:
        await db.close()
        raise
    if pending:
        await db.close()
        raise ValueError(
            f"database schema is behind this checkout by {len(pending)} migration(s), "
            f"starting with {pending[0]!r}; run 'db migrate' first"
        )
    return db


async def add_user(db: Database, user_id: str, email: str, display_name: str | None) -> User:
    existing = await db.fetch_one("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if existing is not None:
        raise ValueError(f"user {user_id!r} already exists")
    user = User(user_id=user_id, display_name=display_name or user_id, email=email)
    return await UserRepo(db).create(user)


async def erase_user(
    db: Database, user_id: str, operator_id: str = ERASURE_OPERATOR_ID
) -> dict[str, Any]:
    """Tombstone a user and redact what they authored. See services/erasure.py.

    ``erase_user`` raises ``DomainError`` for an unknown id or operator, which
    is a ``ValueError`` subclass; ``_run``'s existing ``except ValueError``
    already turns that into the same clean, nonzero-exit refusal every other
    unknown-id error in this CLI gets.
    """
    service = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
    return await service.erase_user(user_id, operator_id)


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


async def backup_database(db: Database, dest: str) -> None:
    """Write a consistent snapshot of the live database to ``dest``.

    A plain file copy of a WAL mode database can miss every write still
    sitting in the ``-wal`` file, so a ``cp`` of the live path silently
    truncates the log this product exists to keep. ``VACUUM INTO`` asks
    SQLite itself for a consistent snapshot, which is safe to run against a
    database another connection is writing to.
    """
    await db.execute("VACUUM INTO ?", (dest,))


async def remove_workspace_member(db: Database, workspace_id: str, user_id: str) -> bool:
    """Remove a workspace member and every room membership they hold in that
    workspace, directly against the tables.

    An operator running this CLI already holds the machine the database file
    lives on, which is a wider trust than any workspace admin the service
    layer's own ``remove_workspace_member`` requires a caller to be; naming
    one here to satisfy that check would be a fiction the row would then
    carry as if a member, not this operator, had decided it.
    """
    member = await WorkspaceRepo(db).get_member(workspace_id, user_id)
    if member is None:
        return False
    for room in await RoomRepo(db).list_by_workspace(workspace_id):
        await RoomMemberRepo(db).remove(room.room_id, user_id)
    await WorkspaceRepo(db).remove_member(workspace_id, user_id)
    return True


_READONLY_VERBS = {
    ("db", "backup"),
    ("token", "list"),
    ("audit", "verify"),
}


async def _run(args: argparse.Namespace) -> int:
    verb = (
        args.command,
        getattr(args, "user_command", None)
        or getattr(args, "token_command", None)
        or getattr(args, "audit_command", None)
        or getattr(args, "db_command", None)
        or getattr(args, "workspace_command", None),
    )
    if verb == ("db", "migrate"):
        db = await open_database(args.db_path)
        await db.close()
        print("migrated")
        return 0
    db = (
        await open_database_readonly(args.db_path)
        if verb in _READONLY_VERBS
        else await open_database(args.db_path)
    )
    try:
        if args.command == "user" and args.user_command == "add":
            user = await add_user(db, args.user_id, args.email, args.display_name)
            print(f"created {user.user_id} <{user.email}>")
        elif args.command == "user" and args.user_command == "erase":
            result = await erase_user(db, args.user_id, args.operator)
            print(
                f"erased {result['user_id']}: {result['redactions']} redaction(s) "
                f"across {len(result['rooms_touched'])} room(s)"
            )
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
        elif args.command == "db" and args.db_command == "backup":
            await backup_database(db, args.dest)
            print(f"backed up to {args.dest}")
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
        elif args.command == "workspace" and args.workspace_command == "remove-member":
            if await remove_workspace_member(db, args.workspace_id, args.user_id):
                print(f"removed {args.user_id} from workspace {args.workspace_id}")
            else:
                print("user is not a workspace member")
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
    user_erase = user.add_parser("erase")
    user_erase.add_argument("user_id")
    user_erase.add_argument("--operator", required=True)

    token = commands.add_parser("token").add_subparsers(dest="token_command", required=True)
    token_mint = token.add_parser("mint")
    token_mint.add_argument("user_id")
    token_mint.add_argument("--label")
    token.add_parser("revoke").add_argument("token_or_hash")
    token.add_parser("list")

    audit = commands.add_parser("audit").add_subparsers(dest="audit_command", required=True)
    audit.add_parser("verify")

    db_commands = commands.add_parser("db").add_subparsers(dest="db_command", required=True)
    db_backup = db_commands.add_parser("backup")
    db_backup.add_argument("dest", help="path to write the backup to")
    db_commands.add_parser("migrate")

    workspace = commands.add_parser("workspace").add_subparsers(
        dest="workspace_command", required=True
    )
    workspace_remove_member = workspace.add_parser("remove-member")
    workspace_remove_member.add_argument("workspace_id")
    workspace_remove_member.add_argument("user_id")

    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
