"""
migrate.py — One-time migration script for existing vaults.

Handles the transition from the old .env + config.py system to the new
JSON-based config_manager pattern. Also renames folders, updates wikilinks,
builds the vault index, and cleans up stale state files.

Usage:
    python migrate.py --vault /path/to/vault
    python migrate.py --vault /path/to/vault --dry-run
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

from rich.console import Console

import config_manager
import vault_index

console = Console()

# The hidden vault state dir was named ".research-workflow" before the plugin
# was renamed to "researcher". config_manager.CONFIG_DIR_NAME is now ".researcher".
OLD_CONFIG_DIR_NAME = config_manager.OLD_CONFIG_DIR_NAME


def migrate_vault_dir(vault_root: Path, dry_run: bool = False) -> str:
    """Migrate the hidden vault state dir from the old name to the new one.

    Renames ``{vault}/.research-workflow/`` to ``{vault}/.researcher/`` (the
    current ``config_manager.CONFIG_DIR_NAME``) so existing vaults keep their
    config, state, cases, and learned patterns after the plugin rename. The
    whole directory is moved atomically; contents are not inspected.

    Args:
        vault_root: Root path of the Obsidian vault.
        dry_run: If True, report what would happen without moving anything.

    Returns:
        One of:
          - ``"migrated"``: the old dir existed and the new did not, so it was
            (or, under dry_run, would be) moved to the new name.
          - ``"noop"``: the old dir is absent (already migrated or never used).
          - ``"conflict"``: both dirs exist; left untouched so neither is
            clobbered. A warning is printed and a manual merge is required.
    """
    old = vault_root / OLD_CONFIG_DIR_NAME
    new = vault_root / config_manager.CONFIG_DIR_NAME

    if not old.exists():
        return "noop"

    if new.exists():
        console.print(
            f"  [yellow]Both {OLD_CONFIG_DIR_NAME}/ and {config_manager.CONFIG_DIR_NAME}/ "
            f"exist under {vault_root}; leaving both. Manual merge required.[/yellow]"
        )
        return "conflict"

    if not dry_run:
        shutil.move(str(old), str(new))
    verb = "[dry-run] Would migrate" if dry_run else "Migrated"
    console.print(f"  {verb} {OLD_CONFIG_DIR_NAME}/ -> {config_manager.CONFIG_DIR_NAME}/")
    return "migrated"


def rename_folder(vault_root: Path, old_name: str, new_name: str, dry_run: bool = False) -> int:
    """Rename a top-level vault folder and update all wikilinks referencing it.

    Args:
        vault_root: Root path of the Obsidian vault.
        old_name: Current folder name (e.g. "Areas").
        new_name: Desired folder name (e.g. "Projects").
        dry_run: If True, report changes without making them.

    Returns:
        Count of .md files whose wikilinks were updated.
    """
    old_folder = vault_root / old_name
    new_folder = vault_root / new_name

    if not old_folder.exists():
        console.print(f"  [yellow]Folder {old_name}/ does not exist, skipping rename.[/yellow]")
        return 0

    if new_folder.exists():
        console.print(f"  [red]Target folder {new_name}/ already exists, skipping rename.[/red]")
        return 0

    # Move the folder
    if not dry_run:
        old_folder.rename(new_folder)
    console.print(f"  {'[dry-run] Would rename' if dry_run else 'Renamed'} {old_name}/ -> {new_name}/")

    # Update wikilinks in all .md files across the vault
    # Matches [[Areas/...]] and [[Areas/...|alias]] patterns
    pattern = re.compile(
        r"\[\["
        + re.escape(old_name)
        + r"/"
        + r"([^\]|]+)"  # path after folder name
        + r"(\|[^\]]+)?"  # optional alias
        + r"\]\]"
    )

    files_modified = 0
    for md_file in vault_root.rglob("*.md"):
        # Skip hidden directories
        rel_parts = md_file.relative_to(vault_root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue

        try:
            text = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        new_text = pattern.sub(
            lambda m: "[[" + new_name + "/" + m.group(1) + (m.group(2) or "") + "]]",
            text,
        )

        if new_text != text:
            files_modified += 1
            if dry_run:
                rel = md_file.relative_to(vault_root)
                console.print(f"  [dry-run] Would update wikilinks in {rel}")
            else:
                md_file.write_text(new_text, encoding="utf-8")

    return files_modified


def migrate_env_to_config(vault_root: Path, env_path: Path | None = None, dry_run: bool = False) -> dict:
    """Parse an old .env file and create a new config.json via config_manager.

    Searches for the .env file in these locations (first found wins):
      1. Explicit env_path argument
      2. vault_root/../.env  (project root, one level up)
      3. vault_root/.env

    Args:
        vault_root: Root path of the Obsidian vault.
        env_path: Explicit path to the .env file (optional).
        dry_run: If True, build the config dict but do not write it.

    Returns:
        The new config dict.
    """
    # Locate .env file
    if env_path is None:
        candidates = [
            vault_root.parent / ".env",
            vault_root / ".env",
        ]
        for candidate in candidates:
            if candidate.exists():
                env_path = candidate
                break

    env_vars = {}
    if env_path and env_path.exists():
        console.print(f"  Reading .env from {env_path}")
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            env_vars[key] = value
    else:
        console.print("  [yellow]No .env file found, using defaults.[/yellow]")

    # Build config from defaults, then overlay .env values
    cfg = config_manager.default_config(str(vault_root))

    if "VAULT_PATH" in env_vars:
        cfg["vault_root"] = env_vars["VAULT_PATH"]
    if "INBOX_PATH" in env_vars:
        # Store as relative folder name if it's inside the vault
        inbox = Path(env_vars["INBOX_PATH"])
        try:
            cfg["inbox"] = str(inbox.relative_to(Path(cfg["vault_root"])))
        except ValueError:
            cfg["inbox"] = str(inbox)
    if "DATE_FORMAT" in env_vars:
        cfg["date_format"] = env_vars["DATE_FORMAT"]
    if "TAG_FORMAT" in env_vars:
        cfg["tag_format"] = env_vars["TAG_FORMAT"]
    if "FRONTMATTER_FIELDS" in env_vars:
        cfg["frontmatter_fields"] = [f.strip() for f in env_vars["FRONTMATTER_FIELDS"].split(",")]

    if dry_run:
        console.print("  [dry-run] Would write config.json with:")
        for key, value in cfg.items():
            console.print(f"    {key}: {value}")
    else:
        config_manager.save_config(Path(cfg["vault_root"]), cfg)
        console.print("  Saved config.json")

    return cfg


def cleanup_old_state(vault_root: Path, dry_run: bool = False) -> list[str]:
    """Remove old state files (.tmp/ directory under the scripts dir).

    Args:
        vault_root: Root path of the Obsidian vault.
        dry_run: If True, report what would be removed without removing it.

    Returns:
        List of paths that were (or would be) removed.
    """
    cleaned = []

    # .tmp/ directory relative to the scripts dir (i.e. repo_root/scripts/.tmp
    # or vault_root/.tmp — check both)
    scripts_dir = Path(__file__).parent
    candidates = [
        scripts_dir / ".tmp",
        vault_root / ".tmp",
    ]

    for tmp_dir in candidates:
        if tmp_dir.exists() and tmp_dir.is_dir():
            if dry_run:
                console.print(f"  [dry-run] Would remove {tmp_dir}")
            else:
                shutil.rmtree(tmp_dir)
                console.print(f"  Removed {tmp_dir}")
            cleaned.append(str(tmp_dir))

    if not cleaned:
        console.print("  [green]No old state files to clean up.[/green]")

    return cleaned


def main():
    parser = argparse.ArgumentParser(
        description="Migrate an existing vault from the old .env system to the new JSON config."
    )
    parser.add_argument("--vault", required=True, help="Path to the Obsidian vault root")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen without making changes")
    args = parser.parse_args()

    vault_root = Path(args.vault).resolve()
    if not vault_root.exists():
        console.print(f"[red]Vault path does not exist: {vault_root}[/red]")
        sys.exit(1)

    dry_run = args.dry_run
    if dry_run:
        console.print("[bold yellow]== DRY RUN — no changes will be made ==[/bold yellow]\n")

    # Step 1: Rename Areas/ -> Projects/
    console.print("[bold]Step 1: Rename Areas/ -> Projects/[/bold]")
    count = rename_folder(vault_root, "Areas", "Projects", dry_run=dry_run)
    console.print(f"  Updated wikilinks in {count} file(s)\n")

    # Step 2: Migrate .env -> config.json
    console.print("[bold]Step 2: Migrate .env -> config.json[/bold]")
    cfg = migrate_env_to_config(vault_root, dry_run=dry_run)
    console.print()

    # Step 3: Build vault index
    console.print("[bold]Step 3: Build vault index[/bold]")
    if dry_run:
        console.print("  [dry-run] Would build FTS5 index\n")
    else:
        db_path = vault_index.build_index(vault_root)
        console.print(f"  Built index at {db_path}\n")

    # Step 4: Clean up old state
    console.print("[bold]Step 4: Clean up old state files[/bold]")
    cleaned = cleanup_old_state(vault_root, dry_run=dry_run)
    console.print()

    console.print("[bold green]Migration complete.[/bold green]")


if __name__ == "__main__":
    main()
