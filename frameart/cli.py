"""FrameArt CLI — command-line interface.

Usage:
    frameart generate --prompt "..."
    frameart apply --image path/to/image.png --tv livingroom_frame
    frameart generate-and-apply --prompt "..." --tv livingroom_frame
    frameart tv status --tv livingroom_frame
    frameart tv pair --tv-ip 192.168.1.100
    frameart list
    frameart cleanup --older-than 30

Debugging:
    frameart --debug generate-and-apply --prompt "..." --tv-ip 1.2.3.4
    frameart generate-and-apply --debug --prompt "..." --tv-ip 1.2.3.4
"""

from __future__ import annotations

import logging
import sys

import click

from frameart import __version__
from frameart.artifacts import setup_logging
from frameart.config import STYLE_PRESETS, load_settings

# Shared options for --debug / --verbose that can appear on any subcommand.
_debug_option = click.option(
    "--debug", is_flag=True, default=False, expose_value=False, is_eager=True,
    callback=lambda ctx, _param, val: ctx.ensure_object(dict).update({"_debug": val}) or val,
    help="Enable debug logging (includes samsungtvws wire protocol).",
)
_verbose_option = click.option(
    "--verbose", "-v", is_flag=True, default=False, expose_value=False, is_eager=True,
    callback=lambda ctx, _param, val: ctx.ensure_object(dict).update({"_verbose": val}) or val,
    help="Enable verbose logging.",
)


def _ensure_logging(ctx: click.Context) -> None:
    """Idempotently set up logging + settings (handles --debug on subcommand)."""
    obj = ctx.ensure_object(dict)
    if "settings" in obj:
        # Already initialised by the group callback — but re-check if a
        # subcommand-level --debug upgraded us from WARNING → DEBUG.
        if obj.get("_debug") and obj.get("_log_level") != "DEBUG":
            obj["_log_level"] = "DEBUG"
            settings = obj["settings"]
            # Re-configure logging at DEBUG
            root = logging.getLogger("frameart")
            root.handlers.clear()
            setup_logging(settings.data_dir, level="DEBUG", log_file=settings.log_file)
        return

    debug = obj.get("_debug", False)
    verbose = obj.get("_verbose", False)
    log_level = "DEBUG" if debug else ("INFO" if verbose else "WARNING")
    obj["_log_level"] = log_level

    overrides = {}
    data_dir = obj.get("_data_dir")
    if data_dir:
        overrides["data_dir"] = data_dir

    settings = load_settings(log_level=log_level, **overrides)
    setup_logging(settings.data_dir, level=log_level, log_file=settings.log_file)
    obj["settings"] = settings


def _print_result(result) -> None:
    """Print a pipeline result summary to the console."""
    if result.error:
        click.secho(f"ERROR: {result.error}", fg="red", err=True)
        return

    click.secho(f"Job ID: {result.job_id}", fg="green")

    if result.source_path:
        click.echo(f"  Source: {result.source_path}")
    if result.final_path:
        click.echo(f"  Final:  {result.final_path} (3840x2160)")
    if result.content_id:
        click.echo(f"  TV content ID: {result.content_id}")
    if result.tv_switched:
        click.secho("  Display switched to new artwork.", fg="green")

    if result.timings:
        parts = []
        for key, val in result.timings.items():
            parts.append(f"{key}={val:.0f}ms")
        click.echo(f"  Timings: {', '.join(parts)}")


# --- Top-level group ---------------------------------------------------------


@click.group()
@click.version_option(__version__, prog_name="frameart")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging.")
@click.option("--debug", is_flag=True, help="Enable debug logging.")
@click.option("--data-dir", type=click.Path(), default=None, help="Data directory override.")
@click.pass_context
def main(ctx, verbose, debug, data_dir):
    """FrameArt — generate AI artwork and display it on Samsung Frame TVs."""
    obj = ctx.ensure_object(dict)

    # Store for _ensure_logging (may be called again from subcommands)
    obj["_debug"] = obj.get("_debug") or debug
    obj["_verbose"] = obj.get("_verbose") or verbose
    if data_dir:
        obj["_data_dir"] = data_dir

    _ensure_logging(ctx)


# --- generate ----------------------------------------------------------------


