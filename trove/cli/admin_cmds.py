"""`trove admin` subcommands — account management from the CLI.

Operates directly on the central app.db under the config home
(``~/.trove/app.db`` by default), mirroring what the web admin console
does over the API. Intended for initial setup and emergencies (e.g.
resetting a lost admin password) without booting the server.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sys
from pathlib import Path

from trove.core.config import ConfigLoader
from trove.core.logging import get_logger
from trove.services.auth.service import AuthService

logger = get_logger(__name__)


def _load_auth() -> AuthService:
    config = ConfigLoader.load_agent_config(None)
    home = Path(config.home).expanduser()
    return AuthService(home / "app.db")


def _print_user(u: dict) -> None:
    flags = "admin" if u["role"] == "admin" else "user"
    if u["disabled"]:
        flags += ", disabled"
    print(f"  #{u['id']:<4} {u['username']:<24} {flags:<20} {u['display_name']}")


async def _cmd_create_user(args) -> None:
    auth = _load_auth()
    if args.password:
        password = args.password
    elif args.prompt:
        password = getpass.getpass(f"Password for '{args.username}': ")
        if not password:
            print("error: empty password", file=sys.stderr)
            sys.exit(1)
    else:
        password = secrets.token_urlsafe(15)[:20]
        print(f"Generated password: {password}")
    try:
        user = await auth.create_user(
            args.username, password, role="admin" if args.admin else "user",
            display_name=args.display_name or "",
        )
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    await auth.record_audit("admin.user.create", user=user, details={"source": "cli"})
    print(f"Created user: {user['username']} (id={user['id']}, role={user['role']})")


async def _cmd_reset_password(args) -> None:
    auth = _load_auth()
    user = await auth.store.get_user_by_username(args.username)
    if user is None:
        print(f"error: no such user: {args.username}", file=sys.stderr)
        sys.exit(1)
    if args.password:
        password = args.password
    else:
        password = secrets.token_urlsafe(15)[:20]
        print(f"Generated password: {password}")
    await auth.update_user(user["id"], password=password)
    await auth.record_audit(
        "admin.user.update", user=user, details={"reason": "password reset"}
    )
    print(f"Password reset for: {args.username}")


async def _cmd_list_users(args) -> None:
    auth = _load_auth()
    users = await auth.list_users()
    if not users:
        print("No users yet.")
        return
    for u in users:
        _print_user(u)


async def _cmd_disable_user(args) -> None:
    auth = _load_auth()
    user = await auth.store.get_user_by_username(args.username)
    if user is None:
        print(f"error: no such user: {args.username}", file=sys.stderr)
        sys.exit(1)
    try:
        await auth.update_user(user["id"], disabled=not args.enable)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    await auth.record_audit(
        "admin.user.update", user=user,
        details={"disabled": not args.enable},
    )
    print(f"{'Enabled' if args.enable else 'Disabled'} user: {args.username}")


async def _cmd_grant(args) -> None:
    auth = _load_auth()
    user = await auth.store.get_user_by_username(args.username)
    if user is None:
        print(f"error: no such user: {args.username}", file=sys.stderr)
        sys.exit(1)
    current = await auth.get_datasources(user["id"])
    if args.revoke:
        if args.datasource not in current:
            print(f"error: {args.username} has no grant for {args.datasource}", file=sys.stderr)
            sys.exit(1)
        updated = [ds for ds in current if ds != args.datasource]
    else:
        if args.datasource in current:
            print(f"already granted: {args.username} → {args.datasource}")
            return
        updated = sorted(current + [args.datasource])
    await auth.set_datasources(user["id"], updated)
    await auth.record_audit(
        "admin.grant.set", user=user,
        details={"datasources": updated},
    )
    print(f"{'Revoked' if args.revoke else 'Granted'}: {args.username} → {args.datasource}")


async def run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="trove admin", description="Trove account management"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-user", help="create a user account")
    p.add_argument("username")
    p.add_argument("--admin", action="store_true", help="grant admin role")
    p.add_argument("--password", help="set password (default: generated)")
    p.add_argument("--prompt", action="store_true", help="prompt for password")
    p.add_argument("--display-name", default="")
    p.set_defaults(func=_cmd_create_user)

    p = sub.add_parser("reset-password", help="reset a user's password")
    p.add_argument("username")
    p.add_argument("--password", help="set password (default: generated)")
    p.set_defaults(func=_cmd_reset_password)

    sub.add_parser("list-users", help="list all users").set_defaults(func=_cmd_list_users)

    p = sub.add_parser("disable-user", help="disable a user account")
    p.add_argument("username")
    p.set_defaults(func=_cmd_disable_user, enable=False)

    p = sub.add_parser("enable-user", help="re-enable a user account")
    p.add_argument("username")
    p.set_defaults(func=_cmd_disable_user, enable=True)

    p = sub.add_parser("grant", help="grant a datasource to a user")
    p.add_argument("username")
    p.add_argument("datasource")
    p.set_defaults(func=_cmd_grant, revoke=False)

    p = sub.add_parser("revoke", help="revoke a datasource from a user")
    p.add_argument("username")
    p.add_argument("datasource")
    p.set_defaults(func=_cmd_grant, revoke=True)

    args = parser.parse_args(argv)
    await args.func(args)


def run_admin_cmds(argv: list[str]) -> None:
    """Entry point from trove.main (asyncio wrapper)."""
    asyncio.run(run(argv))
