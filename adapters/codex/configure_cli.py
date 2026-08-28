"""Command-line wrapper for the transactional Codex config backend."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("install", "uninstall"):
        sub = subparsers.add_parser(action)
        sub.add_argument("--config", type=Path, required=True)
        sub.add_argument("--backup", type=Path, required=True)
        sub.add_argument("--manifest", type=Path, required=True)
    install = subparsers.choices["install"]
    install.add_argument("--command", type=Path, required=True)
    install.add_argument("--force-replace", action="store_true")
    uninstall = subparsers.choices["uninstall"]
    uninstall.add_argument("--force", action="store_true")
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--manifest", type=Path, required=True)
    rollback.add_argument("--force", action="store_true")
    return parser


def _mutation(backend, args):
    if args.action == "install":
        return lambda document: backend.configure_install(
            document, args.command.resolve(), args.force_replace
        )
    return lambda document: (
        backend.configure_uninstall(document, args.force),
        [],
    )[1]


def run(backend, argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "rollback":
            backend.rollback_transaction(args.manifest, args.force)
            result: dict[str, object] = {"rolled_back": True}
        else:
            result = backend.begin_transaction(
                args.config,
                args.backup,
                args.manifest,
                _mutation(backend, args),
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except backend.ConfigTransactionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
