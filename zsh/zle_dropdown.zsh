# cue ZLE inline ghost text + modern Up/Down arrow interactive dropdown

zmodload zsh/parameter 2>/dev/null

typeset -g _CUE_MENU_OPEN=0
typeset -gi _CUE_MENU_INDEX=-1
typeset -gi _CUE_MENU_SCROLL=0
typeset -g _CUE_MODE="smart"   # "smart" (recent+AI+scripts) or "history" (recent history first)
typeset -g _CUE_GHOST_CMD=""
typeset -g _CUE_GHOST_HINT=""
typeset -g _CUE_LAST_POSTDISPLAY=""
typeset -ga _CUE_LAST_BOX_HL
typeset -gi _CUE_IN_TAB_ACCEPT=0

typeset -ga _CUE_ITEMS_SRC
typeset -ga _CUE_ITEMS_CMD
typeset -ga _CUE_ITEMS_REASON

function _cue_collect_recent_history() {
  local -i limit=120
  local -i id="$HISTCMD"
  local -i min_id=$(( id > limit ? id - limit : 1 ))
  local cmd

  _CUE_HIST_LIST=()
  typeset -A _seen_hist
  while (( id >= min_id )); do
    if (( $+history[$id] )); then
      cmd="${history[$id]}"
      cmd="${cmd//$'\n'/ }"
      if [[ -n "$cmd" && -z "${_seen_hist[$cmd]}" ]]; then
        _seen_hist[$cmd]=1
        _CUE_HIST_LIST+=("$cmd")
      fi
    fi
    (( id-- ))
  done
}

function _cue_matches_query() {
  local cmd="$1"
  local q="$2"
  [[ -z "$q" ]] && return 0
  local lc_cmd="${(L)cmd}"
  local lc_q="${(L)q}"
  [[ "$lc_cmd" == "$lc_q"* ]] && return 0
  [[ "$lc_cmd" == *"$lc_q"* ]] && return 0
  return 1
}

