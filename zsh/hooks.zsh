# cue Zsh lifecycle hooks (chpwd, preexec, precmd) and non-blocking async worker

typeset -g _CUE_PLUGIN_DIR="${CUE_PLUGIN_DIR}"
typeset -g _CUE_HOME="${CUE_HOME:-$HOME/.cue}"
typeset -g _CUE_NOTIFY_FD=""
typeset -g _CUE_FIFO=""
typeset -g _CUE_DEBOUNCE_PID=0
typeset -g _CUE_LAST_CMD=""
typeset -g _CUE_CMD_DIR=""
typeset -g _CUE_GEN=0

# Error-fix & post-fix retry ghost-text state
typeset -gi _CUE_AUTO_SHOW_FIX=0
typeset -gi _CUE_AUTO_SHOW_RETRY=0
typeset -gi _CUE_LAST_ERR_CODE=0
typeset -g _CUE_LAST_ERR_CMD=""
typeset -g _CUE_PENDING_RETRY_CMD=""
typeset -g _CUE_RETRY_CMD=""

# In-memory suggestion arrays for zero-latency ZLE rendering
typeset -ga _CUE_PROJ_SRCS
typeset -ga _CUE_PROJ_CMDS
typeset -ga _CUE_PROJ_REASONS
typeset -g _CUE_ACTIVE_STACK=""
typeset -g _CUE_ACTIVE_MODEL=""
typeset -g _CUE_CURRENT_PID=""
typeset -gi _CUE_PROMPT_MODE=0
typeset -gi _CUE_POST_INIT_BOUND=0

function _cue_ensure_notify_pipe() {
  [[ -n "$_CUE_NOTIFY_FD" ]] && return 0
  local cache_dir="${CUE_HOME:-$HOME/.cue}/cache"
  mkdir -p "$cache_dir" 2>/dev/null || return 1
  _CUE_FIFO="$cache_dir/notify_$$.fifo"
  rm -f "$_CUE_FIFO" 2>/dev/null
  mkfifo "$_CUE_FIFO" 2>/dev/null || return 1
  exec {_CUE_NOTIFY_FD}<>"$_CUE_FIFO"
  zle -F "$_CUE_NOTIFY_FD" _cue_on_async_notify 2>/dev/null
}

function _cue_cleanup_notify_pipe() {
  if [[ -n "$_CUE_NOTIFY_FD" ]]; then
    zle -F "$_CUE_NOTIFY_FD" 2>/dev/null
    exec {_CUE_NOTIFY_FD}>&- 2>/dev/null
    _CUE_NOTIFY_FD=""
  fi
  [[ -n "$_CUE_FIFO" ]] && rm -f "$_CUE_FIFO" 2>/dev/null
}

function _cue_resolve_git_root() {
  local dir="$1"
  local cur="$dir"
  while [[ "$cur" != "/" && "$cur" != "$HOME" ]]; do
    if [[ -e "$cur/.git" ]]; then
      print -r -- "$cur"
      return 0
    fi
    cur="${cur:h}"
  done
  print -r -- "$dir"
}

