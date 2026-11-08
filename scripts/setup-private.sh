
#!/usr/bin/env bash
set -euo pipefail

# DRP Climate Master - Private environment setup

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PRIVATE_REPO="${PRIVATE_REPO:-https://github.com/r-renato/drp-climate-master-private.git}"

# Clone only if the private repository is missing
if [ ! -d private/.git ]; then
    if [ -e private ] && [ -n "$(ls -A private)" ]; then
        echo "ERROR: private/ exists and is not an empty directory."
        exit 1
    fi

    echo "Cloning private repository..."
    git clone "$PRIVATE_REPO" private
fi

# Check required private resources
for path in .claude CLAUDE.md docs; do
    if [ ! -e "private/$path" ]; then
        echo "ERROR: Missing private/$path"
        exit 1
    fi
done

# Create links safely
for path in .claude CLAUDE.md docs; do
    target="private/$path"

    if [ -L "$path" ]; then
        if [ "$(readlink "$path")" = "$target" ]; then
            echo "OK: $path already linked"
            continue
        fi
        echo "ERROR: $path has a different symbolic link"
        exit 1
    fi

    if [ -e "$path" ]; then
        echo "ERROR: $path already exists"
        exit 1
    fi

    ln -s "$target" "$path"
    echo "Linked: $path -> $target"
done

echo "Private environment ready."