function _cue_refresh_suggestions() {
  local query="$BUFFER"
  local trimmed="${query#"${query%%[![:space:]]*}"}"
  _CUE_ITEMS_SRC=()
  _CUE_ITEMS_CMD=()
  _CUE_ITEMS_REASON=()
  _CUE_GHOST_CMD=""

  typeset -A seen_cmds
  local -i i n=${#_CUE_PROJ_CMDS}
  local src cmd reason

  if [[ "$_CUE_MODE" == "history" ]]; then
    # 1. Project recent history first
    for (( i=1; i<=n; i++ )); do
      src="${_CUE_PROJ_SRCS[$i]}"
      [[ "$src" != "proj" ]] && continue
      cmd="${_CUE_PROJ_CMDS[$i]}"
      reason="${_CUE_PROJ_REASONS[$i]}"
      if _cue_matches_query "$cmd" "$trimmed" && [[ -z "${seen_cmds[$cmd]}" ]]; then
        seen_cmds[$cmd]=1
        _CUE_ITEMS_SRC+=("$src")
        _CUE_ITEMS_CMD+=("$cmd")
        _CUE_ITEMS_REASON+=("$reason")
      fi
    done

    # 2. Global Zsh history
    _cue_collect_recent_history
    for cmd in "${_CUE_HIST_LIST[@]}"; do
      if _cue_matches_query "$cmd" "$trimmed" && [[ -z "${seen_cmds[$cmd]}" ]]; then
        seen_cmds[$cmd]=1
        _CUE_ITEMS_SRC+=("hist")
        _CUE_ITEMS_CMD+=("$cmd")
        _CUE_ITEMS_REASON+=("Shell history")
      fi
      (( ${#_CUE_ITEMS_CMD} >= 60 )) && break
    done

    # 3. Append fixes, scripts, and AI (only when AI is enabled)
    for (( i=1; i<=n; i++ )); do
      cmd="${_CUE_PROJ_CMDS[$i]}"
      src="${_CUE_PROJ_SRCS[$i]}"
      reason="${_CUE_PROJ_REASONS[$i]}"
      [[ "$src" == "ai" && $_CUE_AI_ENABLED -eq 0 ]] && continue
      if _cue_matches_query "$cmd" "$trimmed" && [[ -z "${seen_cmds[$cmd]}" ]]; then
        seen_cmds[$cmd]=1
        _CUE_ITEMS_SRC+=("$src")
        _CUE_ITEMS_CMD+=("$cmd")
        _CUE_ITEMS_REASON+=("$reason")
      fi
    done
  else
    # Smart suggestion box order:
    # 0) active fix/retry -> 1) Recent (max 3) -> 2) AI (max 12) -> 3) Others (Script & Hist)
    _cue_collect_recent_history
    local -a fix_s fix_c fix_r
    local -a rec_s rec_c rec_r
    local -a ai_s ai_c ai_r
    local -a oth_s oth_c oth_r
    local lc_q="${(L)trimmed}"

    # Helper pass: prefix matches first, then substring matches, separated into buckets
    local pass
    for pass in prefix substr; do
      [[ "$pass" == "prefix" && -z "$trimmed" ]] && continue
      for (( i=1; i<=n; i++ )); do
        cmd="${_CUE_PROJ_CMDS[$i]}"
        src="${_CUE_PROJ_SRCS[$i]}"
        reason="${_CUE_PROJ_REASONS[$i]}"
        [[ -n "$trimmed" && "$cmd" == "$trimmed" ]] && continue
        [[ -n "${seen_cmds[$cmd]}" ]] && continue
        if [[ "$pass" == "prefix" ]]; then
          [[ "${(L)cmd}" != "$lc_q"* ]] && continue
        else
          _cue_matches_query "$cmd" "$trimmed" || continue
        fi
        case "$src" in
          fix|retry)
            seen_cmds[$cmd]=1
            fix_s+=("$src"); fix_c+=("$cmd"); fix_r+=("$reason")
            ;;
          proj)
            if (( ${#rec_c} < 3 )); then
              seen_cmds[$cmd]=1
              rec_s+=("$src"); rec_c+=("$cmd"); rec_r+=("$reason")
            fi
            ;;
          ai)
            if (( _CUE_AI_ENABLED && ${#ai_c} < 12 )); then
              seen_cmds[$cmd]=1
              ai_s+=("$src"); ai_c+=("$cmd"); ai_r+=("$reason")
            fi
            ;;
          *)
            seen_cmds[$cmd]=1
            oth_s+=("$src"); oth_c+=("$cmd"); oth_r+=("$reason")
            ;;
        esac
      done

      for cmd in "${_CUE_HIST_LIST[@]}"; do
        [[ -n "$trimmed" && "$cmd" == "$trimmed" ]] && continue
        [[ -n "${seen_cmds[$cmd]}" ]] && continue
        if [[ "$pass" == "prefix" ]]; then
          [[ "${(L)cmd}" != "$lc_q"* ]] && continue
        else
          _cue_matches_query "$cmd" "$trimmed" || continue
        fi
        seen_cmds[$cmd]=1
        oth_s+=("hist"); oth_c+=("$cmd"); oth_r+=("Shell history")
        (( ${#oth_c} >= 50 )) && break
      done
    done

    _CUE_ITEMS_SRC=("${fix_s[@]}" "${rec_s[@]}" "${ai_s[@]}" "${oth_s[@]}")
    _CUE_ITEMS_CMD=("${fix_c[@]}" "${rec_c[@]}" "${ai_c[@]}" "${oth_c[@]}")
    _CUE_ITEMS_REASON=("${fix_r[@]}" "${rec_r[@]}" "${ai_r[@]}" "${oth_r[@]}")
  fi

  local -i total=${#_CUE_ITEMS_CMD}
  if (( total == 0 )); then
    _CUE_MENU_INDEX=-1
    _CUE_MENU_SCROLL=0
    return 0
  fi

  if (( _CUE_MENU_INDEX >= total )); then
    _CUE_MENU_INDEX=$(( total - 1 ))
  fi

  local -i ghost_idx=$(( _CUE_MENU_INDEX >= 0 ? _CUE_MENU_INDEX + 1 : 1 ))
  local candidate="${_CUE_ITEMS_CMD[$ghost_idx]}"
  if [[ -n "$BUFFER" && "${(L)candidate}" == "${(L)BUFFER}"* && ${#candidate} -gt ${#BUFFER} ]]; then
    _CUE_GHOST_CMD="$candidate"
  elif [[ -z "$BUFFER" && $_CUE_MENU_INDEX -ge 0 ]]; then
    _CUE_GHOST_CMD="$candidate"
  elif [[ -n "$BUFFER" ]]; then
    local item
    for item in "${_CUE_ITEMS_CMD[@]}"; do
      if [[ "${(L)item}" == "${(L)BUFFER}"* && ${#item} -gt ${#BUFFER} ]]; then
        _CUE_GHOST_CMD="$item"
        break
      fi
    done
  fi
}

function _cue_clear_ui() {
  POSTDISPLAY=""
  _CUE_LAST_POSTDISPLAY=""
  _CUE_LAST_BOX_HL=()
  region_highlight=()
  _CUE_MENU_OPEN=0
  _CUE_MENU_INDEX=-1
  _CUE_MENU_SCROLL=0
  _CUE_GHOST_CMD=""
  _CUE_GHOST_HINT=""
}

function _cue_target_rows() {
  local -i term_lines=${LINES:-24}
  local -i max_rows=$(( term_lines * 70 / 100 - 3 ))
  if (( _CUE_LAST_ERR_CODE != 0 )) && [[ "${_CUE_PROJ_SRCS[1]}" == "fix" ]]; then
    (( max_rows-- ))
  fi
  (( max_rows < 6 )) && max_rows=6
  print -r -- "$max_rows"
}

function _cue_render_dropdown() {
  # Preserve any syntax highlighting on BUFFER (indices < ${#BUFFER}), strip old POSTDISPLAY highlights
  local -i b_len=${#BUFFER}
  local -a buf_hl=()
  local h_entry
  for h_entry in "${region_highlight[@]}"; do
    local -i h_start="${h_entry%% *}"
    (( h_start < b_len )) && buf_hl+=("$h_entry")
  done

  local -i total=${#_CUE_ITEMS_CMD}
  if (( total == 0 )) || (( ! _CUE_MENU_OPEN )); then
    if [[ -n "$_CUE_GHOST_CMD" ]]; then
      local ghost_tail=""
      if [[ -z "$BUFFER" ]]; then
        ghost_tail="$_CUE_GHOST_CMD"
      elif [[ "${(L)_CUE_GHOST_CMD}" == "${(L)BUFFER}"* && ${#_CUE_GHOST_CMD} -gt ${#BUFFER} ]]; then
        ghost_tail="${_CUE_GHOST_CMD:${#BUFFER}}"
      fi
      if [[ -n "$ghost_tail" ]]; then
        POSTDISPLAY="${ghost_tail}${_CUE_GHOST_HINT}"
        _CUE_LAST_POSTDISPLAY="$POSTDISPLAY"
        local -i g_end=$(( b_len + ${#ghost_tail} ))
        _CUE_LAST_BOX_HL=("${b_len} ${g_end} fg=242")
        if [[ -n "$_CUE_GHOST_HINT" ]]; then
          _CUE_LAST_BOX_HL+=("${g_end} $(( g_end + ${#_CUE_GHOST_HINT} )) fg=238")
        fi
        region_highlight=("${buf_hl[@]}" "${_CUE_LAST_BOX_HL[@]}")
        return 0
      fi
    fi
    POSTDISPLAY=""
    _CUE_LAST_POSTDISPLAY=""
    _CUE_LAST_BOX_HL=()
    region_highlight=("${buf_hl[@]}")
    return 0
  fi

  local -i cols=${COLUMNS:-80}
  (( cols < 48 )) && cols=48
  local -i box_w=$(( cols > 102 ? 100 : cols - 2 ))
  local -i inner_w=$(( box_w - 2 ))

  local ghost_tail=""
  if [[ -n "$_CUE_GHOST_CMD" && ${#_CUE_GHOST_CMD} -gt ${#BUFFER} ]]; then
    ghost_tail="${_CUE_GHOST_CMD:${#BUFFER}}"
  fi

  local out="$ghost_tail"
  local -i offset=$(( ${#BUFFER} + ${#ghost_tail} ))
  local -a hl=()
  if [[ -n "$ghost_tail" ]]; then
    hl+=("${#BUFFER} ${offset} fg=242")
  fi

  # Header bar with mode indicator
  local stack_lbl="${_CUE_ACTIVE_STACK:-folder}"
  local model_lbl="local"
  if (( _CUE_AI_ENABLED )); then
    model_lbl="${_CUE_ACTIVE_MODEL:-ai}"
  fi
  local mode_lbl="SMART"
  if [[ "$_CUE_MODE" == "history" ]]; then
    mode_lbl="HISTORY"
  elif (( _CUE_LAST_ERR_CODE != 0 )) && [[ "${_CUE_PROJ_SRCS[1]}" == "fix" ]]; then
    mode_lbl="FIX"
  fi
  local pos_lbl=""
  if (( _CUE_MENU_INDEX >= 0 )); then
    pos_lbl=" [$(( _CUE_MENU_INDEX + 1 ))/${total}]"
  else
    pos_lbl=" [${total}]"
  fi
  local hdr_text=" ✦ cue · ${mode_lbl} · ${stack_lbl} · ${model_lbl}${pos_lbl} "
  if (( ${#hdr_text} > inner_w - 2 )); then
    hdr_text="${hdr_text[1,$(( inner_w - 3 ))]}… "
  fi
  local -i pad_dashes=$(( inner_w - 1 - ${#hdr_text} ))
  (( pad_dashes < 1 )) && pad_dashes=1
  local dashes="${(l:$pad_dashes::─:)}"
  local hdr_line=$'\n'"╭─${hdr_text}${dashes}╮"
  local -i hdr_start=$(( offset + 1 ))
  local -i hdr_end=$(( offset + ${#hdr_line} ))
  hl+=("${hdr_start} ${hdr_end} fg=240")
  hl+=("$(( hdr_start + 3 )) $(( hdr_start + 8 )) fg=45,bold")
  out+="$hdr_line"
  offset+=${#hdr_line}

  # Optional error banner strip when previous command failed
  if (( _CUE_LAST_ERR_CODE != 0 )) && [[ "${_CUE_PROJ_SRCS[1]}" == "fix" ]]; then
    local err_msg=" ⚠ Exit ${_CUE_LAST_ERR_CODE}: ${_CUE_LAST_ERR_CMD}"
    if (( ${#err_msg} > inner_w - 1 )); then
      err_msg="${err_msg[1,$(( inner_w - 2 ))]}…"
    fi
    err_msg="${(r:$inner_w:)err_msg}"
    local err_line=$'\n'"│${err_msg}│"
    local -i e_start=$(( offset + 1 ))
    local -i e_end=$(( offset + ${#err_line} ))
    hl+=("${e_start} $(( e_start + 1 )) fg=240")
    hl+=("$(( e_start + 1 )) $(( e_end - 1 )) bg=52,fg=217,bold")
    hl+=("$(( e_end - 1 )) ${e_end} fg=240")
    out+="$err_line"
    offset+=${#err_line}
  fi

  # Scroll window sized to ~70% of terminal height
  local -i vis=$(_cue_target_rows)
  (( total < vis )) && vis=$total
  if (( _CUE_MENU_INDEX >= 0 )); then
    if (( _CUE_MENU_INDEX < _CUE_MENU_SCROLL )); then
      _CUE_MENU_SCROLL=$_CUE_MENU_INDEX
    elif (( _CUE_MENU_INDEX >= _CUE_MENU_SCROLL + vis )); then
      _CUE_MENU_SCROLL=$(( _CUE_MENU_INDEX - vis + 1 ))
    fi
  else
    _CUE_MENU_SCROLL=0
  fi

  local -i cmd_col=$(( inner_w * 46 / 100 ))
  (( cmd_col < 22 )) && cmd_col=22
  (( cmd_col > 46 )) && cmd_col=46
  local -i desc_col=$(( inner_w - 15 - cmd_col ))
  (( desc_col < 8 )) && desc_col=8

  local -i row idx
  for (( row=0; row<vis; row++ )); do
    idx=$(( _CUE_MENU_SCROLL + row + 1 ))
    local src="${_CUE_ITEMS_SRC[$idx]}"
    local cmd="${_CUE_ITEMS_CMD[$idx]}"
    local reason="${_CUE_ITEMS_REASON[$idx]}"

    local badge="⌘ Hist  "
    local badge_fg="fg=111"
    case "$src" in
      fix)    badge="✦ Fix   "; badge_fg="fg=203,bold" ;;
      retry)  badge="↻ Retry "; badge_fg="fg=156,bold" ;;
      ai)     badge="✦ AI    "; badge_fg="fg=45,bold" ;;
      proj)   badge="↺ Recent"; badge_fg="fg=78,bold" ;;
      script) badge="⚙ Script"; badge_fg="fg=221" ;;
      hist)   badge="⌘ Hist  "; badge_fg="fg=111" ;;
    esac

    local pointer=" "
    local is_sel=0
    if (( idx - 1 == _CUE_MENU_INDEX )); then
      pointer="❯"
      is_sel=1
    fi

    local disp_cmd="$cmd"
    if (( ${#disp_cmd} > cmd_col )); then
      disp_cmd="${disp_cmd[1,$(( cmd_col - 1 ))]}…"
    fi
    local padded_cmd="${(r:$cmd_col:)disp_cmd}"

    local disp_desc="$reason"
    if (( ${#disp_desc} > desc_col )); then
      disp_desc="${disp_desc[1,$(( desc_col - 1 ))]}…"
    fi
    local padded_desc="${(r:$desc_col:)disp_desc}"

    local body=" ${pointer} ${badge}  ${padded_cmd}  ${padded_desc}"
    if (( ${#body} > inner_w )); then
      body="${body[1,$inner_w]}"
    elif (( ${#body} < inner_w )); then
      body="${(r:$inner_w:)body}"
    fi

    # Visual scrollbar on right border when list exceeds viewport
    local r_border="│"
    if (( total > vis )); then
      local -i thumb_row=$(( _CUE_MENU_SCROLL * (vis - 1) / (total - vis) ))
      (( row == thumb_row )) && r_border="┃"
    fi

    local line=$'\n'"│${body}${r_border}"
    local -i l_start=$(( offset + 1 ))
    local -i l_end=$(( offset + ${#line} ))

    if (( is_sel )); then
      hl+=("${l_start} $(( l_start + 1 )) fg=240")
      hl+=("$(( l_start + 1 )) $(( l_end - 1 )) bg=24,fg=255,bold")
      hl+=("$(( l_end - 1 )) ${l_end} fg=45")
    else
      hl+=("${l_start} $(( l_start + 1 )) fg=240")
      hl+=("$(( l_end - 1 )) ${l_end} fg=240")
      hl+=("$(( l_start + 4 )) $(( l_start + 12 )) ${badge_fg}")
      hl+=("$(( l_start + 14 )) $(( l_start + 14 + cmd_col )) fg=252,bold")
      hl+=("$(( l_start + 14 + cmd_col + 2 )) $(( l_end - 1 )) fg=244")
    fi

    out+="$line"
    offset+=${#line}
  done

  # Footer bar
  local ftr_text=" ↑/↓ select · Tab/→ fill · Enter run · Esc close "
  if (( _CUE_AI_ENABLED )); then
    ftr_text=" ↑/↓ select · Tab/→ fill · Enter run · ^Space AI · Esc close "
  fi
  if (( ${#ftr_text} > inner_w - 2 )); then
    ftr_text=" ↑/↓ select · Tab fill · Enter run · Esc close "
  fi
  if (( ${#ftr_text} > inner_w - 2 )); then
    ftr_text="${ftr_text[1,$(( inner_w - 2 ))]}"
  fi
  local -i f_dashes=$(( inner_w - 1 - ${#ftr_text} ))
  (( f_dashes < 1 )) && f_dashes=1
  local f_line=$'\n'"╰─${ftr_text}${(l:$f_dashes::─:)}╯"
  hl+=("$(( offset + 1 )) $(( offset + ${#f_line} )) fg=240")
  out+="$f_line"

  POSTDISPLAY="$out"
  _CUE_LAST_POSTDISPLAY="$out"
  _CUE_LAST_BOX_HL=("${hl[@]}")
  region_highlight=("${buf_hl[@]}" "${hl[@]}")
}

# --- ZLE Widgets ---

function _cue_on_line_init() {
  if (( _CUE_AUTO_SHOW_FIX )) && [[ -n "${_CUE_PROJ_CMDS[1]}" ]]; then
    _CUE_AUTO_SHOW_FIX=0
    _CUE_MENU_OPEN=0
    _CUE_MENU_INDEX=-1
    _CUE_GHOST_CMD="${_CUE_PROJ_CMDS[1]}"
    _CUE_GHOST_HINT="  [Tab: fix · ↓: options]"
    _cue_render_dropdown
  elif (( _CUE_AUTO_SHOW_RETRY )) && [[ -n "$_CUE_RETRY_CMD" ]]; then
    _CUE_AUTO_SHOW_RETRY=0
    _CUE_MENU_OPEN=0
    _CUE_MENU_INDEX=-1
    _CUE_GHOST_CMD="$_CUE_RETRY_CMD"
    _CUE_GHOST_HINT="  [Tab: retry command]"
    _cue_render_dropdown
  fi
}

function _cue_self_insert() {
  _CUE_GHOST_HINT=""
  _CUE_AUTO_SHOW_FIX=0
  _CUE_AUTO_SHOW_RETRY=0
  zle .self-insert
  _CUE_MODE="smart"
  _CUE_MENU_INDEX=-1
  _CUE_MENU_OPEN=1
  _cue_refresh_suggestions
  _cue_render_dropdown
  if (( _CUE_AI_ENABLED )) && (( ${#BUFFER} >= 2 )); then
    _cue_schedule_ai_suggest "$BUFFER"
  fi
}

function _cue_backward_delete() {
  zle .backward-delete-char
  if [[ -z "$BUFFER" ]]; then
    _cue_clear_ui
    return 0
  fi
  _CUE_MODE="smart"
  _CUE_MENU_INDEX=-1
  _CUE_MENU_OPEN=1
  _cue_refresh_suggestions
  _cue_render_dropdown
}

function _cue_arrow_down() {
  _CUE_GHOST_HINT=""
  if (( ! _CUE_MENU_OPEN )); then
    _CUE_MODE="smart"
    _CUE_MENU_OPEN=1
    _CUE_MENU_INDEX=0
    _cue_refresh_suggestions
    (( ${#_CUE_ITEMS_CMD} == 0 )) && _CUE_MENU_OPEN=0
    _cue_render_dropdown
    return 0
  fi
  local -i total=${#_CUE_ITEMS_CMD}
  if (( total > 0 )); then
    if (( _CUE_MENU_INDEX < total - 1 )); then
      (( _CUE_MENU_INDEX++ ))
    else
      _CUE_MENU_INDEX=0
    fi
  fi
  _cue_refresh_suggestions
  _cue_render_dropdown
}

function _cue_arrow_up() {
  _CUE_GHOST_HINT=""
  if (( ! _CUE_MENU_OPEN )); then
    _CUE_MODE="history"
    _CUE_MENU_OPEN=1
    _CUE_MENU_INDEX=0
    _cue_refresh_suggestions
    (( ${#_CUE_ITEMS_CMD} == 0 )) && _CUE_MENU_OPEN=0
    _cue_render_dropdown
    return 0
  fi
  local -i total=${#_CUE_ITEMS_CMD}
  if (( total > 0 )); then
    if (( _CUE_MENU_INDEX > 0 )); then
      (( _CUE_MENU_INDEX-- ))
    else
      _CUE_MENU_INDEX=$(( total - 1 ))
    fi
  fi
  _cue_refresh_suggestions
  _cue_render_dropdown
}

function _cue_arrow_right() {
  if (( CURSOR == ${#BUFFER} )); then
    if (( _CUE_MENU_OPEN && _CUE_MENU_INDEX >= 0 )); then
      BUFFER="${_CUE_ITEMS_CMD[$(( _CUE_MENU_INDEX + 1 ))]}"
      CURSOR=${#BUFFER}
      _cue_clear_ui
      return 0
    elif [[ -n "$_CUE_GHOST_CMD" ]]; then
      BUFFER="$_CUE_GHOST_CMD"
      CURSOR=${#BUFFER}
      _cue_clear_ui
      return 0
    fi
  fi
  zle .forward-char
}

function _cue_tab_accept() {
  if (( _CUE_IN_TAB_ACCEPT )); then
    _cue_clear_ui
    zle .expand-or-complete
    return $?
  fi
  _CUE_IN_TAB_ACCEPT=1

  if (( _CUE_MENU_OPEN && _CUE_MENU_INDEX >= 0 )); then
    BUFFER="${_CUE_ITEMS_CMD[$(( _CUE_MENU_INDEX + 1 ))]}"
    CURSOR=${#BUFFER}
    _cue_clear_ui
    zle -R
    _CUE_IN_TAB_ACCEPT=0
    return 0
  elif [[ -n "$_CUE_GHOST_CMD" ]] && { [[ -n "$_CUE_GHOST_HINT" ]] || [[ -z "$BUFFER" ]]; }; then
    # Accept inline fix/retry ghost prompt cleanly on Tab; preserve native path completion while typing
    BUFFER="$_CUE_GHOST_CMD"
    CURSOR=${#BUFFER}
    _cue_clear_ui
    zle -R
    _CUE_IN_TAB_ACCEPT=0
    return 0
  fi

  _cue_clear_ui
  local orig_w="${_CUE_ORIG_TAB_WIDGET:-}"
  if [[ -n "$orig_w" && "$orig_w" != "_cue_tab_accept" && "$orig_w" != *"expand-or-complete"* ]] && (( $+widgets[$orig_w] )); then
    zle "$orig_w"
  else
    zle .expand-or-complete
  fi
  local rc=$?
  _CUE_IN_TAB_ACCEPT=0
  return $rc
}

function _cue_accept_line() {
  if (( _CUE_MENU_OPEN && _CUE_MENU_INDEX >= 0 )); then
    BUFFER="${_CUE_ITEMS_CMD[$(( _CUE_MENU_INDEX + 1 ))]}"
    CURSOR=${#BUFFER}
  fi
  _cue_clear_ui
  zle .accept-line
}

function _cue_dismiss_menu() {
  _cue_clear_ui
  zle -R
}

function _cue_trigger_ai_now() {
  _CUE_MODE="smart"
  _CUE_MENU_OPEN=1
  _CUE_MENU_INDEX=0
  _cue_refresh_suggestions
  _cue_render_dropdown
  if (( _CUE_AI_ENABLED )); then
    _cue_schedule_ai_suggest "$BUFFER"
  fi
}

function _cue_async_redraw() {
  if (( _CUE_MENU_OPEN )) || [[ -n "$BUFFER" ]]; then
    _cue_refresh_suggestions
    _cue_render_dropdown
    zle -R
  elif (( _CUE_LAST_ERR_CODE != 0 )) && [[ "${_CUE_PROJ_SRCS[1]}" == "fix" && -n "${_CUE_PROJ_CMDS[1]}" ]]; then
    _CUE_GHOST_CMD="${_CUE_PROJ_CMDS[1]}"
    _CUE_GHOST_HINT="  [Tab: accept fix · ↓: options]"
    _cue_render_dropdown
    zle -R
  fi
}

function _cue_post_redraw() {
  # Re-apply cue dropdown POSTDISPLAY & highlights if another redraw hook (e.g. zsh-syntax-highlighting) cleared them
  if [[ -n "$_CUE_LAST_POSTDISPLAY" ]]; then
    POSTDISPLAY="$_CUE_LAST_POSTDISPLAY"
    local -i b_len=${#BUFFER}
    local -a buf_hl=()
    local h_entry
    for h_entry in "${region_highlight[@]}"; do
      local -i h_start="${h_entry%% *}"
      (( h_start < b_len )) && buf_hl+=("$h_entry")
    done
    region_highlight=("${buf_hl[@]}" "${_CUE_LAST_BOX_HL[@]}")
  fi
}

function _cue_register_widgets_and_keys() {
  if (( $+functions[_zsh_autosuggest_disable] )); then
    _zsh_autosuggest_disable 2>/dev/null
  fi
  typeset -g ZSH_AUTOSUGGEST_MANUAL_REBIND=1

  zle -N zle-line-init _cue_on_line_init
  zle -N _cue_post_redraw
  autoload -Uz add-zle-hook-widget 2>/dev/null
  if (( $+functions[add-zle-hook-widget] )); then
    add-zle-hook-widget -d line-pre-redraw _cue_post_redraw 2>/dev/null
    add-zle-hook-widget line-pre-redraw _cue_post_redraw 2>/dev/null
  fi
  zle -N self-insert _cue_self_insert
  zle -N backward-delete-char _cue_backward_delete

  # Capture pre-existing Tab widget (e.g. fzf-completion) once without overwriting expand-or-complete
  if [[ -z "${_CUE_ORIG_TAB_WIDGET:-}" ]]; then
    local tab_bind
    tab_bind="$(bindkey -M main '^I' 2>/dev/null || true)"
    local prev_tab="${tab_bind##* }"
    if [[ -n "$prev_tab" && "$prev_tab" != "_cue_tab_accept" && "$prev_tab" != *"_zsh_highlight_widget"* ]]; then
      typeset -g _CUE_ORIG_TAB_WIDGET="$prev_tab"
    else
      typeset -g _CUE_ORIG_TAB_WIDGET="expand-or-complete"
    fi
  fi
  # Clean up any legacy self-referential alias from earlier sessions
  if [[ "${widgets[expand-or-complete]}" == "user:_cue_tab_accept" ]]; then
    zle -A .expand-or-complete expand-or-complete 2>/dev/null || true
  fi
  zle -N _cue_arrow_down
  zle -N _cue_arrow_up
  zle -N _cue_arrow_right
  zle -N _cue_tab_accept
  zle -N _cue_accept_line
  zle -N _cue_dismiss_menu
  zle -N _cue_trigger_ai_now
  zle -N _cue_async_redraw

  local kmap
  for kmap in main emacs viins; do
    bindkey -M "$kmap" '^[[A' _cue_arrow_up 2>/dev/null
    bindkey -M "$kmap" '^[OA' _cue_arrow_up 2>/dev/null
    bindkey -M "$kmap" '^[[B' _cue_arrow_down 2>/dev/null
    bindkey -M "$kmap" '^[OB' _cue_arrow_down 2>/dev/null
    bindkey -M "$kmap" '^[[C' _cue_arrow_right 2>/dev/null
    bindkey -M "$kmap" '^[OC' _cue_arrow_right 2>/dev/null
    [[ -n "${terminfo[kcuu1]}" ]] && bindkey -M "$kmap" -- "${terminfo[kcuu1]}" _cue_arrow_up 2>/dev/null
    [[ -n "${terminfo[kcud1]}" ]] && bindkey -M "$kmap" -- "${terminfo[kcud1]}" _cue_arrow_down 2>/dev/null
    [[ -n "${terminfo[kcuf1]}" ]] && bindkey -M "$kmap" -- "${terminfo[kcuf1]}" _cue_arrow_right 2>/dev/null

    bindkey -M "$kmap" '^I' _cue_tab_accept 2>/dev/null
    bindkey -M "$kmap" '^M' _cue_accept_line 2>/dev/null
    bindkey -M "$kmap" '^J' _cue_accept_line 2>/dev/null
    bindkey -M "$kmap" '^G' _cue_dismiss_menu 2>/dev/null
    bindkey -M "$kmap" '^[' _cue_dismiss_menu 2>/dev/null
    bindkey -M "$kmap" '^ ' _cue_trigger_ai_now 2>/dev/null
    bindkey -M "$kmap" '^X^A' _cue_trigger_ai_now 2>/dev/null
  done
}