@main.command()
@_debug_option
@_verbose_option
@click.option("--prompt", "-p", required=True, help="Text description of the image.")
@click.option(
    "--style", "-s",
    type=click.Choice(list(STYLE_PRESETS.keys()) + ["custom"], case_sensitive=False),
    default=None,
    help="Style preset.",
)
@click.option("--provider", type=str, default=None, help="Image provider (openai, ollama, etc.).")
@click.option("--model", type=str, default=None, help="Provider-specific model ID.")
@click.option("--upscaler", type=str, default=None, help="Upscaler to use.")
@click.option("--negative-prompt", type=str, default=None, help="Negative prompt (if supported).")
@click.option("--seed", type=int, default=None, help="Deterministic seed (if supported).")
@click.option("--steps", type=int, default=None, help="Diffusion steps (if supported).")
@click.option("--guidance", type=float, default=None, help="Guidance scale (if supported).")
@click.pass_context
def generate(ctx, prompt, style, provider, model, upscaler, negative_prompt, seed, steps, guidance):
    """Generate an image from a text prompt (no TV upload)."""
    _ensure_logging(ctx)
    from frameart.pipeline import run_generate

    settings = ctx.obj["settings"]
    result = run_generate(
        settings,
        prompt,
        style=style,
        provider_name=provider,
        model=model,
        upscaler_name=upscaler,
        negative_prompt=negative_prompt,
        seed=seed,
        steps=steps,
        guidance=guidance,
    )
    _print_result(result)
    sys.exit(1 if result.error else 0)


# --- apply -------------------------------------------------------------------


@main.command()
@_debug_option
@_verbose_option
@click.option("--image", "-i", required=True, type=click.Path(exists=True), help="Image to upload.")
@click.option("--tv", type=str, default=None, help="TV profile name from config.")
@click.option("--tv-ip", type=str, default=None, help="TV IP address.")
@click.option("--matte", type=str, default="none", help="Matte style (e.g., modern_black).")
@click.pass_context
def apply(ctx, image, tv, tv_ip, matte):
    """Upload an existing image to the Frame TV and switch to it."""
    _ensure_logging(ctx)
    from frameart.pipeline import run_apply

    settings = ctx.obj["settings"]
    result = run_apply(settings, image, tv_name=tv, tv_ip=tv_ip, matte=matte)
    _print_result(result)
    sys.exit(1 if result.error else 0)


# --- generate-and-apply ------------------------------------------------------


@main.command("generate-and-apply")
@_debug_option
@_verbose_option
@click.option("--prompt", "-p", required=True, help="Text description of the image.")
@click.option(
    "--style", "-s",
    type=click.Choice(list(STYLE_PRESETS.keys()) + ["custom"], case_sensitive=False),
    default=None,
    help="Style preset.",
)
@click.option("--provider", type=str, default=None, help="Image provider.")
@click.option("--model", type=str, default=None, help="Provider-specific model ID.")
@click.option("--upscaler", type=str, default=None, help="Upscaler.")
@click.option("--negative-prompt", type=str, default=None, help="Negative prompt.")
@click.option("--seed", type=int, default=None, help="Deterministic seed.")
@click.option("--steps", type=int, default=None, help="Diffusion steps.")
@click.option("--guidance", type=float, default=None, help="Guidance scale.")
@click.option("--tv", type=str, default=None, help="TV profile name from config.")
@click.option("--tv-ip", type=str, default=None, help="TV IP address.")
@click.option("--matte", type=str, default="none", help="Matte style.")
@click.option("--no-upload", is_flag=True, help="Generate + postprocess but skip upload.")
@click.option("--no-switch", is_flag=True, help="Upload but don't switch displayed art.")
@click.option("--dry-run", is_flag=True, help="Alias for --no-upload.")
@click.pass_context
def generate_and_apply(
    ctx, prompt, style, provider, model, upscaler, negative_prompt,
    seed, steps, guidance, tv, tv_ip, matte, no_upload, no_switch, dry_run,
):
    """Generate an image and display it on the Frame TV."""
    _ensure_logging(ctx)
    from frameart.pipeline import run_generate_and_apply

    settings = ctx.obj["settings"]
    result = run_generate_and_apply(
        settings,
        prompt,
        style=style,
        provider_name=provider,
        model=model,
        upscaler_name=upscaler,
        negative_prompt=negative_prompt,
        seed=seed,
        steps=steps,
        guidance=guidance,
        tv_name=tv,
        tv_ip=tv_ip,
        matte=matte,
        no_upload=no_upload or dry_run,
        no_switch=no_switch,
    )
    _print_result(result)
    sys.exit(1 if result.error else 0)


