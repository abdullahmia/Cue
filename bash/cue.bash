# bash/cue.bash - Bash support for cue (Linux, macOS, WSL, Git Bash)

if [[ -z "${BASH_VERSION:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi

export CUE_VERSION="0.0.1"
export CUE_PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

mkdir -p "$CUE_HOME/projects" "$CUE_HOME/cache" "$CUE_HOME/logs" 2>/dev/null
[[ ! -f "$CUE_HOME/.cue_home" ]] && printf 'cue\n' > "$CUE_HOME/.cue_home" 2>/dev/null

_cue_bash_python() {
  if [[ -n "${CUE_PYTHON:-}" ]]; then
    printf '%s\n' "$CUE_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then
    printf 'python3\n'
  elif command -v python >/dev/null 2>&1; then
    printf 'python\n'
  fi
}

cue() {
  local pybin
  pybin="$(_cue_bash_python)"
  [[ -z "$pybin" ]] && { echo "cue: python3 not found" >&2; return 1; }
  PYTHONPATH="${CUE_PLUGIN_DIR}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli "$@"
}

_CUE_BASH_LAST_DIR=""
_CUE_BASH_LAST_HIST=""

_cue_bash_prompt_hook() {
  local last_exit=$?
  local pybin
  pybin="$(_cue_bash_python)"
  [[ -z "$pybin" ]] && return 0

  # Detect directory change & analyze silently in background
  if [[ "$PWD" != "$_CUE_BASH_LAST_DIR" ]]; then
    _CUE_BASH_LAST_DIR="$PWD"
    (
      PYTHONPATH="${CUE_PLUGIN_DIR}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli analyze --dir "$PWD" --quiet >/dev/null 2>&1
    ) &
    disown 2>/dev/null || true
  fi

  # Capture last executed command from history
  local hist_entry cmd
  hist_entry="$(HISTTIMEFORMAT='' history 1 2>/dev/null)"
  cmd="${hist_entry#*[0-9]  }"
  cmd="${cmd#"${cmd%%[![:space:]]*}"}"

  if [[ -n "$cmd" && "$hist_entry" != "$_CUE_BASH_LAST_HIST" ]]; then
    _CUE_BASH_LAST_HIST="$hist_entry"
    if [[ "$cmd" != cue* ]]; then
      if (( last_exit != 0 && last_exit != 130 )); then
        PYTHONPATH="${CUE_PLUGIN_DIR}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli record --dir "$PWD" --exit-code "$last_exit" --no-ai -- "$cmd" >/dev/null 2>&1
        PYTHONPATH="${CUE_PLUGIN_DIR}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli fix --dir "$PWD" --exit-code "$last_exit" --no-ai "$cmd" 2>/dev/null
      else
        (
          PYTHONPATH="${CUE_PLUGIN_DIR}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -m engine.cli record --dir "$PWD" --exit-code "$last_exit" --no-ai -- "$cmd" >/dev/null 2>&1
        ) &
        disown 2>/dev/null || true
      fi
    fi
  fi
}

_cue_bash_dropdown() {
  local pybin
  pybin="$(_cue_bash_python)"
  [[ -z "$pybin" ]] && return 0

  local selected
  selected="$(PYTHONPATH="${CUE_PLUGIN_DIR}${PYTHONPATH:+:$PYTHONPATH}" "$pybin" -c '
import os, sys, termios, tty
from pathlib import Path
from engine.context_manager import ContextManager
from engine.suggester import build_ranked_suggestions

cm = ContextManager()
ctx = cm.load_project(os.getcwd())
query = sys.argv[1] if len(sys.argv) > 1 else ""
items = build_ranked_suggestions(ctx, partial_input=query, limit=12)
if not items:
    sys.exit(0)

if not sys.stdin.isatty():
    sys.exit(0)

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
idx = 0
visible = items[:8]
out = sys.stderr

def render():
    out.write("\r\n\033[38;5;240m╭─ cue · ↑/↓ select · Enter fill · Esc cancel ───────────────────────╮\033[0m\r\n")
    for i, it in enumerate(visible):
        ptr = "❯" if i == idx else " "
        src = it["source"].upper()[:3]
        cmd = it["cmd"][:36]
        rsn = it["reason"][:24]
        if i == idx:
            out.write(f"\033[48;5;24;38;5;255;1m {ptr} [{src}] {cmd:<36} {rsn:<24} \033[0m\r\n")
        else:
            out.write(f" {ptr} \033[38;5;45m[{src}]\033[0m \033[1m{cmd:<36}\033[0m \033[2m{rsn:<24}\033[0m\r\n")
    out.write("\033[38;5;240m╰────────────────────────────────────────────────────────────────────╯\033[0m")
    out.flush()

def clear_lines(n):
    out.write(f"\033[{n}A\r\033[J")
    out.flush()

lines_drawn = len(visible) + 2
try:
    tty.setraw(fd)
    render()
    while True:
        ch = os.read(fd, 3)
        if ch in (b"\r", b"\n"):
            clear_lines(lines_drawn)
            print(visible[idx]["cmd"])
            break
        if ch in (b"\x1b", b"\x03", b"\x07"):
            clear_lines(lines_drawn)
            break
        if ch in (b"\x1b[A", b"\x1bOA"):
            idx = (idx - 1) % len(visible)
            clear_lines(lines_drawn)
            render()
        elif ch in (b"\x1b[B", b"\x1bOB"):
            idx = (idx + 1) % len(visible)
            clear_lines(lines_drawn)
            render()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
' "${READLINE_LINE:-}" 2>/dev/tty)"

  if [[ -n "$selected" ]]; then
    READLINE_LINE="$selected"
    READLINE_POINT=${#READLINE_LINE}
  fi
}

if [[ $- == *i* ]]; then
  if [[ ! -f "$CUE_HOME/config.json" && -f "$HOME/.tawh/config.json" ]]; then
    cue status >/dev/null 2>&1 || true
  fi
  if [[ "${CUE_NO_ONBOARD:-0}" != "1" && ! -f "$CUE_HOME/config.json" ]]; then
    cue onboard
  fi
  if [[ ";${PROMPT_COMMAND:-};" != *";_cue_bash_prompt_hook;"* ]]; then
    PROMPT_COMMAND="_cue_bash_prompt_hook${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
  fi
  bind -x '"\C-@":_cue_bash_dropdown' 2>/dev/null || true
  bind -x '"\e/":_cue_bash_dropdown' 2>/dev/null || true
fi
