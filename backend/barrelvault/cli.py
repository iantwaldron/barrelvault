"""The setup command."""

import argparse
import getpass
import sys

from barrelvault.account import create_account, is_set_up
from barrelvault.auth import ServiceError
from barrelvault.db import apply_migrations, open_session


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m barrelvault.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup")
    parser.parse_args(argv)
    setup()


def setup() -> None:
    apply_migrations()
    with open_session() as session:
        exists = is_set_up(session)
    if exists:
        _exit("Barrelvault is already set up.")
    name = input("Name: ")
    email = input("Email: ")
    password = getpass.getpass("Password: ")
    confirmed = getpass.getpass("Password (again): ")
    if password != confirmed:
        _exit("Passwords do not match.")
    try:
        with open_session(immediate=True) as session:
            create_account(session, name, email, password)
    except ServiceError:
        _exit("Setup was refused.")
    print("Account created.")


def _exit(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
