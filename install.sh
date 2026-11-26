#!/usr/bin/env bash
set -euo pipefail

CUE_REPO_URL="${CUE_REPO_URL:-https://github.com/abdullahmia/Cue.git}"
CUE_INSTALL_DIR="${CUE_INSTALL_DIR:-$HOME/.local/share/cue}"

SCRIPT_SRC="${BASH_SOURCE[0]:-}"
CUE_DIR=""
if [[ -n "$SCRIPT_SRC" && -f "$SCRIPT_SRC" ]]; then
  CANDIDATE_DIR="$(cd "$(dirname "$SCRIPT_SRC")" && pwd)"
  if [[ -f "$CANDIDATE_DIR/cue.plugin.zsh" ]]; then
    CUE_DIR="$CANDIDATE_DIR"
  fi
fi

echo ""
echo "✦ Installing cue Terminal Plugin (macOS / Linux)..."

if [[ -z "$CUE_DIR" ]]; then
  if ! command -v git >/dev/null 2>&1; then
    echo "✗ Error: git is required for standalone installation."
    exit 1
  fi
  CUE_DIR="$CUE_INSTALL_DIR"
  mkdir -p "$(dirname "$CUE_DIR")"
  if [[ -d "$CUE_DIR/.git" ]]; then
    echo "  Updating existing repository in $CUE_DIR..."
    git -C "$CUE_DIR" pull --ff-only
  elif [[ -e "$CUE_DIR" ]]; then
    if [[ ! -f "$CUE_DIR/cue.plugin.zsh" ]]; then
      echo "✗ Error: $CUE_DIR already exists and is not a cue installation."
      exit 1
    fi
  else
    echo "  Cloning $CUE_REPO_URL into $CUE_DIR..."
    git clone --depth 1 "$CUE_REPO_URL" "$CUE_DIR"
  fi
fi

chmod +x "$CUE_DIR/bin/cue"
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
    grep -v -E "tawh\.plugin\.zsh|# tawh .*Smart AI" "$ZSHRC" > "${ZSHRC}.cue.tmp" && mv "${ZSHRC}.cue.tmp" "$ZSHRC"
  fi
  if grep -Fq "cue.plugin.zsh" "$ZSHRC"; then
    echo "  ✓ Zsh already configured in $ZSHRC"
  else
    {
      echo ""
      echo "# cue - Smart AI Zsh autocomplete & project history"
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
        echo "# cue - Smart AI Bash autocomplete & error-fix assistant"
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
