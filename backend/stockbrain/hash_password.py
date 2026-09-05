"""Generate the value for ``WEB_OWNER_PASSWORD_HASH``.

    python -m stockbrain.hash_password

Reads the password from a hidden prompt, twice, and prints the hash.  The
password is never taken from a command-line argument and never from an
environment variable, because both are visible to ``ps`` and both end up in a
shell history file.  Nothing is written to disk: the operator copies one line
into ``.env``.

Run inside the container -- ``docker compose exec stockbrain python -m
stockbrain.hash_password`` -- so the hash is produced by the same scrypt
parameters that will verify it.
"""

from __future__ import annotations

import getpass
import sys

from stockbrain.api.auth import hash_password

_MIN_LENGTH = 12


def main() -> int:
    """Prompt twice, print the hash, or explain the refusal.

    Exit 2 rather than raising, so a mistyped confirmation is a message and not
    a traceback.
    """
    if sys.stdin.isatty():
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm:  ")
    else:
        # Piped input, for scripted first-time setup. One line, no confirmation
        # to compare it against, so there is nothing to check it against either.
        password = sys.stdin.readline().rstrip("\n")
        confirm = password

    if password != confirm:
        print("The two entries did not match.", file=sys.stderr)  # noqa: T201
        return 2
    if len(password) < _MIN_LENGTH:
        # A length floor and nothing else. Composition rules push people towards
        # "Password1!" and this account's only real defence is that the hash is
        # memory-hard and the listener is not on the public internet.
        print(  # noqa: T201
            f"Use at least {_MIN_LENGTH} characters. This password is the only thing "
            "between the network and a broker order.",
            file=sys.stderr,
        )
        return 2

    print("")  # noqa: T201
    print("Add this line to .env (the value is a hash, not the password):")  # noqa: T201
    print("")  # noqa: T201
    print(f"WEB_OWNER_PASSWORD_HASH={hash_password(password)}")  # noqa: T201
    print("")  # noqa: T201
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