# --- tv subgroup -------------------------------------------------------------


@main.group()
def tv():
    """Samsung Frame TV management commands."""


@tv.command("status")
@_debug_option
@_verbose_option
@click.option("--tv", "tv_name", type=str, default=None, help="TV profile name.")
@click.option("--tv-ip", type=str, default=None, help="TV IP address.")
@click.pass_context
def tv_status(ctx, tv_name, tv_ip):
    """Check the status of a Frame TV."""
    _ensure_logging(ctx)
    from frameart.config import TVProfile
    from frameart.tv.controller import _connect, _run_with_timeout

    settings = ctx.obj["settings"]

    profile = None
    if tv_name and tv_name in settings.tvs:
        profile = settings.tvs[tv_name]
    elif tv_ip:
        profile = TVProfile(ip=tv_ip)
    elif len(settings.tvs) == 1:
        profile = next(iter(settings.tvs.values()))

    if profile is None:
        click.secho("No TV specified. Use --tv or --tv-ip.", fg="red", err=True)
        sys.exit(1)

    click.echo(f"Checking TV at {profile.ip}:{profile.port}...")

    # Step 1: REST reachability
    click.echo("  [1/4] REST device info...", nl=False)
    try:
        tv = _connect(profile)
        device_info = tv.rest_device_info()
        device = device_info.get("device", {})
        click.secho(" OK", fg="green")
        click.echo(f"         Model: {device.get('modelName', '?')}")
        click.echo(f"         Name:  {device.get('name', '?')}")
    except Exception as e:
        click.secho(f" FAILED: {e}", fg="red")
        sys.exit(1)

    # Step 2: FrameTVSupport
    click.echo("  [2/4] Frame TV support...", nl=False)
    is_support_str = device_info.get("isSupport", "{}")
    frame_supported = (
        device.get("FrameTVSupport") == "true"
        or '"FrameTVSupport":"true"' in is_support_str
    )
    if frame_supported:
        click.secho(" Yes", fg="green")
    else:
        click.secho(" No", fg="yellow")
        return

    # Step 3: Art mode status (websocket — may time out)
    click.echo("  [3/4] Art mode status...", nl=False)
    result, err = _run_with_timeout(lambda: tv.art().get_artmode())
    if err:
        click.secho(f" unavailable ({err})", fg="yellow")
    else:
        state = "ON" if result else "OFF"
        color = "green" if result else "yellow"
        click.secho(f" {state}", fg=color)

    # Step 4: Current artwork (websocket — may time out)
    click.echo("  [4/4] Current artwork...", nl=False)
    result, err = _run_with_timeout(lambda: tv.art().get_current())
    if err:
        click.secho(f" unavailable ({err})", fg="yellow")
    else:
        if isinstance(result, dict):
            click.secho(f" {result.get('content_id', '?')}", fg="green")
        elif isinstance(result, str):
            click.secho(f" {result}", fg="green")
        else:
            click.secho(f" {result}", fg="green")


@tv.command("pair")
@_debug_option
@_verbose_option
@click.option("--tv-ip", required=True, help="TV IP address.")
@click.option("--port", type=int, default=8002, help="TV port (default: 8002).")
@click.option("--name", type=str, default="FrameArt", help="Name shown on TV during pairing.")
@click.pass_context
def tv_pair(ctx, tv_ip, port, name):
    """Pair with a Samsung Frame TV.

    The TV will display an "Allow" prompt — accept it on the TV.
    """
    _ensure_logging(ctx)
    from frameart.config import TVProfile
    from frameart.tv.controller import pair

    settings = ctx.obj["settings"]

    # Determine token file path
    secrets_dir = settings.data_dir / "secrets"
    token_file = str(secrets_dir / f"{tv_ip.replace('.', '_')}.token")

    profile = TVProfile(ip=tv_ip, port=port, name=name, token_file=token_file)

    click.echo(f"Pairing with TV at {tv_ip}:{port}...")
    click.echo("Please accept the connection prompt on your TV.")

    try:
        pair(profile)
        click.secho("Pairing successful!", fg="green")
        click.echo(f"Token saved to: {token_file}")
        click.echo(
            f"\nAdd this to your config.yaml:\n"
            f"  tvs:\n"
            f"    my_frame:\n"
            f"      ip: \"{tv_ip}\"\n"
            f"      port: {port}\n"
            f"      token_file: \"{token_file}\"\n"
        )
    except Exception as e:
        click.secho(f"Pairing failed: {e}", fg="red", err=True)
        sys.exit(1)


