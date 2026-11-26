#!/usr/bin/env zsh
set -e

PLUGIN_DIR="${0:A:h:h}"
TMP_HOME="$(mktemp -d)"
trap 'rm -rf "$TMP_HOME"' EXIT

export CUE_HOME="$TMP_HOME/.cue"
export CUE_NO_ONBOARD=1

# 1. Syntax check all Zsh and Bash scripts
zsh -n "$PLUGIN_DIR/cue.plugin.zsh"
zsh -n "$PLUGIN_DIR/zsh/ui.zsh"
zsh -n "$PLUGIN_DIR/zsh/hooks.zsh"
zsh -n "$PLUGIN_DIR/zsh/zle_dropdown.zsh"
bash -n "$PLUGIN_DIR/bash/cue.bash"

# 2. Load plugin in a clean subshell and verify hooks, widgets, and dropdown rendering logic
zsh -c '
  export CUE_HOME="'"$CUE_HOME"'"
  export CUE_NO_ONBOARD=1
  source "'"$PLUGIN_DIR"'/cue.plugin.zsh"

  # Verify functions exist
  (( $+functions[cue] )) || { echo "Missing cue function"; exit 1; }
  (( $+functions[_cue_arrow_up] )) || { echo "Missing _cue_arrow_up"; exit 1; }
  (( $+functions[_cue_arrow_down] )) || { echo "Missing _cue_arrow_down"; exit 1; }
  (( $+functions[_cue_render_dropdown] )) || { echo "Missing _cue_render_dropdown"; exit 1; }

  # Pre-seed project suggestions in memory and test filtering + dropdown rendering
  _CUE_PROJ_SRCS=(ai proj script)
  _CUE_PROJ_CMDS=("pnpm dev" "pnpm test" "git status")
  _CUE_PROJ_REASONS=("Start dev server" "Project recent (1m ago)" "Check git tree")
  _CUE_ACTIVE_STACK="Next.js"
  _CUE_ACTIVE_MODEL="qwen/qwen3-coder:free"

  BUFFER="pnpm"
  CURSOR=4
  _CUE_MENU_OPEN=1
  _CUE_MENU_INDEX=0
  COLUMNS=80

  _cue_refresh_suggestions
  (( ${#_CUE_ITEMS_CMD} >= 2 )) || { echo "Expected >= 2 filtered items, got ${#_CUE_ITEMS_CMD}"; exit 1; }
  [[ "${_CUE_ITEMS_CMD[1]}" == "pnpm test" ]] || { echo "Expected Recent command first, got: ${_CUE_ITEMS_CMD[1]}"; exit 1; }
  [[ "${_CUE_ITEMS_CMD[2]}" == "pnpm dev" ]] || { echo "Expected AI command second, got: ${_CUE_ITEMS_CMD[2]}"; exit 1; }

  _cue_render_dropdown
  [[ "$POSTDISPLAY" == *"✦ cue"* ]] || { echo "Dropdown box header missing in POSTDISPLAY"; exit 1; }
  [[ "$POSTDISPLAY" == *"pnpm dev"* ]] || { echo "Expected command missing in POSTDISPLAY"; exit 1; }
  (( ${#region_highlight} > 0 )) || { echo "Expected region_highlight entries"; exit 1; }

  # Verify dimmed inline ghost text after error and Tab acceptance
  _CUE_PROJ_SRCS=(fix proj ai)
  _CUE_PROJ_CMDS=("python3 -m pip install pytest" "git status" "pnpm dev")
  _CUE_PROJ_REASONS=("Install missing pytest" "✓ 1m ago" "Start dev server")
  _CUE_AUTO_SHOW_FIX=1
  BUFFER=""
  CURSOR=0
  _cue_on_line_init
  [[ "$_CUE_MENU_OPEN" == "0" ]] || { echo "Expected inline ghost mode without blocking box, got _CUE_MENU_OPEN=$_CUE_MENU_OPEN"; exit 1; }
  [[ "$POSTDISPLAY" == *"python3 -m pip install pytest"* ]] || { echo "Expected dimmed fix ghost text in POSTDISPLAY, got: $POSTDISPLAY"; exit 1; }
  _cue_tab_accept
  [[ "$BUFFER" == "python3 -m pip install pytest" ]] || { echo "Expected Tab to fill fix command into BUFFER, got: $BUFFER"; exit 1; }

  # Verify post-fix retry ghost text and Tab acceptance
  _CUE_AUTO_SHOW_RETRY=1
  _CUE_RETRY_CMD="python3 -m pytest tests/test_cue_engine.py -v"
  BUFFER=""
  CURSOR=0
  _cue_on_line_init
  [[ "$POSTDISPLAY" == *"python3 -m pytest tests/test_cue_engine.py -v"* ]] || { echo "Expected retry ghost text, got: $POSTDISPLAY"; exit 1; }

  # Verify error-fix banner & FIX badge rendering when dropdown is opened
  _CUE_LAST_ERR_CODE=1
  _CUE_LAST_ERR_CMD="python3 -m pytest tests/test_cue_engine.py -v"
  BUFFER=""
  CURSOR=0
  _CUE_MENU_OPEN=1
  _CUE_MENU_INDEX=0
  _cue_refresh_suggestions
  _cue_render_dropdown
  [[ "$POSTDISPLAY" == *"⚠ Exit 1"* ]] || { echo "Error banner missing in POSTDISPLAY"; exit 1; }
  [[ "$POSTDISPLAY" == *"✦ Fix"* ]] || { echo "Fix badge missing in POSTDISPLAY"; exit 1; }

  # Verify suggestion box order & caps: Recent (max 3) -> AI (max 12) -> Others
  _CUE_LAST_ERR_CODE=0
  _CUE_PROJ_SRCS=(proj proj proj proj proj ai ai ai ai ai ai ai ai ai ai ai ai ai ai ai script)
  _CUE_PROJ_CMDS=(r1 r2 r3 r4 r5 a1 a2 a3 a4 a5 a6 a7 a8 a9 a10 a11 a12 a13 a14 a15 s1)
  _CUE_PROJ_REASONS=(d d d d d d d d d d d d d d d d d d d d d)
  _CUE_MODE="smart"
  BUFFER=""
  CURSOR=0
  _CUE_MENU_OPEN=1
  _CUE_MENU_INDEX=0
  _cue_refresh_suggestions
  [[ "${_CUE_ITEMS_SRC[1]}" == "proj" && "${_CUE_ITEMS_SRC[3]}" == "proj" ]] || { echo "Expected first 3 Recent"; exit 1; }
  [[ "${_CUE_ITEMS_SRC[4]}" == "ai" && "${_CUE_ITEMS_SRC[15]}" == "ai" ]] || { echo "Expected next 12 AI"; exit 1; }
  [[ "${_CUE_ITEMS_SRC[16]}" == "script" ]] || { echo "Expected Others after AI, got ${_CUE_ITEMS_SRC[16]}"; exit 1; }

  # Verify 70% terminal height dropdown sizing (LINES=40 -> 25 rows)
  _CUE_LAST_ERR_CODE=0
  _CUE_PROJ_SRCS=()
  _CUE_PROJ_CMDS=()
  _CUE_PROJ_REASONS=()
  for (( k=1; k<=30; k++ )); do
    _CUE_PROJ_SRCS+=(script)
    _CUE_PROJ_CMDS+=("cmd_$k")
    _CUE_PROJ_REASONS+=("reason_$k")
  done
  LINES=40
  COLUMNS=90
  BUFFER="cmd_"
  CURSOR=4
  _CUE_MENU_OPEN=1
  _CUE_MENU_INDEX=0
  _cue_refresh_suggestions
  _cue_render_dropdown
  local -i line_count=${#${(f)POSTDISPLAY}}
  # ghost + header + 25 rows + footer = 28 lines
  (( line_count >= 24 )) || { echo "Expected tall 70% terminal height (>=24 lines for LINES=40), got $line_count"; exit 1; }

  # Verify _cue_post_redraw preserves BUFFER highlight while restoring box
  region_highlight=("0 4 fg=green")
  POSTDISPLAY=""
  _cue_post_redraw
  [[ "$POSTDISPLAY" == *"cmd_25"* ]] || { echo "_cue_post_redraw failed to restore box"; exit 1; }
  [[ "${region_highlight[1]}" == "0 4 fg=green" ]] || { echo "_cue_post_redraw clobbered BUFFER highlight"; exit 1; }

  # Verify foreign ~/.cue causes plugin to select ~/.cuee
  local foreign_home="$(mktemp -d)"
  mkdir -p "$foreign_home/.cue"
  touch "$foreign_home/.cue/unrelated_file"
  local resolved_home
  resolved_home=$(HOME="$foreign_home" CUE_HOME="" CUE_NO_ONBOARD=1 zsh -c "source '$PLUGIN_DIR/cue.plugin.zsh'; print -r -- \$CUE_HOME")
  rm -rf "$foreign_home"
  [[ "$resolved_home" == *"/.cuee" ]] || { echo "Expected ~/.cuee fallback when foreign ~/.cue exists, got: $resolved_home"; exit 1; }
'

# Verify both `cue ask` and `cue prompt` work cleanly (even with unquoted '?' via alias cue='noglob cue')
glob_check="$(zsh -f -c '
  export CUE_HOME="'"$CUE_HOME"'" CUE_NO_ONBOARD=1
  source "'"$PLUGIN_DIR"'/cue.plugin.zsh"
  eval "cue ask --json What is the version of this plugin?"
  eval "cue prompt --json What will be command"
' 2>&1)" || { echo "cue ask/prompt failed: $glob_check"; exit 1; }
[[ "$glob_check" != *"no matches found"* ]] || { echo "Unexpected glob error: $glob_check"; exit 1; }

echo "Zsh integration checks passed!"
