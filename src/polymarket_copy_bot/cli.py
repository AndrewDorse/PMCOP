from __future__ import annotations

import argparse

from rich.console import Console

from .config import Settings
from .engine import CopyTradingEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket copy trading bot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List watched wallets")

    add_wallet = subparsers.add_parser("add-wallet", help="Add wallet to watch list")
    add_wallet.add_argument("wallet")

    remove_wallet = subparsers.add_parser("remove-wallet", help="Remove wallet from watch list")
    remove_wallet.add_argument("wallet")

    subparsers.add_parser("sync-once", help="Build and execute one sync cycle")
    subparsers.add_parser("run", help="Run sync loop")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    console = Console()
    engine = CopyTradingEngine(settings, console)

    if args.command == "list":
        wallets = engine.load_watch_wallets()
        if not wallets:
            console.print("[yellow]No watched wallets configured.[/yellow]")
            return
        for wallet in wallets:
            console.print(wallet)
        return

    if args.command == "add-wallet":
        engine.add_wallet(args.wallet)
        console.print(f"Added {args.wallet.lower()}")
        return

    if args.command == "remove-wallet":
        existing = engine.load_watch_wallets()
        wallet = args.wallet.lower()
        if wallet not in existing:
            console.print(f"[yellow]Wallet not found:[/yellow] {wallet}")
            return
        engine.remove_wallet(wallet)
        console.print(f"Removed {wallet}")
        return

    if args.command == "sync-once":
        wallets = engine.load_watch_wallets()
        if not wallets:
            raise RuntimeError("Watch list is empty. Add at least one wallet first.")
        plan = engine.build_sync_plan(wallets)
        engine.print_plan(plan)
        results = engine.execute_plan(plan)
        for r in results:
            style = "green" if r.ok else "red"
            console.print(f"[{style}]{r.message}[/{style}]")
        return

    if args.command == "run":
        engine.loop()
        return


if __name__ == "__main__":
    main()
