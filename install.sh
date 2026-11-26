#!/usr/bin/env bash
set -euo pipefail

CUE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "$CUE_DIR/bin/cue"

echo ""
echo "✦ Installing cue Terminal Plugin (macOS / Linux)..."
echo "  Plugin directory: $CUE_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ Error: python3 is required."
  exit 1
fi

if command -v zsh >/dev/null 2>&1; then
  ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"
  SOURCE_LINE="source \"$CUE_DIR/cue.plugin.zsh\""

  OMZ_CUSTOM="${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}"
  if [[ -d "$HOME/.oh-my-zsh" && -d "$OMZ_CUSTOM/plugins" ]]; then
    TARGET_LINK="$OMZ_CUSTOM/plugins/cue"
    if [[ ! -e "$TARGET_LINK" ]]; then
      ln -s "$CUE_DIR" "$TARGET_LINK"
      echo "  ✓ Linked into Oh My Zsh custom plugins: $TARGET_LINK"
    fi
  fi

  touch "$ZSHRC"
  # Remove stale legacy tawh.plugin.zsh line/symlink if present
  if [[ -L "$OMZ_CUSTOM/plugins/tawh" ]]; then
    rm -f "$OMZ_CUSTOM/plugins/tawh"
  fi
  if grep -Fq "tawh.plugin.zsh" "$ZSHRC"; then
    grep -v -E "tawh\.plugin\.zsh|# tawh —" "$ZSHRC" > "${ZSHRC}.cue.tmp" && mv "${ZSHRC}.cue.tmp" "$ZSHRC"
  fi
  if grep -Fq "cue.plugin.zsh" "$ZSHRC"; then
    echo "  ✓ Zsh already configured in $ZSHRC"
  else
    {
      echo ""
      echo "# cue — Smart AI Zsh autocomplete & project history"
      echo "$SOURCE_LINE"
    } >> "$ZSHRC"
    echo "  ✓ Added Zsh plugin source line to $ZSHRC"
  fi
fi

if command -v bash >/dev/null 2>&1; then
  BASHRC="$HOME/.bashrc"
  if [[ -f "$BASHRC" ]] || [[ "${SHELL:-}" == */bash ]]; then
    touch "$BASHRC"
    if grep -Fq "bash/cue.bash" "$BASHRC"; then
      echo "  ✓ Bash already configured in $BASHRC"
    else
      {
        echo ""
        echo "# cue — Smart AI Bash autocomplete & error-fix assistant"
        echo "source \"$CUE_DIR/bash/cue.bash\""
      } >> "$BASHRC"
      echo "  ✓ Added Bash adapter source line to $BASHRC"
    fi
  fi
fi

echo ""
echo "✓ Installation complete!"
echo "  Start a new terminal session (or run: source \"$CUE_DIR/cue.plugin.zsh\")"
echo "  On your first session, the onboarding wizard will ask for your OpenRouter API key."
echo ""
