"""Administrative CLI.

Usage inside the container:
    python -m app.cli create-owner
    python -m app.cli check-config
"""

from __future__ import annotations

import asyncio
import getpass

import typer
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import session_scope
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.auth import MIN_PASSWORD_LENGTH
from app.services.auth_service import AuthService

cli = typer.Typer(help="Administrative commands.")


@cli.command("create-owner")
def create_owner(
    email: str = typer.Option(..., prompt=True),
    display_name: str = typer.Option(..., prompt=True),
    timezone: str = typer.Option("UTC", prompt="IANA timezone (e.g. Africa/Lagos)"),
) -> None:
    """Create the single owner account."""
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        typer.secho("Passwords do not match.", fg=typer.colors.RED)
        raise typer.Exit(1)
    if len(password) < MIN_PASSWORD_LENGTH:
        typer.secho(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", fg=typer.colors.RED
        )
        raise typer.Exit(1)

    async def _run() -> None:
        async with session_scope() as db:
            count = await db.scalar(select(func.count()).select_from(User))
            if count:
                typer.secho(
                    "A user already exists. This deployment is single-tenant.",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)
            user = await AuthService(db).register(
                email=email,
                password=password,
                display_name=display_name,
                timezone=timezone,
                role=UserRole.OWNER,
            )
            typer.secho(f"Created owner {user.email}", fg=typer.colors.GREEN)

    asyncio.run(_run())


@cli.command("check-config")
def check_config() -> None:
    """Validate configuration without starting the server."""
    s = get_settings()
    typer.echo(f"environment           : {s.environment}")
    typer.echo(f"database host         : {s.postgres_host}:{s.postgres_port}/{s.postgres_db}")
    typer.echo(f"redis                 : {s.redis_url}")
    typer.echo(f"frontend origin       : {s.frontend_origin}")
    typer.echo(f"X billing mode        : {s.x_billing_mode}")
    typer.echo(f"X monthly budget      : ${s.x_monthly_budget_usd:.2f}")
    typer.echo(f"X write actions       : {s.x_enable_write_actions}")
    typer.echo(f"X credentials present : {bool(s.x_client_id and s.x_client_secret)}")
    typer.echo(f"Anthropic key present : {bool(s.anthropic_api_key)}")
    typer.secho("Configuration is valid.", fg=typer.colors.GREEN)

    if s.x_enable_write_actions:
        typer.secho(
            "\nWARNING: X_ENABLE_WRITE_ACTIONS is true. The agent may request "
            "tweet.write and draft posts for your approval. No post is ever "
            "published without explicit per-draft approval.",
            fg=typer.colors.YELLOW,
        )


if __name__ == "__main__":
    cli()
