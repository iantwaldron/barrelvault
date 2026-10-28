import subprocess
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import select

from barrelvault import auth
from barrelvault.cli import main
from barrelvault.db import apply_migrations, open_session
from barrelvault.models import Human, User


def test_setup_creates_the_account(database, monkeypatch, capsys):
    assert not database.exists()
    prompts = _prompt(monkeypatch, "Ada", "ada@example.com", "password-1", "password-1")
    main(["setup"])
    assert database.is_file()
    assert prompts == ["Name: ", "Email: ", "Password: ", "Password (again): "]
    with open_session() as session:
        human = session.exec(select(Human)).one()
        user = session.get(User, human.user_id)
        assert user is not None
        assert user.name == "Ada"
        assert human.email == "ada@example.com"
        assert human.password_hash != "password-1"
        assert human.password_hash.startswith("$argon2")
        assert auth._passwords.verify("password-1", human.password_hash)
        names = session.connection().execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
        assert {"user", "human", "agent", "token"} <= {row[0] for row in names}
    assert b"password-1" not in database.read_bytes()
    assert capsys.readouterr().out == "Account created.\n"


def test_setup_exits_when_an_account_exists(database, monkeypatch, capsys):
    _prompt(monkeypatch, "Ada", "ada@example.com", "password-1", "password-1")
    main(["setup"])
    prompts = _reject_prompts(monkeypatch)
    with pytest.raises(SystemExit) as raised:
        main(["setup"])
    assert raised.value.code == 1
    assert prompts == []
    assert capsys.readouterr().err == "Barrelvault is already set up.\n"
    with open_session() as session:
        assert len(session.exec(select(Human)).all()) == 1


def test_an_existing_user_exits_before_the_prompts(database, monkeypatch, capsys):
    apply_migrations()
    with open_session() as session:
        session.add(User(name="Bot"))
    prompts = _reject_prompts(monkeypatch)
    with pytest.raises(SystemExit) as raised:
        main(["setup"])
    assert raised.value.code == 1
    assert prompts == []
    assert capsys.readouterr().err == "Barrelvault is already set up.\n"
    with open_session() as session:
        assert session.exec(select(Human)).first() is None


def test_mismatched_passwords_leave_no_account(database, monkeypatch, capsys):
    _prompt(monkeypatch, "Ada", "ada@example.com", "password-1", "password-2")
    with pytest.raises(SystemExit) as raised:
        main(["setup"])
    assert raised.value.code == 1
    assert capsys.readouterr().err == "Passwords do not match.\n"
    with open_session() as session:
        assert session.exec(select(Human)).first() is None


def test_a_blank_password_is_refused(database, monkeypatch, capsys):
    _prompt(monkeypatch, "Ada", "ada@example.com", " ", " ")
    with pytest.raises(SystemExit) as raised:
        main(["setup"])
    assert raised.value.code == 1
    assert capsys.readouterr().err == "Setup was refused: password is required.\n"
    with open_session() as session:
        assert session.exec(select(User)).first() is None


def test_overlapping_setup_runs_create_one_human(database, monkeypatch, capsys):
    # Alembic keeps its context in a module global, so threads cannot migrate
    # at once. Real setup runs are separate processes; test_db covers those.
    apply_migrations()
    monkeypatch.setattr("barrelvault.cli.apply_migrations", lambda: None)
    both_prompted = threading.Barrier(2)
    answers = {"Name: ": "Ada", "Email: ": "ada@example.com"}
    monkeypatch.setattr("builtins.input", lambda prompt: answers[prompt])

    def read_password(prompt: str) -> str:
        if prompt == "Password (again): ":
            both_prompted.wait(timeout=5)
        return "password-1"

    monkeypatch.setattr("barrelvault.cli.getpass.getpass", read_password)
    outcomes: list[object] = []

    def run() -> None:
        try:
            main(["setup"])
            outcomes.append("created")
        except BaseException as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    exits = [outcome for outcome in outcomes if isinstance(outcome, SystemExit)]
    assert outcomes.count("created") == 1
    assert len(exits) == 1
    assert exits[0].code == 1
    assert "Setup was refused: setup has already run.\n" in capsys.readouterr().err
    with open_session() as session:
        assert len(session.exec(select(User)).all()) == 1


def test_the_password_is_not_read_from_the_environment(database, monkeypatch):
    monkeypatch.setenv("BARRELVAULT_PASSWORD", "from-env")
    _prompt(monkeypatch, "Ada", "ada@example.com", "typed-password", "typed-password")
    main(["setup"])
    with open_session() as session:
        human = session.exec(select(Human)).one()
        password_hash = human.password_hash
    assert auth._passwords.verify("typed-password", password_hash)
    assert not auth._passwords.verify("from-env", password_hash)


def test_the_password_is_not_a_command_argument(database):
    with pytest.raises(SystemExit):
        main(["setup", "--password", "from-argv"])
    assert not database.exists()


def test_setup_is_the_command(database):
    with pytest.raises(SystemExit) as raised:
        main([])
    assert raised.value.code == 2
    completed = subprocess.run(
        [sys.executable, "-m", "barrelvault.cli"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "setup" in completed.stderr
    assert not database.exists()


def _prompt(monkeypatch, name: str, email: str, password: str, confirmed: str) -> list[str]:
    prompts: list[str] = []
    answers = {"Name: ": name, "Email: ": email}

    def read_input(prompt: str) -> str:
        prompts.append(prompt)
        return answers[prompt]

    def read_password(prompt: str) -> str:
        prompts.append(prompt)
        if prompt == "Password: ":
            return password
        if prompt == "Password (again): ":
            return confirmed
        raise AssertionError(prompt)

    monkeypatch.setattr("builtins.input", read_input)
    monkeypatch.setattr("barrelvault.cli.getpass.getpass", read_password)
    return prompts


def _reject_prompts(monkeypatch) -> list[str]:
    prompts: list[str] = []

    def rejected(prompt: str) -> str:
        prompts.append(prompt)
        raise AssertionError(prompt)

    monkeypatch.setattr("builtins.input", rejected)
    monkeypatch.setattr("barrelvault.cli.getpass.getpass", rejected)
    return prompts