@tv.command("list-art")
@_debug_option
@_verbose_option
@click.option("--tv", "tv_name", type=str, default=None, help="TV profile name.")
@click.option("--tv-ip", type=str, default=None, help="TV IP address.")
@click.pass_context
def tv_list_art(ctx, tv_name, tv_ip):
    """List artworks on the Frame TV."""
    _ensure_logging(ctx)
    from frameart.config import TVProfile
    from frameart.tv.controller import list_art

    settings = ctx.obj["settings"]

    profile = None
    if tv_name and tv_name in settings.tvs:
        profile = settings.tvs[tv_name]
    elif tv_ip:
        profile = TVProfile(ip=tv_ip)
    elif len(settings.tvs) == 1:
        profile = next(iter(settings.tvs.values()))

    if profile is None:
        click.secho("No TV specified.", fg="red", err=True)
        sys.exit(1)

    try:
        artworks = list_art(profile)
        click.echo(f"Found {len(artworks)} artwork(s):")
        for art in artworks:
            cid = art.get("content_id", "unknown")
            click.echo(f"  {cid}")
    except Exception as e:
        click.secho(f"Failed to list art: {e}", fg="red", err=True)
        sys.exit(1)


# --- list (artifacts) --------------------------------------------------------


@main.command("list")
@_debug_option
@_verbose_option
@click.option("--limit", type=int, default=20, help="Max number of jobs to show.")
@click.pass_context
def list_jobs(ctx, limit):
    """List recent generated artifacts."""
    _ensure_logging(ctx)

    settings = ctx.obj["settings"]
    artifacts_dir = settings.data_dir / "artifacts"

    if not artifacts_dir.exists():
        click.echo("No artifacts found.")
        return

    # Walk the date-structured directories and find meta.json files
    meta_files = sorted(artifacts_dir.rglob("meta.json"), reverse=True)[:limit]

    if not meta_files:
        click.echo("No artifacts found.")
        return

    import json

    click.echo(f"Recent jobs (showing up to {limit}):")
    for meta_path in meta_files:
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            job_id = meta.get("job_id", meta_path.parent.name)
            prompt = meta.get("prompt_original", "")[:60]
            provider = meta.get("provider", "?")
            content_id = meta.get("content_id", "")
            click.echo(f"  {job_id}  [{provider}]  {prompt}")
            if content_id:
                click.echo(f"    TV content_id: {content_id}")
        except Exception:
            click.echo(f"  {meta_path.parent.name}  (metadata unreadable)")


# --- cleanup -----------------------------------------------------------------


@main.command()
@_debug_option
@_verbose_option
@click.option("--older-than", type=int, default=30, help="Delete artifacts older than N days.")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted without deleting.")
@click.pass_context
def cleanup(ctx, older_than, dry_run):
    """Remove old generated artifacts."""
    _ensure_logging(ctx)
    import shutil
    from datetime import datetime, timedelta, timezone

    settings = ctx.obj["settings"]
    artifacts_dir = settings.data_dir / "artifacts"

    if not artifacts_dir.exists():
        click.echo("No artifacts to clean up.")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than)
    removed = 0

    for year_dir in sorted(artifacts_dir.iterdir()):
        if not year_dir.is_dir():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir():
                continue
            for day_dir in sorted(month_dir.iterdir()):
                if not day_dir.is_dir():
                    continue
                try:
                    dir_date = datetime.strptime(
                        f"{year_dir.name}/{month_dir.name}/{day_dir.name}",
                        "%Y/%m/%d",
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue

                if dir_date < cutoff:
                    for job_dir in day_dir.iterdir():
                        if job_dir.is_dir():
                            if dry_run:
                                click.echo(f"  Would delete: {job_dir}")
                            else:
                                shutil.rmtree(job_dir)
                            removed += 1

    action = "Would remove" if dry_run else "Removed"
    click.echo(f"{action} {removed} job(s) older than {older_than} days.")


if __name__ == "__main__":
    main()
