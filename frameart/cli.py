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
        dims = result.metadata.get("final_width", ""), result.metadata.get("final_height", "")
        dim_str = f" ({dims[0]}x{dims[1]})" if all(dims) else ""
        click.echo(f"  Final:  {result.final_path}{dim_str}")
    if result.content_id:
        click.echo(f"  TV content ID: {result.content_id}")
    if result.tv_switched:
        click.secho("  Display switched to new artwork.", fg="green")
    if result.meural_displayed:
        click.secho("  Image displayed on Meural canvas.", fg="green")

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
@click.option(
    "--cleanup-keep", type=int, default=None,
    help="After upload, keep N user artworks on TV and delete the rest (default: disabled).",
)
@click.option(
    "--cleanup-order", type=click.Choice(["oldest_first", "newest_first"]),
    default="oldest_first", help="Which artworks to delete first during cleanup.",
)
@click.option(
    "--cleanup-include-favourites", is_flag=True,
    help="Also delete favourited artworks during cleanup.",
)
@click.pass_context
def apply(ctx, image, tv, tv_ip, matte, cleanup_keep, cleanup_order, cleanup_include_favourites):
    """Upload an existing image to the Frame TV and switch to it."""
    _ensure_logging(ctx)
    from frameart.pipeline import run_apply

    settings = ctx.obj["settings"]
    result = run_apply(
        settings, image, tv_name=tv, tv_ip=tv_ip, matte=matte,
        cleanup_keep=cleanup_keep,
        cleanup_order=cleanup_order,
        cleanup_include_favourites=cleanup_include_favourites,
    )
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
@click.option(
    "--cleanup-keep", type=int, default=None,
    help="After upload, keep N user artworks on TV and delete the rest (default: disabled).",
)
@click.option(
    "--cleanup-order", type=click.Choice(["oldest_first", "newest_first"]),
    default="oldest_first", help="Which artworks to delete first during cleanup.",
)
@click.option(
    "--cleanup-include-favourites", is_flag=True,
    help="Also delete favourited artworks during cleanup.",
)
@click.pass_context
def generate_and_apply(
    ctx, prompt, style, provider, model, upscaler, negative_prompt,
    seed, steps, guidance, tv, tv_ip, matte, no_upload, no_switch, dry_run,
    cleanup_keep, cleanup_order, cleanup_include_favourites,
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
        cleanup_keep=cleanup_keep,
        cleanup_order=cleanup_order,
        cleanup_include_favourites=cleanup_include_favourites,
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


@tv.command("discover")
@_debug_option
@_verbose_option
@click.option("--timeout", type=float, default=4.0, help="SSDP timeout in seconds.")
@click.option("--frame-only", is_flag=True, help="Only show Frame TVs.")
@click.pass_context
def tv_discover(ctx, timeout, frame_only):
    """Discover Samsung TVs on the local network via SSDP."""
    _ensure_logging(ctx)
    from frameart.tv.discovery import discover

    click.echo("Scanning for Samsung TVs on the network...")
    tvs = discover(timeout=timeout, frame_only=frame_only)

    if not tvs:
        click.secho("No Samsung TVs found.", fg="yellow")
        return

    click.echo(f"Found {len(tvs)} TV(s):\n")
    for dtv in tvs:
        frame_label = click.style(" [Frame TV]", fg="green") if dtv.frame_tv else ""
        click.echo(f"  {dtv.ip:<16} {dtv.model:<20} {dtv.name}{frame_label}")

    # Hint for pairing
    frame_tvs = [t for t in tvs if t.frame_tv]
    if frame_tvs:
        click.echo(f"\nTo pair: frameart tv pair --tv-ip {frame_tvs[0].ip}")


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


@tv.command("cleanup")
@_debug_option
@_verbose_option
@click.option("--tv", "tv_name", type=str, default=None, help="TV profile name.")
@click.option("--tv-ip", type=str, default=None, help="TV IP address.")
@click.option("--keep", type=int, default=20, help="Number of user artworks to keep (default: 20).")
@click.option("--delete-all", is_flag=True, help="Delete ALL user-uploaded artworks.")
@click.option(
    "--order", type=click.Choice(["oldest_first", "newest_first"]),
    default="oldest_first", help="Which artworks to delete first.",
)
@click.option(
    "--include-favourites", is_flag=True,
    help="Also delete favourited artworks (default: protect them).",
)
@click.option("--dry-run", is_flag=True, help="Show what would be deleted without deleting.")
@click.pass_context
def tv_cleanup(ctx, tv_name, tv_ip, keep, delete_all, order, include_favourites, dry_run):
    """Delete old user-uploaded artworks from the Frame TV.

    Only removes artworks uploaded by the user (content_id starting with MY_).
    Samsung Art Store items and built-in art are never touched.
    Favourited artworks are protected by default.
    """
    _ensure_logging(ctx)
    from frameart.config import TVProfile
    from frameart.tv.cleanup import cleanup_artworks

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

    if dry_run:
        # List what would be cleaned up without actually deleting
        from frameart.tv.cleanup import _is_favourite, _is_user_upload
        from frameart.tv.controller import list_art

        try:
            artworks = list_art(profile)
        except Exception as e:
            click.secho(f"Failed to list art: {e}", fg="red", err=True)
            sys.exit(1)

        user_art = [a for a in artworks if _is_user_upload(a)]
        favs = [a for a in user_art if _is_favourite(a)]
        if include_favourites:
            candidates = user_art
        else:
            candidates = [a for a in user_art if not _is_favourite(a)]
        candidates.sort(key=lambda a: a.get("content_id", ""))

        if delete_all:
            to_delete = candidates
        elif len(candidates) <= keep:
            to_delete = []
        elif order == "oldest_first":
            to_delete = candidates[: len(candidates) - keep]
        else:
            to_delete = candidates[keep:]

        click.echo(f"Total artworks on TV: {len(artworks)}")
        click.echo(f"User-uploaded: {len(user_art)}")
        click.echo(f"Favourites: {len(favs)} (protected: {not include_favourites})")
        click.echo(f"Would delete: {len(to_delete)}")
        for a in to_delete:
            fav = " [favourite]" if _is_favourite(a) else ""
            click.echo(f"  {a.get('content_id', '?')}{fav}")
        return

    result = cleanup_artworks(
        profile, keep=keep, delete_all=delete_all,
        order=order, include_favourites=include_favourites,
    )

    if result.error:
        click.secho(f"Cleanup error: {result.error}", fg="red", err=True)
        sys.exit(1)

    click.echo(f"Deleted {len(result.deleted)} artwork(s), kept {result.kept}.")
    if result.skipped_favourites:
        click.echo(f"Protected {result.skipped_favourites} favourite(s).")
    if result.deleted:
        for cid in result.deleted:
            click.echo(f"  Deleted: {cid}")


# --- meural subgroup ---------------------------------------------------------


@main.group()
def meural():
    """Netgear Meural canvas management commands."""


@meural.command("status")
@_debug_option
@_verbose_option
@click.option("--meural", "meural_name", type=str, default=None, help="Meural profile name.")
@click.option("--meural-ip", type=str, default=None, help="Meural IP address.")
@click.pass_context
def meural_status(ctx, meural_name, meural_ip):
    """Check the status of a Meural canvas."""
    _ensure_logging(ctx)
    from frameart.config import MeuralProfile
    from frameart.meural.controller import get_status

    settings = ctx.obj["settings"]
    profile = None
    if meural_name and meural_name in settings.meurals:
        profile = settings.meurals[meural_name]
    elif meural_ip:
        profile = MeuralProfile(ip=meural_ip)
    elif len(settings.meurals) == 1:
        profile = next(iter(settings.meurals.values()))

    if profile is None:
        click.secho(
            "No Meural specified. Use --meural or --meural-ip.", fg="red", err=True,
        )
        sys.exit(1)

    click.echo(f"Checking Meural at {profile.ip}...")
    status = get_status(profile)

    if not status.reachable:
        click.secho(f"  Not reachable: {status.error}", fg="red")
        sys.exit(1)

    click.secho("  Reachable: Yes", fg="green")
    if status.device_name:
        click.echo(f"  Name:  {status.device_name}")
    if status.device_model:
        click.echo(f"  Model: {status.device_model}")
    click.echo(f"  Orientation: {status.orientation or '?'}")
    sleep_state = "sleeping" if status.sleeping else "awake"
    color = "yellow" if status.sleeping else "green"
    click.secho(f"  State: {sleep_state}", fg=color)
    if status.current_gallery:
        click.echo(f"  Gallery: {status.current_gallery}")
    if status.current_item:
        click.echo(f"  Item:    {status.current_item}")


@meural.command("display")
@_debug_option
@_verbose_option
@click.option(
    "--image", "-i", required=True,
    type=click.Path(exists=True), help="Image to display.",
)
@click.option("--meural", "meural_name", type=str, default=None, help="Meural profile name.")
@click.option("--meural-ip", type=str, default=None, help="Meural IP address.")
@click.option(
    "--duration", type=int, default=0,
    help="Seconds to display before returning to playlist (0 = stay indefinitely).",
)
@click.pass_context
def meural_display(ctx, image, meural_name, meural_ip, duration):
    """Display an image on the Meural canvas via the local postcard API."""
    _ensure_logging(ctx)
    from frameart.pipeline import run_meural_apply

    settings = ctx.obj["settings"]
    result = run_meural_apply(
        settings, image,
        meural_name=meural_name, meural_ip=meural_ip,
        duration=duration,
    )
    _print_result(result)
    sys.exit(1 if result.error else 0)


@meural.command("generate-and-display")
@_debug_option
@_verbose_option
@click.option("--prompt", "-p", required=True, help="Text description of the image.")
@click.option(
    "--style", "-s",
    type=click.Choice(list(STYLE_PRESETS.keys()) + ["custom"], case_sensitive=False),
    default=None, help="Style preset.",
)
@click.option("--provider", type=str, default=None, help="Image provider.")
@click.option("--model", type=str, default=None, help="Provider-specific model ID.")
@click.option("--upscaler", type=str, default=None, help="Upscaler.")
@click.option("--negative-prompt", type=str, default=None, help="Negative prompt.")
@click.option("--seed", type=int, default=None, help="Deterministic seed.")
@click.option("--steps", type=int, default=None, help="Diffusion steps.")
@click.option("--guidance", type=float, default=None, help="Guidance scale.")
@click.option("--meural", "meural_name", type=str, default=None, help="Meural profile name.")
@click.option("--meural-ip", type=str, default=None, help="Meural IP address.")
@click.option(
    "--orientation", type=click.Choice(["vertical", "horizontal"]),
    default="vertical", help="Canvas orientation (default: vertical).",
)
@click.option(
    "--duration", type=int, default=0,
    help="Seconds to display before returning to playlist (0 = stay indefinitely).",
)
@click.option("--no-upload", is_flag=True, help="Generate only, skip display.")
@click.option("--dry-run", is_flag=True, help="Alias for --no-upload.")
@click.pass_context
def meural_generate_and_display(
    ctx, prompt, style, provider, model, upscaler, negative_prompt,
    seed, steps, guidance, meural_name, meural_ip, orientation,
    duration, no_upload, dry_run,
):
    """Generate an image and display it on the Meural canvas."""
    _ensure_logging(ctx)
    from frameart.pipeline import run_meural_generate_and_apply

    settings = ctx.obj["settings"]
    result = run_meural_generate_and_apply(
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
        meural_name=meural_name,
        meural_ip=meural_ip,
        orientation=orientation,
        duration=duration,
        no_upload=no_upload or dry_run,
    )
    _print_result(result)
    sys.exit(1 if result.error else 0)


@meural.command("orientation")
@_debug_option
@_verbose_option
@click.option("--meural", "meural_name", type=str, default=None, help="Meural profile name.")
@click.option("--meural-ip", type=str, default=None, help="Meural IP address.")
@click.argument("direction", type=click.Choice(["portrait", "landscape"]))
@click.pass_context
def meural_orientation(ctx, meural_name, meural_ip, direction):
    """Set canvas orientation to portrait or landscape."""
    _ensure_logging(ctx)
    from frameart.meural.controller import set_orientation

    settings = ctx.obj["settings"]
    profile = _resolve_meural(settings, meural_name, meural_ip)
    if set_orientation(profile, direction):
        click.secho(f"Orientation set to {direction}.", fg="green")
    else:
        click.secho("Failed to set orientation.", fg="red", err=True)
        sys.exit(1)


@meural.command("brightness")
@_debug_option
@_verbose_option
@click.option("--meural", "meural_name", type=str, default=None, help="Meural profile name.")
@click.option("--meural-ip", type=str, default=None, help="Meural IP address.")
@click.option("--reset", is_flag=True, help="Reset to auto (ambient light sensor).")
@click.argument("level", type=int, required=False)
@click.pass_context
def meural_brightness(ctx, meural_name, meural_ip, reset, level):
    """Set backlight brightness (0-100), or --reset for auto."""
    _ensure_logging(ctx)
    from frameart.meural.controller import reset_brightness, set_brightness

    settings = ctx.obj["settings"]
    profile = _resolve_meural(settings, meural_name, meural_ip)

    if reset:
        if reset_brightness(profile):
            click.secho("Brightness reset to auto.", fg="green")
        else:
            click.secho("Failed to reset brightness.", fg="red", err=True)
            sys.exit(1)
    elif level is not None:
        if set_brightness(profile, level):
            click.secho(f"Brightness set to {level}.", fg="green")
        else:
            click.secho("Failed to set brightness.", fg="red", err=True)
            sys.exit(1)
    else:
        click.secho("Provide a level (0-100) or --reset.", fg="red", err=True)
        sys.exit(1)


@meural.command("sleep")
@_debug_option
@_verbose_option
@click.option("--meural", "meural_name", type=str, default=None, help="Meural profile name.")
@click.option("--meural-ip", type=str, default=None, help="Meural IP address.")
@click.pass_context
def meural_sleep(ctx, meural_name, meural_ip):
    """Put the Meural canvas to sleep (screen off)."""
    _ensure_logging(ctx)
    from frameart.meural.controller import sleep

    settings = ctx.obj["settings"]
    profile = _resolve_meural(settings, meural_name, meural_ip)
    if sleep(profile):
        click.secho("Meural is now sleeping.", fg="green")
    else:
        click.secho("Failed to put Meural to sleep.", fg="red", err=True)
        sys.exit(1)


@meural.command("wake")
@_debug_option
@_verbose_option
@click.option("--meural", "meural_name", type=str, default=None, help="Meural profile name.")
@click.option("--meural-ip", type=str, default=None, help="Meural IP address.")
@click.pass_context
def meural_wake(ctx, meural_name, meural_ip):
    """Wake the Meural canvas (screen on)."""
    _ensure_logging(ctx)
    from frameart.meural.controller import wake

    settings = ctx.obj["settings"]
    profile = _resolve_meural(settings, meural_name, meural_ip)
    if wake(profile):
        click.secho("Meural is now awake.", fg="green")
    else:
        click.secho("Failed to wake Meural.", fg="red", err=True)
        sys.exit(1)


@meural.command("next")
@_debug_option
@_verbose_option
@click.option("--meural", "meural_name", type=str, default=None, help="Meural profile name.")
@click.option("--meural-ip", type=str, default=None, help="Meural IP address.")
@click.pass_context
def meural_next(ctx, meural_name, meural_ip):
    """Skip to the next image in the current playlist."""
    _ensure_logging(ctx)
    from frameart.meural.controller import next_image

    settings = ctx.obj["settings"]
    profile = _resolve_meural(settings, meural_name, meural_ip)
    if next_image(profile):
        click.echo("Skipped to next image.")
    else:
        click.secho("Failed.", fg="red", err=True)
        sys.exit(1)


@meural.command("previous")
@_debug_option
@_verbose_option
@click.option("--meural", "meural_name", type=str, default=None, help="Meural profile name.")
@click.option("--meural-ip", type=str, default=None, help="Meural IP address.")
@click.pass_context
def meural_previous(ctx, meural_name, meural_ip):
    """Go to the previous image in the current playlist."""
    _ensure_logging(ctx)
    from frameart.meural.controller import previous_image

    settings = ctx.obj["settings"]
    profile = _resolve_meural(settings, meural_name, meural_ip)
    if previous_image(profile):
        click.echo("Went to previous image.")
    else:
        click.secho("Failed.", fg="red", err=True)
        sys.exit(1)


@meural.command("galleries")
@_debug_option
@_verbose_option
@click.option("--meural", "meural_name", type=str, default=None, help="Meural profile name.")
@click.option("--meural-ip", type=str, default=None, help="Meural IP address.")
@click.pass_context
def meural_galleries(ctx, meural_name, meural_ip):
    """List galleries on the Meural canvas."""
    _ensure_logging(ctx)
    from frameart.meural.controller import list_galleries

    settings = ctx.obj["settings"]
    profile = _resolve_meural(settings, meural_name, meural_ip)

    try:
        galleries = list_galleries(profile)
        if not galleries:
            click.echo("No galleries found.")
            return
        click.echo(f"Found {len(galleries)} gallery(ies):")
        for g in galleries:
            click.echo(f"  [{g.id}] {g.name} ({g.item_count} items)")
    except Exception as e:
        click.secho(f"Failed to list galleries: {e}", fg="red", err=True)
        sys.exit(1)


@meural.command("discover")
@_debug_option
@_verbose_option
@click.option(
    "--subnet", type=str, required=True,
    help="Subnet prefix to scan (e.g., 192.168.1).",
)
@click.option("--timeout", type=float, default=3.0, help="Per-host timeout in seconds.")
@click.pass_context
def meural_discover(ctx, subnet, timeout):
    """Scan a local subnet for Meural canvases."""
    _ensure_logging(ctx)
    from frameart.meural.discovery import discover_subnet

    click.echo(f"Scanning {subnet}.0/24 for Meural canvases...")
    devices = discover_subnet(subnet, timeout=timeout)

    if not devices:
        click.secho("No Meural canvases found.", fg="yellow")
        return

    click.echo(f"Found {len(devices)} Meural canvas(es):\n")
    for d in devices:
        click.echo(
            f"  {d.ip:<16} {d.model:<20} {d.name} "
            f"[{d.orientation}]"
        )


def _resolve_meural(settings, meural_name, meural_ip):
    """Helper to resolve a MeuralProfile from CLI args."""
    from frameart.config import MeuralProfile

    profile = None
    if meural_name and meural_name in settings.meurals:
        profile = settings.meurals[meural_name]
    elif meural_ip:
        profile = MeuralProfile(ip=meural_ip)
    elif len(settings.meurals) == 1:
        profile = next(iter(settings.meurals.values()))

    if profile is None:
        click.secho(
            "No Meural specified. Use --meural or --meural-ip.", fg="red", err=True,
        )
        sys.exit(1)
    return profile


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


# --- serve (HTTP API) --------------------------------------------------------


@main.command()
@_debug_option
@_verbose_option
@click.option("--host", type=str, default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
@click.option("--port", type=int, default=8000, help="Port (default: 8000).")
@click.pass_context
def serve(ctx, host, port):
    """Start the HTTP API server (requires `pip install frameart[api]`)."""
    _ensure_logging(ctx)

    try:
        from frameart.api import run_server
    except ImportError as e:
        click.secho(
            f"Missing API dependencies: {e}\n"
            "Install with: pip install frameart[api]",
            fg="red", err=True,
        )
        sys.exit(1)

    click.echo(f"Starting FrameArt API server on {host}:{port}")
    click.echo(f"  Docs: http://{host}:{port}/docs")
    run_server(host=host, port=port)


if __name__ == "__main__":
    main()
