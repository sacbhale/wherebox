"""Operator commands: ``python -m app.cli <command>``.

Subcommands are registered in ``build_parser``; each handler takes the parsed args and
the Settings and returns an exit code.
"""

import argparse
import getpass
import json
import sys
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from app.auth import passwords
from app.config import Settings
from app.db import backup
from app.db.connection import connect
from app.db.migrate import migrate
from app.db.timestamps import utcnow
from app.inventory.export import build_export


def _read_password() -> str:
    if sys.stdin.isatty():
        password = getpass.getpass("New household password: ")
        if password != getpass.getpass("Repeat password: "):
            raise SystemExit("error: passwords do not match")
        return password
    return sys.stdin.readline().rstrip("\r\n")


def cmd_set_password(args: argparse.Namespace, settings: Settings) -> int:
    password = _read_password()
    if not password:
        print("error: password must not be empty", file=sys.stderr)
        return 1
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with closing(connect(settings.db_path)) as conn:
        migrate(conn)
        passwords.set_password(conn, password)
    print("Password updated; all sessions were signed out.")
    if settings.household_password is not None:
        print(
            "warning: HOUSEHOLD_PASSWORD is set and will replace this password on the next "
            "restart.",
            file=sys.stderr,
        )
    return 0


def cmd_set_username(args: argparse.Namespace, settings: Settings) -> int:
    if args.name is not None:
        name = args.name
    elif sys.stdin.isatty():
        name = input("Household login name: ")
    else:
        name = sys.stdin.readline().rstrip("\r\n")
    try:
        name = passwords.validate_username(name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with closing(connect(settings.db_path)) as conn:
        migrate(conn)
        passwords.set_username(conn, name)
    print(f"Login name set to {name!r}; all sessions were signed out.")
    if settings.household_username is not None:
        print(
            "warning: HOUSEHOLD_USERNAME is set and will replace this login name on the next "
            "restart.",
            file=sys.stderr,
        )
    return 0


def _require_db(settings: Settings) -> bool:
    if settings.db_path.exists():
        return True
    print(f"error: no database at {settings.db_path}", file=sys.stderr)
    return False


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    if not _require_db(settings):
        return 1
    with closing(connect(settings.db_path)) as conn:
        document = build_export(conn)
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.write_text(text, encoding="utf-8")
        print(f"Export written to {args.out}", file=sys.stderr)
    return 0


def cmd_backup(args: argparse.Namespace, settings: Settings) -> int:
    if not _require_db(settings):
        return 1
    with closing(connect(settings.db_path)) as conn:
        path = backup.write_backup(conn, settings, utcnow())
    print(f"Backup written to {path}")
    return 0


def cmd_check(args: argparse.Namespace, settings: Settings) -> int:
    errors = backup.integrity_errors(settings.db_path)
    if not errors:
        print(f"ok: {settings.db_path} passed PRAGMA integrity_check")
        return 0
    print(f"error: {settings.db_path} failed PRAGMA integrity_check:", file=sys.stderr)
    for line in errors[:20]:
        print(f"  {line}", file=sys.stderr)
    latest = backup.latest_path(settings)
    backup_errors = backup.integrity_errors(latest)
    if backup_errors:
        print(
            f"error: no usable backup at {latest} ({backup_errors[0]}); nothing to restore.",
            file=sys.stderr,
        )
        return 1
    if not args.repair:
        print(
            f"A good backup exists at {latest}. Stop the app, then run `check --repair` to "
            "replace the database with it (changes since that backup are lost).",
            file=sys.stderr,
        )
        return 1
    kept = backup.restore_latest(settings, utcnow())
    remaining = backup.integrity_errors(settings.db_path)
    if remaining:
        print(f"error: the restored database still fails: {remaining[0]}", file=sys.stderr)
        return 1
    print(
        f"Restored {settings.db_path} from {latest} (removed -wal/-shm first). "
        f"The damaged database was kept as {kept}. Changes since the backup are lost."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    set_password = commands.add_parser(
        "set-password",
        help="set the household password (read from the prompt, or one line of stdin)",
    )
    set_password.set_defaults(handler=cmd_set_password)
    set_username = commands.add_parser(
        "set-username",
        help="set the household login name (--name, the prompt, or one line of stdin)",
    )
    set_username.add_argument("--name", help="the login name (otherwise prompted)")
    set_username.set_defaults(handler=cmd_set_username)
    export = commands.add_parser(
        "export", help="print the JSON export (same as GET /api/export.json)"
    )
    export.add_argument("--out", type=Path, help="write to this file instead of stdout")
    export.set_defaults(handler=cmd_export)
    backup_cmd = commands.add_parser("backup", help="write backups/latest.db now")
    backup_cmd.set_defaults(handler=cmd_backup)
    check = commands.add_parser(
        "check",
        help="PRAGMA integrity_check on inventory.db; exits non-zero when it fails",
    )
    check.add_argument(
        "--repair",
        action="store_true",
        help="if the check fails, replace inventory.db with backups/latest.db "
        "(stop the app first; the damaged file is kept)",
    )
    check.set_defaults(handler=cmd_check)
    return parser


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args, settings or Settings())


if __name__ == "__main__":
    raise SystemExit(main())
