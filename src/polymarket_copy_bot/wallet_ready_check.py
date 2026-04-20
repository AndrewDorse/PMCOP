from __future__ import annotations

from py_clob_client.client import ClobClient
from rich.console import Console

from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    console = Console()

    console.print("[cyan]Checking Polymarket wallet/client readiness...[/cyan]")
    console.print(f"Host: {settings.pm_host}")
    console.print(f"Chain ID: {settings.pm_chain_id}")
    console.print(f"Signature type: {settings.pm_signature_type}")
    console.print(f"Funder: {settings.pm_funder}")
    console.print(f"DRY_RUN: {settings.dry_run}")

    try:
        read_client = ClobClient(settings.pm_host)
        ok = read_client.get_ok()
        server_time = read_client.get_server_time()
        console.print(f"[green]Read-only CLOB OK:[/green] {ok}")
        console.print(f"[green]Server time:[/green] {server_time}")
    except Exception as exc:
        console.print(f"[red]Read-only connectivity failed:[/red] {exc}")
        raise SystemExit(1)

    try:
        client = ClobClient(
            settings.pm_host,
            key=settings.pm_private_key,
            chain_id=settings.pm_chain_id,
            signature_type=settings.pm_signature_type,
            funder=settings.pm_funder,
        )
        api_creds = client.create_or_derive_api_creds()
        client.set_api_creds(api_creds)
        console.print("[green]API credentials derived and attached successfully.[/green]")
    except Exception as exc:
        console.print(f"[red]Auth setup failed:[/red] {exc}")
        raise SystemExit(2)

    try:
        trades = client.get_trades()
        console.print(f"[green]Authenticated trade history call succeeded.[/green] Trades returned: {len(trades)}")
    except Exception as exc:
        console.print(f"[red]Authenticated trade call failed:[/red] {exc}")
        raise SystemExit(3)

    console.print("[bold green]Wallet looks connected and ready for copy trading.[/bold green]")
    console.print("[yellow]This does not guarantee sufficient balance, allowances, or regional eligibility for every order.[/yellow]")


if __name__ == "__main__":
    main()
