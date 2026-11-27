# cue.plugin.zsh - Smart AI Zsh Autocomplete & Project History Dropdown
# Compatible with Oh My Zsh, Zinit, Antigen, Sheldon, or direct `source`.

if [[ -z "$ZSH_VERSION" ]]; then
  return 0
fi

# Resolve plugin root directory reliably
export CUE_VERSION="0.2.0"
export CUE_PLUGIN_DIR="${0:A:h}"

# Resolve ~/.cue (or ~/.cuee if an unrelated ~/.cue already existed beforehand)
if [[ -z "${CUE_HOME:-}" ]]; then
  if [[ -d "$HOME/.cue" && ( -f "$HOME/.cue/.cue_home" || -f "$HOME/.cue/config.json" || ( -d "$HOME/.cue/projects" && -d "$HOME/.cue/cache" ) ) ]]; then
    export CUE_HOME="$HOME/.cue"
  elif [[ -d "$HOME/.cuee" && ( -f "$HOME/.cuee/.cue_home" || -f "$HOME/.cuee/config.json" ) ]]; then
    export CUE_HOME="$HOME/.cuee"
  elif [[ -e "$HOME/.cue" ]]; then
    export CUE_HOME="$HOME/.cuee"
  else
    export CUE_HOME="$HOME/.cue"
  fi
fi

# Ensure root context folder exists from the start
mkdir -p "$CUE_HOME/projects" "$CUE_HOME/cache" "$CUE_HOME/logs" 2>/dev/null
[[ ! -f "$CUE_HOME/.cue_home" ]] && print -r -- "cue" > "$CUE_HOME/.cue_home" 2>/dev/null

# Add bin/ to PATH for `cue` binary convenience
if [[ -d "$CUE_PLUGIN_DIR/bin" && ":${PATH}:" != *":${CUE_PLUGIN_DIR}/bin:"* ]]; then
  export PATH="${CUE_PLUGIN_DIR}/bin:${PATH}"
fi

source "$CUE_PLUGIN_DIR/zsh/ui.zsh"
source "$CUE_PLUGIN_DIR/zsh/hooks.zsh"
source "$CUE_PLUGIN_DIR/zsh/zle_dropdown.zsh"

# Register shell hooks cleanly (idempotent)
autoload -Uz add-zsh-hook
add-zsh-hook -d chpwd _cue_chpwd_hook 2>/dev/null
add-zsh-hook -d preexec _cue_preexec_hook 2>/dev/null
add-zsh-hook -d precmd _cue_precmd_hook 2>/dev/null
add-zsh-hook -d zshexit _cue_cleanup_notify_pipe 2>/dev/null

add-zsh-hook chpwd _cue_chpwd_hook
add-zsh-hook preexec _cue_preexec_hook
add-zsh-hook precmd _cue_precmd_hook
add-zsh-hook zshexit _cue_cleanup_notify_pipe

# Interactive shell initialization
if [[ -o interactive ]]; then
  _cue_maybe_onboard
  _cue_register_widgets_and_keys
  _cue_load_dir_cache "$PWD"
  _cue_spawn_analyze "$PWD"
fi