function _cue_load_dir_cache() {
  local dir="${1:-$PWD}"
  local root="$(_cue_resolve_git_root "$dir")"
  local cache_dir="${CUE_HOME:-$HOME/.cue}/cache"
  local projects_dir="${CUE_HOME:-$HOME/.cue}/projects"
  local state_file="${CUE_HOME:-$HOME/.cue}/state.json"

  _CUE_PROJ_SRCS=()
  _CUE_PROJ_CMDS=()
  _CUE_PROJ_REASONS=()

  # 1. Look up existing project context in ~/.cue via dir_map.tsv
  local pid=""
  local dir_map="$cache_dir/dir_map.tsv"
  if [[ -f "$dir_map" ]]; then
    while IFS=$'\t' read -r mpath mpid mstack; do
      if [[ "$mpath" == "$root" || "$mpath" == "$dir" ]]; then
        pid="$mpid"
        _CUE_ACTIVE_STACK="$mstack"
        break
      fi
    done < "$dir_map"
  fi

  # 2. Fallback: check ~/.cue/projects/index.json if dir_map.tsv didn't have it yet
  if [[ -z "$pid" && -f "$projects_dir/index.json" ]]; then
    local pybin="$(_cue_python)"
    if [[ -n "$pybin" ]]; then
      local found_line
      found_line="$("$pybin" -c '
import json, sys
from pathlib import Path
idx_path = Path(sys.argv[1])
root, cur = sys.argv[2], sys.argv[3]
try:
    data = json.loads(idx_path.read_text("utf-8"))
except Exception:
    sys.exit(0)
for pid, info in data.items():
    if info.get("project_path") in (root, cur):
        stacks = ", ".join(info.get("stacks", [])[:3])
        print(f"{pid}\t{stacks}")
        break
' "$projects_dir/index.json" "$root" "$dir" 2>/dev/null)"
      if [[ -n "$found_line" ]]; then
        pid="${found_line%%$'\t'*}"
        _CUE_ACTIVE_STACK="${found_line#*$'\t'}"
      fi
    fi
  fi

  # Read active_model from state.json (or fallback to preferred_model in config.json)
  local cfg_file="${CUE_HOME:-$HOME/.cue}/config.json"
  if [[ -f "$state_file" ]]; then
    local state_raw="$(<"$state_file")"
    if [[ "$state_raw" =~ '"active_model"[[:space:]]*:[[:space:]]*"([^"]+)"' ]]; then
      local raw_model="${match[1]}"
      _CUE_ACTIVE_MODEL="${raw_model##*/}"
    fi
  fi
  if [[ -z "$_CUE_ACTIVE_MODEL" && -f "$cfg_file" ]]; then
    local cfg_raw="$(<"$cfg_file")"
    if [[ "$cfg_raw" =~ '"preferred_model"[[:space:]]*:[[:space:]]*"([^"]+)"' ]]; then
      local pref_model="${match[1]}"
      _CUE_ACTIVE_MODEL="${pref_model##*/}"
    fi
  fi
  _CUE_PROMPT_MODE=0
  if [[ -f "$cfg_file" ]]; then
    local cfg_pm_raw="$(<"$cfg_file")"
    if [[ "$cfg_pm_raw" =~ '"prompt_mode"[[:space:]]*:[[:space:]]*true' ]]; then
      _CUE_PROMPT_MODE=1
    fi
  fi

  # Ensure TSV cache exists if project JSON exists in ~/.cue/projects
  if [[ -n "$pid" && ! -f "$cache_dir/proj_${pid}.tsv" && -f "$projects_dir/${pid}.json" ]]; then
    local pybin="$(_cue_python)"
    [[ -n "$pybin" ]] && PYTHONPATH="${CUE_PLUGIN_DIR}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli suggest --dir "$dir" --json >/dev/null 2>&1
  fi

  if [[ -n "$pid" && -f "$cache_dir/proj_${pid}.tsv" ]]; then
    _CUE_CURRENT_PID="$pid"
    local src cmd reason
    while IFS=$'\t' read -r src cmd reason; do
      [[ -z "$cmd" ]] && continue
      _CUE_PROJ_SRCS+=("$src")
      _CUE_PROJ_CMDS+=("$cmd")
      _CUE_PROJ_REASONS+=("$reason")
    done < "$cache_dir/proj_${pid}.tsv"
  fi
}

function _cue_update_dir_map() {
  local json_payload="$1"
  local dir="$2"
  local cache_dir="${CUE_HOME:-$HOME/.cue}/cache"
  local dir_map="$cache_dir/dir_map.tsv"
  local root="$(_cue_resolve_git_root "$dir")"
  local pid="" stacks=""
  if [[ "$json_payload" =~ '"project_id"[[:space:]]*:[[:space:]]*"([^"]+)"' ]]; then
    pid="${match[1]}"
  fi
  if [[ "$json_payload" =~ '"stacks"[[:space:]]*:[[:space:]]*\[([^\]]*)\]' ]]; then
    stacks="${match[1]//\"/}"
    stacks="${stacks//, /, }"
  fi
  [[ -z "$pid" ]] && return 0

  local tmp="$dir_map.tmp.$$"
  if [[ -f "$dir_map" ]]; then
    grep -v -F -- "${root}"$'\t' "$dir_map" > "$tmp" 2>/dev/null || true
  fi
  print -r -- "${root}"$'\t'"${pid}"$'\t'"${stacks}" >> "$tmp"
  mv -f "$tmp" "$dir_map" 2>/dev/null
}

function _cue_spawn_analyze() {
  setopt LOCAL_OPTIONS NO_BG_NICE
  local target_dir="${1:-$PWD}"
  local pybin="$(_cue_python)"
  [[ -z "$pybin" ]] && return 0
  _cue_ensure_notify_pipe || true

  local fifo="$_CUE_FIFO"
  local plugin_dir="$CUE_PLUGIN_DIR"
  (
    local out
    out="$(PYTHONPATH="${plugin_dir}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli analyze --dir "$target_dir" --json 2>/dev/null)"
    if [[ -n "$out" && -p "$fifo" ]]; then
      _cue_update_dir_map "$out" "$target_dir"
      print -r -- "analyze:$target_dir" > "$fifo" 2>/dev/null
    fi
  ) &!
}

function _cue_schedule_ai_suggest() {
  setopt LOCAL_OPTIONS NO_BG_NICE
  local query="$1"
  local target_dir="$PWD"
  local pybin="$(_cue_python)"
  [[ -z "$pybin" ]] && return 0

  if (( _CUE_DEBOUNCE_PID > 0 )); then
    kill "$_CUE_DEBOUNCE_PID" 2>/dev/null
    _CUE_DEBOUNCE_PID=0
  fi

  (( _CUE_GEN++ ))
  local my_gen=$_CUE_GEN
  _cue_ensure_notify_pipe || return 0
  local fifo="$_CUE_FIFO"
  local plugin_dir="$CUE_PLUGIN_DIR"

  local suggest_mode="complete"

  (
    sleep 0.30
    PYTHONPATH="${plugin_dir}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli suggest --dir "$target_dir" --input "$query" --mode "$suggest_mode" --live --json >/dev/null 2>&1
    [[ -p "$fifo" ]] && print -r -- "suggest:${my_gen}:${target_dir}" > "$fifo" 2>/dev/null
  ) &!
  _CUE_DEBOUNCE_PID=$!
}

