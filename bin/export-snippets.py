"""Copy your local snippets into the app's tracked bundle before committing an update."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import settings
from fsutil import write_bytes


def main():
    source = settings.snippets_file()
    if not source.is_file():
        raise SystemExit(f"No local snippets file to export: {source}")
    text = source.read_text(encoding="utf-8-sig")
    # The imported file's provenance may name a private source-machine folder.
    if text.startswith("// Imported from Obsidian Latex Suite:"):
        text = "// Imported from Obsidian Latex Suite (MIT).\n" + text.partition("\n")[2]
    write_bytes(settings.BUNDLED_SNIPPETS, text.encode("utf-8"))
    print(f"Exported snippets to {settings.BUNDLED_SNIPPETS}")


if __name__ == "__main__":
    main()
