"""Modern interactive ANSI terminal UI components, card layouts, tables, and key handling."""

from __future__ import annotations

import getpass
import os
import re
import shutil
import sys
import time
from typing import Any, Callable

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _enable_windows_ansi() -> None:
    """Enable VT100 ANSI escape processing on Windows consoles when available."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    _enable_windows_ansi()
    return sys.stdout.isatty()


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def truncate_plain(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if len(text) <= max_width:
        return text
    if max_width == 1:
        return "…"
    return text[: max_width - 1] + "…"


def pad_visible(text: str, width: int) -> str:
    """Pad an ANSI-styled string on the right to exact visible width."""
    vlen = visible_len(text)
    if vlen >= width:
        return text
    return text + (" " * (width - vlen))


def term_size() -> tuple[int, int]:
    sz = shutil.get_terminal_size((84, 26))
    return max(54, sz.columns), max(16, sz.lines)


class Theme:
    """Modern terminal color palette with automatic NO_COLOR fallback."""

    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = supports_color() if enabled is None else enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def cyan(self, text: str) -> str:
        return self._wrap("38;5;45" if self._true_or_256() else "36", text)

    def teal(self, text: str) -> str:
        return self._wrap("38;5;43" if self._true_or_256() else "36", text)

    def green(self, text: str) -> str:
        return self._wrap("38;5;78" if self._true_or_256() else "32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("38;5;221" if self._true_or_256() else "33", text)

    def red(self, text: str) -> str:
        return self._wrap("38;5;203" if self._true_or_256() else "31", text)

    def magenta(self, text: str) -> str:
        return self._wrap("38;5;177" if self._true_or_256() else "35", text)

    def slate(self, text: str) -> str:
        return self._wrap("38;5;244" if self._true_or_256() else "90", text)

    def border(self, text: str) -> str:
        return self._wrap("38;5;240" if self._true_or_256() else "90", text)

    def pill_selected(self, text: str) -> str:
        return self._wrap("48;5;24;38;5;255;1" if self._true_or_256() else "7;1", text)

    def _true_or_256(self) -> bool:
        term = os.environ.get("TERM", "")
        return "256color" in term or os.environ.get("COLORTERM", "") in ("truecolor", "24bit")


def format_bytes(num_bytes: int) -> str:
    """Format byte counts cleanly (e.g. 14.2 KB, 1.8 MB)."""
    val = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0 or unit == "GB":
            return f"{int(val)} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{num_bytes} B"


def format_age(age_days: float) -> str:
    """Human-readable age string from age in days."""
    secs = age_days * 86400.0
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(age_days)}d ago"


def draw_card(
    theme: Theme,
    header_left: str,
    header_right: str = "",
    lines: list[str] | None = None,
    footer: str = "",
    width: int | None = None,
) -> list[str]:
    """Build a crisp rounded box card with pixel-aligned borders."""
    cols, _ = term_size()
    box_w = min(width or 84, max(54, cols - 2))
    inner_w = box_w - 2
    out: list[str] = []

    top_title = f" {header_left} "
    right_chip = f" {header_right} " if header_right else ""
    dash_count = max(2, inner_w - visible_len(top_title) - visible_len(right_chip))
    out.append(
        theme.border("╭")
        + top_title
        + theme.border("─" * dash_count)
        + right_chip
        + theme.border("╮")
    )

    for row in lines or []:
        if row == "---":
            out.append(theme.border("├" + ("─" * inner_w) + "┤"))
            continue
        content = pad_visible(row, inner_w)
        out.append(theme.border("│") + content + theme.border("│"))

    if footer:
        ftr = f" {footer} "
        fdash = max(2, inner_w - visible_len(ftr))
        out.append(theme.border("╰") + theme.dim(ftr) + theme.border("─" * fdash + "╯"))
    else:
        out.append(theme.border("╰" + ("─" * inner_w) + "╯"))
    return out


def print_banner(theme: Theme, subtitle: str = "") -> None:
    """Render a sleek modern terminal header card with closed, aligned borders."""
    title = theme.bold(theme.cyan("✦ cue")) + theme.slate(" · ") + theme.bold("Smart AI Terminal Assistant")
    sub = "  " + (theme.dim(subtitle) if subtitle else theme.dim("Project-aware completions · Multi-provider AI"))
    card = draw_card(theme, title, "", [sub], width=76)
    print("")
    for line in card:
        print("  " + line)


class _CbreakMode:
    """Context manager keeping POSIX tty in cbreak mode without echo across a TUI screen."""

    def __init__(self) -> None:
        self.fd: int | None = None
        self.old: Any = None

    def __enter__(self) -> "_CbreakMode":
        if os.name == "nt":
            return self
        import termios

        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        mode = termios.tcgetattr(self.fd)
        mode[0] &= ~(termios.IXON | termios.ICRNL)
        mode[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, mode)
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.fd is not None and self.old is not None:
            import termios

            termios.tcsetattr(self.fd, termios.TCSANOW, self.old)


def read_key(already_raw: bool = False) -> str:
    """Read a single keypress cross-platform (macOS/Linux unbuffered fd or Windows msvcrt)."""
    if os.name == "nt":
        import msvcrt

        try:
            ch = msvcrt.getwch()  # type: ignore[attr-defined]
        except KeyboardInterrupt:
            return "CTRL_C"
        if ch in ("\x00", "\xe0"):
            ext = msvcrt.getwch()  # type: ignore[attr-defined]
            return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(ext, "")
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == "\x1b":
            return "ESC"
        if ch == " ":
            return "SPACE"
        if ch in ("\x08", "\x7f"):
            return "BACKSPACE"
        if ch == "\x03":
            return "CTRL_C"
        return ch

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = None if already_raw else termios.tcgetattr(fd)
    try:
        if not already_raw:
            tty.setcbreak(fd)
        raw_b = os.read(fd, 1)
        if not raw_b:
            return ""
        if raw_b == b"\x1b":
            r, _, _ = select.select([fd], [], [], 0.04)
            if r:
                seq = os.read(fd, 1)
                if seq in (b"[", b"O"):
                    r2, _, _ = select.select([fd], [], [], 0.04)
                    code = os.read(fd, 1) if r2 else b""
                    return {b"A": "UP", b"B": "DOWN", b"C": "RIGHT", b"D": "LEFT"}.get(code, "ESC")
            return "ESC"
        if raw_b in (b"\r", b"\n"):
            return "ENTER"
        if raw_b == b" ":
            return "SPACE"
        if raw_b == b"\t":
            return "TAB"
        if raw_b in (b"\x7f", b"\x08"):
            return "BACKSPACE"
        if raw_b == b"\x03":
            return "CTRL_C"
        lead = raw_b[0]
        if lead >= 0xC0:
            extra = 1 if lead < 0xE0 else (2 if lead < 0xF0 else 3)
            raw_b += os.read(fd, extra)
        return raw_b.decode("utf-8", errors="replace")
    except KeyboardInterrupt:
        return "CTRL_C"
    finally:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSANOW, old)


def _clear_and_draw(lines: list[str], prev_count: int) -> int:
    if prev_count > 0:
        sys.stdout.write(f"\033[{prev_count}A\r\033[J")
    sys.stdout.write("\n".join("  " + ln for ln in lines) + "\n")
    sys.stdout.flush()
    return len(lines)


def tui_menu_loop(
    theme: Theme,
    title: str,
    subtitle_right: str,
    items_fn: Callable[[], list[dict[str, str]]],
    on_select: Callable[[dict[str, Any], int], str | None],
    footer: str = "↑/↓ move · Enter/Space select · Esc/q back",
    status_msg: str = "",
    width: int = 86,
) -> None:
    """Interactive arrow-key card menu rendered in place with live updates."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return

    idx = 0
    scroll = 0
    drawn = 0
    toast = status_msg
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    try:
        with _CbreakMode():
            while True:
                items = items_fn()
                if not items:
                    break
                idx = max(0, min(idx, len(items) - 1))
                cols, rows_h = term_size()
                box_w = min(width, max(56, cols - 4))
                inner_w = box_w - 2
                vis = max(5, min(len(items), int(rows_h * 0.65) - 5))

                if idx < scroll:
                    scroll = idx
                elif idx >= scroll + vis:
                    scroll = idx - vis + 1

                body: list[str] = []
                if toast:
                    body.append(f"  {theme.green('✓')} {truncate_plain(toast, inner_w - 6)}")
                    body.append("---")

                label_w = max(18, min(28, inner_w // 3))
                val_w = max(16, min(28, inner_w // 3))
                hint_w = max(8, inner_w - label_w - val_w - 8)

                for row_i in range(scroll, min(len(items), scroll + vis)):
                    it = items[row_i]
                    is_sel = row_i == idx
                    ptr = "❯" if is_sel else " "
                    lbl = truncate_plain(str(it.get("label", "")), label_w).ljust(label_w)
                    val_raw = truncate_plain(str(it.get("value", "")), val_w).ljust(val_w)
                    hint_raw = truncate_plain(str(it.get("hint", "")), hint_w).ljust(hint_w)

                    if is_sel:
                        plain_row = f" {ptr} {lbl}  {val_raw}  {hint_raw} "
                        body.append(theme.pill_selected(pad_visible(plain_row, inner_w)))
                    else:
                        tone = str(it.get("tone", "cyan"))
                        styled_val = getattr(theme, tone, theme.cyan)(val_raw)
                        body.append(f" {ptr} {theme.bold(lbl)}  {styled_val}  {theme.dim(hint_raw)}")

                if len(items) > vis:
                    body.append(theme.dim(f"   Showing {scroll + 1}–{min(len(items), scroll + vis)} of {len(items)}"))

                hdr = f"{theme.bold(theme.cyan('✦ cue'))} {theme.slate('·')} {theme.bold(title)}"
                card = draw_card(theme, hdr, theme.dim(subtitle_right), body, footer=footer, width=box_w)
                drawn = _clear_and_draw(card, drawn)

                key = read_key(already_raw=True)
                toast = ""
                if key in ("ESC", "LEFT", "q", "CTRL_C"):
                    break
                elif key in ("UP", "k"):
                    idx = (idx - 1) % len(items)
                elif key in ("DOWN", "j"):
                    idx = (idx + 1) % len(items)
                elif key in ("ENTER", "SPACE", "RIGHT"):
                    sys.stdout.write(f"\033[{drawn}A\r\033[J\033[?25h")
                    sys.stdout.flush()
                    drawn = 0
                    res = on_select(items[idx], idx)
                    sys.stdout.write("\033[?25l")
                    sys.stdout.flush()
                    if res == "__EXIT__":
                        break
                    if res:
                        toast = res
    finally:
        if drawn > 0:
            sys.stdout.write(f"\033[{drawn}A\r\033[J")
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def tui_confirm_card(
    theme: Theme,
    title: str,
    message: str,
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
) -> bool:
    """Modern interactive arrow-key confirmation card (defaults safely to Cancel)."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False

    chosen = [False]

    def _items() -> list[dict[str, str]]:
        return [
            {"id": "cancel", "label": cancel_label, "value": "Safe", "hint": message, "tone": "slate"},
            {"id": "confirm", "label": confirm_label, "value": "Proceed", "hint": "Action cannot be undone", "tone": "red"},
        ]

    def _on_sel(item: dict[str, Any], _i: int) -> str | None:
        chosen[0] = item["id"] == "confirm"
        return "__EXIT__"

    tui_menu_loop(
        theme,
        title,
        "Confirm",
        _items,
        _on_sel,
        footer="↑/↓ choose · Enter confirm · Esc/← cancel",
        width=82,
    )
    return chosen[0]


def tui_input_card(
    theme: Theme,
    title: str,
    subtitle: str,
    prompt_label: str,
    masked: bool = True,
) -> str | None:
    """Modern card-styled text/secret input supporting Backspace, paste, Enter, and Esc."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None

    buf: list[str] = []
    drawn = 0
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    try:
        with _CbreakMode():
            while True:
                cols, _ = term_size()
                box_w = min(84, max(56, cols - 4))
                inner_w = box_w - 2
                raw_val = "".join(buf)
                disp_val = ("•" * len(raw_val)) if masked else raw_val
                disp_val = truncate_plain(disp_val, max(12, inner_w - 8))
                input_line = f"  {theme.cyan('❯')} {disp_val}{theme.cyan('▌')}"
                body = [
                    f"  {theme.dim(subtitle)}",
                    f"  {theme.bold(prompt_label)}",
                    "---",
                    input_line,
                ]
                hdr = f"{theme.bold(theme.cyan('✦ cue'))} {theme.slate('·')} {theme.bold(title)}"
                card = draw_card(
                    theme,
                    hdr,
                    theme.dim(f"{len(raw_val)} chars") if raw_val else "",
                    body,
                    footer="Type or paste · Enter save · Backspace delete · Esc cancel",
                    width=box_w,
                )
                drawn = _clear_and_draw(card, drawn)

                key = read_key(already_raw=True)
                if key in ("ESC", "CTRL_C"):
                    return None
                if key == "ENTER":
                    return raw_val.strip()
                if key == "BACKSPACE":
                    if buf:
                        buf.pop()
                elif key == "SPACE":
                    buf.append(" ")
                elif len(key) == 1 and ord(key) >= 32:
                    buf.append(key)
    finally:
        if drawn > 0:
            sys.stdout.write(f"\033[{drawn}A\r\033[J")
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def tui_confirm_card(
    theme: Theme,
    title: str,
    message: str,
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
) -> bool:
    """Modern interactive arrow-key confirmation card (defaults safely to Cancel)."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False

    chosen = [False]

    def _items() -> list[dict[str, str]]:
        return [
            {"id": "cancel", "label": cancel_label, "value": "Safe", "hint": message, "tone": "slate"},
            {"id": "confirm", "label": confirm_label, "value": "Proceed", "hint": "Action cannot be undone", "tone": "red"},
        ]

    def _on_sel(item: dict[str, Any], _i: int) -> str | None:
        chosen[0] = item["id"] == "confirm"
        return "__EXIT__"

    tui_menu_loop(
        theme,
        title,
        "Confirm",
        _items,
        _on_sel,
        footer="↑/↓ choose · Enter confirm · Esc/← cancel",
        width=82,
    )
    return chosen[0]


def tui_multi_select_projects(
    theme: Theme,
    title: str,
    entries: list[dict[str, Any]],
) -> list[int]:
    """Modern interactive multi-select TUI for removing stored projects."""
    if not entries or not sys.stdin.isatty() or not sys.stdout.isatty():
        return []

    selected: set[int] = set()
    idx = 0
    scroll = 0
    drawn = 0
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    try:
        with _CbreakMode():
            while True:
                cols, rows_h = term_size()
                box_w = min(92, max(58, cols - 4))
                inner_w = box_w - 2
                vis = max(5, min(len(entries), int(rows_h * 0.65) - 6))
                if idx < scroll:
                    scroll = idx
                elif idx >= scroll + vis:
                    scroll = idx - vis + 1

                name_w = 20
                stack_w = 18
                size_w = 8
                age_w = 8
                root_w = max(8, inner_w - 10 - name_w - stack_w - size_w - age_w - 10)

                body: list[str] = [
                    f"  {theme.dim('Space: toggle · m: mark missing · o: mark old (>30d) · Enter: delete marked')}",
                    "---",
                ]
                for r in range(scroll, min(len(entries), scroll + vis)):
                    p = entries[r]
                    is_cur = r == idx
                    chk = "✓" if r in selected else " "
                    ptr = "❯" if is_cur else " "
                    name = truncate_plain(str(p.get("name", "")), name_w).ljust(name_w)
                    stacks = truncate_plain(", ".join(p.get("stacks", [])[:2]) or "General", stack_w).ljust(stack_w)
                    sz = format_bytes(int(p.get("size_bytes", 0))).rjust(size_w)
                    age = format_age(float(p.get("age_days", 0.0))).rjust(age_w)
                    src_root = truncate_plain(str(p.get("src_root", ".cue")), root_w)
                    st_txt = "ok" if p.get("exists") else "missing"

                    if is_cur:
                        row_txt = f" {ptr} [{chk}] {name}  {stacks}  {sz}  {age}  {st_txt} ({src_root}) "
                        body.append(theme.pill_selected(pad_visible(row_txt, inner_w)))
                    else:
                        chk_styled = theme.red(f"[{chk}]") if r in selected else theme.slate(f"[{chk}]")
                        st_styled = theme.green(st_txt) if p.get("exists") else theme.yellow(st_txt)
                        body.append(
                            f" {ptr} {chk_styled} {theme.bold(name)}  {theme.cyan(stacks)}  {sz}  {theme.dim(age)}  {st_styled} {theme.dim(src_root)}"
                        )

                if len(entries) > vis:
                    body.append(theme.dim(f"   {scroll + 1}–{min(len(entries), scroll + vis)} of {len(entries)} · {len(selected)} selected"))

                hdr = f"{theme.bold(theme.cyan('✦ cue'))} {theme.slate('·')} {theme.bold(title)}"
                card = draw_card(
                    theme,
                    hdr,
                    theme.dim(f"{len(selected)} selected"),
                    body,
                    footer="↑/↓ move · Space toggle · Enter delete selected · Esc cancel",
                    width=box_w,
                )
                drawn = _clear_and_draw(card, drawn)

                key = read_key(already_raw=True)
                if key in ("ESC", "LEFT", "q", "CTRL_C"):
                    return []
                elif key in ("UP", "k"):
                    idx = (idx - 1) % len(entries)
                elif key in ("DOWN", "j"):
                    idx = (idx + 1) % len(entries)
                elif key == "SPACE":
                    if idx in selected:
                        selected.remove(idx)
                    else:
                        selected.add(idx)
                elif key == "m":
                    for i, p in enumerate(entries):
                        if not p.get("exists"):
                            selected.add(i)
                elif key == "o":
                    for i, p in enumerate(entries):
                        if float(p.get("age_days", 0.0)) >= 30.0:
                            selected.add(i)
                elif key == "a":
                    if len(selected) == len(entries):
                        selected.clear()
                    else:
                        selected = set(range(len(entries)))
                elif key == "ENTER":
                    return sorted(selected)
    finally:
        if drawn > 0:
            sys.stdout.write(f"\033[{drawn}A\r\033[J")
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def tui_confirm_card(
    theme: Theme,
    title: str,
    message: str,
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
) -> bool:
    """Modern interactive arrow-key confirmation card (defaults safely to Cancel)."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False

    chosen = [False]

    def _items() -> list[dict[str, str]]:
        return [
            {"id": "cancel", "label": cancel_label, "value": "Safe", "hint": message, "tone": "slate"},
            {"id": "confirm", "label": confirm_label, "value": "Proceed", "hint": "Action cannot be undone", "tone": "red"},
        ]

    def _on_sel(item: dict[str, Any], _i: int) -> str | None:
        chosen[0] = item["id"] == "confirm"
        return "__EXIT__"

    tui_menu_loop(
        theme,
        title,
        "Confirm",
        _items,
        _on_sel,
        footer="↑/↓ choose · Enter confirm · Esc/← cancel",
        width=82,
    )
    return chosen[0]


def tui_input_card(
    theme: Theme,
    title: str,
    subtitle: str,
    prompt_label: str,
    masked: bool = True,
) -> str | None:
    """Modern card-styled text/secret input supporting Backspace, paste, Enter, and Esc."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None

    buf: list[str] = []
    drawn = 0
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    try:
        with _CbreakMode():
            while True:
                cols, _ = term_size()
                box_w = min(84, max(56, cols - 4))
                inner_w = box_w - 2
                raw_val = "".join(buf)
                disp_val = ("•" * len(raw_val)) if masked else raw_val
                disp_val = truncate_plain(disp_val, max(12, inner_w - 8))
                input_line = f"  {theme.cyan('❯')} {disp_val}{theme.cyan('▌')}"
                body = [
                    f"  {theme.dim(subtitle)}",
                    f"  {theme.bold(prompt_label)}",
                    "---",
                    input_line,
                ]
                hdr = f"{theme.bold(theme.cyan('✦ cue'))} {theme.slate('·')} {theme.bold(title)}"
                card = draw_card(
                    theme,
                    hdr,
                    theme.dim(f"{len(raw_val)} chars") if raw_val else "",
                    body,
                    footer="Type or paste · Enter save · Backspace delete · Esc cancel",
                    width=box_w,
                )
                drawn = _clear_and_draw(card, drawn)

                key = read_key(already_raw=True)
                if key in ("ESC", "CTRL_C"):
                    return None
                if key == "ENTER":
                    return raw_val.strip()
                if key == "BACKSPACE":
                    if buf:
                        buf.pop()
                elif key == "SPACE":
                    buf.append(" ")
                elif len(key) == 1 and ord(key) >= 32:
                    buf.append(key)
    finally:
        if drawn > 0:
            sys.stdout.write(f"\033[{drawn}A\r\033[J")
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def tui_confirm_card(
    theme: Theme,
    title: str,
    message: str,
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
) -> bool:
    """Modern interactive arrow-key confirmation card (defaults safely to Cancel)."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False

    chosen = [False]

    def _items() -> list[dict[str, str]]:
        return [
            {"id": "cancel", "label": cancel_label, "value": "Safe", "hint": message, "tone": "slate"},
            {"id": "confirm", "label": confirm_label, "value": "Proceed", "hint": "Action cannot be undone", "tone": "red"},
        ]

    def _on_sel(item: dict[str, Any], _i: int) -> str | None:
        chosen[0] = item["id"] == "confirm"
        return "__EXIT__"

    tui_menu_loop(
        theme,
        title,
        "Confirm",
        _items,
        _on_sel,
        footer="↑/↓ choose · Enter confirm · Esc/← cancel",
        width=82,
    )
    return chosen[0]


def prompt_api_key(
    theme: Theme,
    provider_name: str = "OpenRouter",
    key_url: str = "https://openrouter.ai/keys",
    key_hint: str = "",
) -> str:
    """Prompt for a provider API key cleanly with masked input and fallback."""
    print("")
    print(theme.bold(f"  Connect {provider_name}"))
    if key_url:
        print(theme.dim(f"  Get your API key at: {key_url}"))
    if key_hint:
        print(theme.dim(f"  Expected format: {key_hint}"))
    print(theme.dim("  Your key is stored locally in config.json (0600 permissions)."))
    print(theme.dim("  Press Enter without typing to skip for now."))
    print("")
    try:
        key = getpass.getpass(theme.cyan(f"  ❯ {provider_name} API key (hidden): "))
    except (EOFError, KeyboardInterrupt):
        print("")
        return ""
    return key.strip()


def prompt_yes_no(theme: Theme, question: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"  {theme.cyan('❯')} {question} {theme.dim(hint)}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("")
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def render_projects_table(theme: Theme, projects: list[dict[str, Any]]) -> None:
    """Print a modern card table of stored project contexts."""
    if not projects:
        print(theme.dim("  No project context files stored yet."))
        return

    body: list[str] = [
        f"  {theme.bold('#'.ljust(3))} {theme.bold('PROJECT'.ljust(20))} {theme.bold('STACK'.ljust(20))} {theme.bold('SIZE'.rjust(9))} {theme.bold('USED'.rjust(9))} {theme.bold('STATUS')}",
        "---",
    ]
    for idx, p in enumerate(projects, start=1):
        name = truncate_plain(str(p.get("name", "")), 20).ljust(20)
        stacks = truncate_plain(", ".join(p.get("stacks", [])[:2]) or "General", 20).ljust(20)
        size_str = format_bytes(int(p.get("size_bytes", 0))).rjust(9)
        age_str = format_age(float(p.get("age_days", 0.0))).rjust(9)
        exists = bool(p.get("exists", False))
        badge = theme.green("● active") if exists else theme.yellow("○ legacy")
        body.append(f"  {theme.slate(str(idx).ljust(3))} {theme.bold(name)} {theme.cyan(stacks)} {size_str} {theme.dim(age_str)} {badge}")
        body.append(f"      {theme.dim(truncate_plain(str(p.get('project_path', '')), 72))}")

    hdr = f"{theme.bold(theme.cyan('✦ cue'))} {theme.slate('·')} {theme.bold('Stored Projects')}"
    print("")
    for line in draw_card(theme, hdr, theme.dim(f"{len(projects)} total"), body, width=88):
        print("  " + line)
    print("")


def render_history_table(theme: Theme, project_name: str, recent_commands: list[dict[str, Any]], limit: int = 20) -> None:
    """Render project history in an interactive modern TUI (or static card if non-TTY)."""
    if not recent_commands:
        print_banner(theme, f"Project history · {project_name}")
        print(theme.dim("  No commands recorded for this project yet.\n"))
        return

    now = int(time.time())
    items = list(reversed(recent_commands[-max(limit, 40):]))

    if sys.stdin.isatty() and sys.stdout.isatty():
        def _items() -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for entry in items:
                code = int(entry.get("exit_code", 0))
                st = "✓" if code == 0 else f"✗{code}"
                cnt = f"{int(entry.get('count', 1))}×"
                age = format_age(max(0.0, (now - int(entry.get("ts", now))) / 86400.0))
                rows.append(
                    {
                        "label": str(entry.get("cmd", "")),
                        "value": f"{st} · {cnt}",
                        "hint": age,
                        "tone": "green" if code == 0 else "red",
                        "cmd": str(entry.get("cmd", "")),
                    }
                )
            return rows

        def _on_sel(item: dict[str, Any], _i: int) -> str | None:
            return f"Selected: {item['cmd']}"

        tui_menu_loop(
            theme,
            f"History · {project_name}",
            f"{len(items)} commands",
            _items,
            _on_sel,
            footer="↑/↓ browse history · Enter inspect · Esc/q close",
            width=90,
        )
        return

    # Static card fallback for non-TTY/tests
    body: list[str] = [
        f"  {theme.bold('#'.ljust(3))} {theme.bold('ST'.ljust(4))} {theme.bold('RUNS'.rjust(4))} {theme.bold('WHEN'.rjust(8))}  {theme.bold('COMMAND')}",
        "---",
    ]
    for idx, entry in enumerate(items[:limit], start=1):
        code = int(entry.get("exit_code", 0))
        st = theme.green("✓   ") if code == 0 else theme.red(f"✗{code}".ljust(4))
        count = f"{int(entry.get('count', 1))}×".rjust(4)
        age = format_age(max(0.0, (now - int(entry.get("ts", now))) / 86400.0)).rjust(8)
        cmd = truncate_plain(str(entry.get("cmd", "")), 52)
        body.append(f"  {theme.slate(str(idx).ljust(3))} {st} {theme.dim(count)} {theme.cyan(age)}  {theme.bold(cmd)}")

    hdr = f"{theme.bold(theme.cyan('✦ cue'))} {theme.slate('·')} {theme.bold(f'History · {project_name}')}"
    print("")
    for line in draw_card(theme, hdr, theme.dim(f"{len(items[:limit])} shown"), body, width=88):
        print("  " + line)
    print("")