function _cue_on_async_notify() {
  local fd="$1"
  local line=""
  while IFS= read -r -t 0 -u "$fd" line 2>/dev/null; do
    :
  done
  _cue_load_dir_cache "$PWD"
  if zle; then
    zle _cue_async_redraw 2>/dev/null || true
  fi
}

function _cue_chpwd_hook() {
  _CUE_AUTO_SHOW_FIX=0
  _CUE_AUTO_SHOW_RETRY=0
  _CUE_LAST_ERR_CODE=0
  _CUE_LAST_ERR_CMD=""
  _CUE_PENDING_RETRY_CMD=""
  _CUE_RETRY_CMD=""
  _cue_load_dir_cache "$PWD"
  _cue_spawn_analyze "$PWD"
}

function _cue_preexec_hook() {
  _CUE_LAST_CMD="$1"
  _CUE_CMD_DIR="$PWD"
}

function _cue_precmd_hook() {
  setopt LOCAL_OPTIONS NO_BG_NICE
  local last_exit=$?
  _cue_ensure_notify_pipe

  # Guard against plugins loaded later (e.g., zsh-autosuggestions, bindkey -e, compinit, fzf) overriding cue widgets/keys
  if (( $+functions[_zsh_autosuggest_disable] )); then
    _zsh_autosuggest_disable 2>/dev/null
  fi
  if (( ! _CUE_POST_INIT_BOUND )) || [[ "${widgets[self-insert]}" != "user:_cue_self_insert" || "${widgets[expand-or-complete]}" != "user:_cue_tab_accept" ]]; then
    _CUE_POST_INIT_BOUND=1
    _cue_register_widgets_and_keys 2>/dev/null
  fi

  if [[ -n "$_CUE_LAST_CMD" ]]; then
    local cmd_to_save="$_CUE_LAST_CMD"
    local cmd_dir="${_CUE_CMD_DIR:-$PWD}"
    _CUE_LAST_CMD=""
    _CUE_CMD_DIR=""
    local pybin="$(_cue_python)"
    local plugin_dir="$CUE_PLUGIN_DIR"

    if (( last_exit != 0 && last_exit != 130 )) && [[ "$cmd_to_save" != cue* ]]; then
      _CUE_LAST_ERR_CODE=$last_exit
      _CUE_LAST_ERR_CMD="$cmd_to_save"
      _CUE_PENDING_RETRY_CMD="$cmd_to_save"
      _CUE_AUTO_SHOW_RETRY=0
      _CUE_RETRY_CMD=""
      if [[ -n "$pybin" ]]; then
        # Compute instant local fixes synchronously (<30ms) so dimmed fix ghost appears immediately
        PYTHONPATH="${plugin_dir}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli record --dir "$cmd_dir" --exit-code "$last_exit" --no-ai --json -- "$cmd_to_save" >/dev/null 2>&1
        _cue_load_dir_cache "$PWD"
        if [[ "${_CUE_PROJ_SRCS[1]}" == "fix" ]]; then
          _CUE_AUTO_SHOW_FIX=1
        fi
        # Also enrich with AI fix suggestions asynchronously
        local fifo="$_CUE_FIFO"
        (
          PYTHONPATH="${plugin_dir}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli fix --dir "$cmd_dir" --exit-code "$last_exit" --json "$cmd_to_save" >/dev/null 2>&1
          [[ -p "$fifo" ]] && print -r -- "fix:$cmd_dir" > "$fifo" 2>/dev/null
        ) &!
      fi
    else
      _CUE_AUTO_SHOW_FIX=0
      _CUE_LAST_ERR_CODE=0
      _CUE_LAST_ERR_CMD=""
      # If a previous command had failed and this command succeeded (e.g. running the fix),
      # suggest the original failed command next!
      if (( last_exit == 0 )) && [[ -n "$_CUE_PENDING_RETRY_CMD" && "$cmd_to_save" != "$_CUE_PENDING_RETRY_CMD" && "$cmd_to_save" != cue* ]]; then
        _CUE_RETRY_CMD="$_CUE_PENDING_RETRY_CMD"
        _CUE_AUTO_SHOW_RETRY=1
      else
        _CUE_AUTO_SHOW_RETRY=0
        _CUE_RETRY_CMD=""
      fi
      _CUE_PENDING_RETRY_CMD=""

      if [[ -n "$pybin" ]]; then
        (
          PYTHONPATH="${plugin_dir}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli record --dir "$cmd_dir" --exit-code "$last_exit" --no-ai -- "$cmd_to_save" >/dev/null 2>&1
        ) &!
      fi
    fi
  fi
}
