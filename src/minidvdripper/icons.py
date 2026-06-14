"""Glyphs and 'chip' markup for the TUI — a Nerd Font set with a unicode fallback.

Two presentation layers:
  * level icons (ok / warn / fail / info / dim) drawn on every log line;
  * field chips (device / media / status / size / label) for the disc summary,
    rendered as background-coloured boxes so the header reads like labelled tags
    instead of one run-on line.

Nerd Font glyphs (the \\uF0xx Private-Use codepoints below) only render in a
Nerd-Font-patched terminal font. When the user has none they show as tofu boxes,
so `nerd=False` falls back to a unicode set that renders in any modern terminal
(and field chips simply drop the glyph, keeping the coloured box). The choice is
the `nerd_icons` config toggle.
"""
from __future__ import annotations

# level -> (nerd glyph, unicode glyph, colour)
_LEVEL = {
    "ok":   ("", "✓", "green"),          # nf-fa-check        / ✓
    "warn": ("", "⚠", "dark_orange"),    # nf-fa-warning      / ⚠
    "fail": ("", "✗", "red"),            # nf-fa-times        / ✗
    "info": ("", "•", "grey70"),         # nf-fa-info_circle  / •
    "dim":  ("", "·", "grey50"),         # nf-fa-circle       / ·
}

# step state -> (nerd glyph, unicode glyph, colour)
_STEP = {
    "idle": ("", "○", "grey50"),         # nf-fa-circle_o       / ○
    "run":  ("", "◐", "yellow"),         # nf-fa-adjust         / ◐
    "done": ("", "●", "green"),          # nf-fa-check_circle   / ●
    "warn": ("", "▲", "dark_orange"),    # nf-fa-warning        / ▲
    "fail": ("", "✗", "red"),            # nf-fa-times_circle   / ✗
    "skip": ("", "·", "grey50"),         # nf-fa-minus_circle   / ·
}

# field name -> (nerd glyph, unicode glyph). Unicode '' = no glyph (clean chip).
_FIELD = {
    "device": ("", ""),     # nf-fa-hdd_o        (drive path)
    "media":  ("", ""),     # nf-fa-compact_disc (DVD type)
    "ok":     ("", ""),     # nf-fa-check_circle (finalized)
    "warn":   ("", ""),     # nf-fa-warning      (unfinalized)
    "size":   ("", ""),     # nf-fa-database     (size)
    "label":  ("", ""),     # nf-fa-tag          (volume label)
    "date":   ("", ""),     # nf-fa-calendar     (date)
}


def level_icon(level: str, nerd: bool) -> tuple[str, str]:
    """(glyph, colour) for a log level."""
    g_nerd, g_uni, color = _LEVEL.get(level, _LEVEL["info"])
    return (g_nerd if nerd else g_uni), color


def step_icon(state: str, nerd: bool) -> tuple[str, str]:
    """(glyph, colour) for a pipeline-step state."""
    g_nerd, g_uni, color = _STEP.get(state, _STEP["idle"])
    return (g_nerd if nerd else g_uni), color


def field_icon(name: str, nerd: bool) -> str:
    g_nerd, g_uni = _FIELD.get(name, ("", ""))
    return g_nerd if nerd else g_uni


def chip(text: str, fg: str, bg: str, icon: str = "") -> str:
    """A background-coloured 'chip': [fg on bg] icon text [/]."""
    inner = f"{icon} {text}".strip() if icon else text
    return f"[{fg} on {bg}] {inner} [/]"


def chip_plain(text: str, icon: str = "") -> str:
    """Plain-text form of a chip for the copy buffer: [icon text]."""
    inner = f"{icon} {text}".strip() if icon else text
    return f"[{inner}]"
