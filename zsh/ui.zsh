# cue Zsh UI helpers, CLI entrypoint, and first-session onboarding check

function _cue_python() {
  if [[ -n "$CUE_PYTHON" ]]; then
    print -r -- "$CUE_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then
    print -r -- "python3"
  else
    print -r -- ""
  fi
}

function _cue_exec() {
  local pybin="$(_cue_python)"
  [[ -z "$pybin" ]] && return 1
  PYTHONPATH="${CUE_PLUGIN_DIR}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli "$@"
}

function _cue_unhook_current_shell() {
  add-zsh-hook -d chpwd _cue_chpwd_hook 2>/dev/null
  add-zsh-hook -d preexec _cue_preexec_hook 2>/dev/null
  add-zsh-hook -d precmd _cue_precmd_hook 2>/dev/null
  add-zsh-hook -d zshexit _cue_cleanup_notify_pipe 2>/dev/null
  _cue_cleanup_notify_pipe 2>/dev/null
  if zle; then
    _cue_clear_ui
  fi
  zle -A .self-insert self-insert 2>/dev/null || true
  zle -A .backward-delete-char backward-delete-char 2>/dev/null || true
  if (( $+widgets[_cue_orig_tab_widget] )); then
    zle -A _cue_orig_tab_widget expand-or-complete 2>/dev/null || true
  else
    zle -A .expand-or-complete expand-or-complete 2>/dev/null || true
  fi
  zle -D zle-line-init 2>/dev/null || true
  add-zle-hook-widget -d line-pre-redraw _cue_post_redraw 2>/dev/null || true
  unalias cue 2>/dev/null || true
}

# Public CLI command `cue`
function cue() {
  _cue_exec "$@"
  local rc=$?
  # Refresh in-memory cache after commands that mutate context/suggestions/models/token
  case "$1" in
    onboard|token|key|fix|analyze|suggest|ask|prompt|settings|models|context|config)
      _cue_load_dir_cache "$PWD" 2>/dev/null
      ;;
    uninstall)
      if (( rc == 0 )); then
        _cue_unhook_current_shell
        unfunction cue 2>/dev/null || true
      fi
      ;;
  esac
  return $rc
}

# Disable Zsh globbing on `cue` args so unquoted natural-language queries like
# `cue ask what is the version of this plugin?` never fail with "no matches found".
alias cue='noglob cue'

# Run interactive onboarding automatically on first interactive session
function _cue_maybe_onboard() {
  [[ -o interactive ]] || return 0
  [[ "${CUE_NO_ONBOARD:-0}" == "1" ]] && return 0

  local cue_home="${CUE_HOME:-$HOME/.cue}"
  local cfg_file="$cue_home/config.json"

  # Auto-migrate legacy ~/.tawh configuration if ~/.cue/config.json doesn't exist yet
  if [[ ! -f "$cfg_file" && -f "$HOME/.tawh/config.json" ]]; then
    _cue_exec status >/dev/null 2>&1 || true
  fi

  if [[ ! -f "$cfg_file" ]]; then
    _cue_exec onboard
    _cue_load_dir_cache "$PWD"
  fi
}
