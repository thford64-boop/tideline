"""Colours and CSS. The palette is Tideline's own: slate blues with a lagoon
accent and a coral playhead."""

from gi.repository import Gdk, Gtk

BG0 = "#0e1115"        # window background, gaps between panels
BG1 = "#161a20"        # panel background
BG2 = "#1d222a"        # headers, track headers
BG3 = "#272e38"        # controls, hover
BG4 = "#333c49"
LINE = "#090b0e"
TEXT = "#d6dbe3"
TEXT_DIM = "#8b94a2"
TEXT_FAINT = "#59616d"
ACCENT = "#2fc6b5"
ACCENT_DIM = "#1b7a71"
PLAYHEAD = "#ff7a59"
SELECT = "#f3f6fa"
WARN = "#e5a93b"
DANGER = "#e0564b"
MARKER_COLORS = ["#2fc6b5", "#ff7a59", "#e5a93b", "#7ecb5f", "#5aa9e6", "#b07be0", "#e06c9f", "#9aa5b4"]


def rgb(hex_color, alpha=1.0):
    """'#rrggbb[aa]' -> (r, g, b, a) floats for cairo."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    a = int(h[6:8], 16) / 255.0 if len(h) >= 8 else 1.0
    return r, g, b, a * alpha


def shade(hex_color, factor):
    """Lighten (>1) or darken (<1) a colour; returns (r, g, b)."""
    r, g, b, _ = rgb(hex_color)
    if factor >= 1:
        f = factor - 1
        return r + (1 - r) * f, g + (1 - g) * f, b + (1 - b) * f
    return r * factor, g * factor, b * factor


CSS = f"""
@define-color accent_color {ACCENT};
@define-color accent_bg_color {ACCENT_DIM};
@define-color window_bg_color {BG0};
@define-color view_bg_color {BG1};
@define-color headerbar_bg_color {BG0};
@define-color popover_bg_color {BG2};
@define-color dialog_bg_color {BG1};
@define-color card_bg_color {BG2};

window, .background {{ background-color: {BG0}; color: {TEXT}; font-size: 12px; }}
headerbar {{ background: {BG0}; min-height: 30px; padding: 0 4px; border-bottom: 1px solid {LINE}; box-shadow: none; }}
headerbar .title {{ font-weight: 600; color: {TEXT_DIM}; font-size: 12px; }}
headerbar windowcontrols button {{ min-height: 20px; min-width: 20px; }}
menubar, popovermenubar {{ background: transparent; }}
popovermenubar > item {{ padding: 4px 9px; border-radius: 4px; color: {TEXT}; min-height: 0; }}
popovermenubar > item:hover, popovermenubar > item:selected {{ background: {BG3}; }}
popover > contents {{ background: {BG2}; border: 1px solid {LINE}; border-radius: 6px; padding: 4px; }}
popover modelbutton {{ padding: 4px 10px; min-height: 22px; border-radius: 4px; }}
popover modelbutton:hover {{ background: {ACCENT_DIM}; }}
popover modelbutton accelerator {{ color: {TEXT_DIM}; margin-left: 24px; }}
popover separator {{ margin: 3px 0; background: {BG4}; }}

.panel-group {{ background: {BG1}; border: 1px solid {LINE}; border-radius: 5px; }}
.panel-group.focused {{ border-color: {ACCENT_DIM}; }}
.panel-tabs {{ background: {BG2}; border-bottom: 1px solid {LINE}; border-radius: 5px 5px 0 0; min-height: 26px; padding: 0 3px; }}
.panel-tab {{ background: none; border: none; box-shadow: none; border-radius: 0; padding: 3px 10px; min-height: 0;
              color: {TEXT_DIM}; font-weight: 500; border-bottom: 2px solid transparent; }}
.panel-tab:hover {{ color: {TEXT}; }}
.panel-tab:checked {{ color: {TEXT}; border-bottom-color: {ACCENT}; background: none; }}
.panel-body {{ background: {BG1}; border-radius: 0 0 5px 5px; }}

button {{ border-radius: 4px; min-height: 22px; padding: 2px 8px; }}
button.flat, .tool-button, .icon-button {{ background: none; border: none; box-shadow: none; }}
.icon-button {{ padding: 3px; min-height: 20px; min-width: 20px; color: {TEXT_DIM}; border-radius: 4px; }}
.icon-button:hover {{ background: {BG3}; color: {TEXT}; }}
.icon-button:checked {{ color: {ACCENT}; background: {BG3}; }}
.icon-button:disabled {{ color: {TEXT_FAINT}; }}
.tool-strip {{ background: {BG1}; border: 1px solid {LINE}; border-radius: 5px; padding: 4px 3px; }}
.tool-button {{ padding: 5px; min-height: 22px; min-width: 22px; color: {TEXT_DIM}; border-radius: 5px; }}
.tool-button:hover {{ background: {BG3}; color: {TEXT}; }}
.tool-button:checked {{ background: {ACCENT_DIM}; color: #ffffff; }}
.transport {{ padding: 2px 0 5px 0; }}
.transport .icon-button {{ padding: 4px 5px; }}
.transport .play {{ color: {TEXT}; }}

.timecode {{ font-family: monospace; font-size: 13px; font-weight: 600; color: {ACCENT}; padding: 0 4px; }}
.timecode.dim {{ color: {TEXT_DIM}; font-weight: 500; }}
.timecode-entry {{ font-family: monospace; font-size: 13px; min-height: 20px; padding: 0 4px; }}
.hot-number {{ color: {ACCENT}; font-feature-settings: "tnum"; padding: 1px 3px; border-radius: 3px; }}
.hot-number:hover {{ background: {BG3}; }}
.hot-entry {{ min-height: 20px; padding: 0 4px; }}
.dim {{ color: {TEXT_DIM}; }}
.faint {{ color: {TEXT_FAINT}; }}
.hint {{ color: {TEXT_FAINT}; font-size: 13px; }}
.statusbar {{ background: {BG0}; padding: 2px 8px; color: {TEXT_DIM}; font-size: 11px; min-height: 18px; }}
.statusbar .warning {{ color: {WARN}; }}

entry, spinbutton, dropdown > button, combobox button {{ background: {BG0}; border: 1px solid {BG4}; border-radius: 4px; min-height: 22px; }}
entry {{ padding: 0 6px; }}
entry:focus-within {{ border-color: {ACCENT_DIM}; outline: none; }}
dropdown > button {{ padding: 0 6px; }}
dropdown.flat > button {{ background: none; border-color: transparent; color: {TEXT_DIM}; }}
scale {{ padding: 2px 4px; min-height: 14px; }}
scale trough {{ background: {BG0}; min-height: 3px; border-radius: 2px; }}
scale highlight {{ background: {ACCENT_DIM}; border-radius: 2px; }}
scale slider {{ background: {TEXT_DIM}; min-width: 10px; min-height: 10px; border-radius: 6px; margin: -4px; border: none; box-shadow: none; }}
scale slider:hover {{ background: {TEXT}; }}
scrollbar {{ background: transparent; border: none; }}
scrollbar slider {{ background: {BG4}; border-radius: 6px; min-width: 6px; min-height: 6px; margin: 0; border: 2px solid transparent; background-clip: padding-box; }}
scrollbar slider:hover {{ background: {TEXT_FAINT}; }}
progressbar trough {{ background: {BG0}; min-height: 6px; border-radius: 3px; }}
progressbar progress {{ background: {ACCENT}; min-height: 6px; border-radius: 3px; }}
switch {{ min-height: 16px; min-width: 30px; }}
switch:checked {{ background: {ACCENT_DIM}; }}
checkbutton check {{ min-height: 13px; min-width: 13px; }}
checkbutton check:checked {{ background: {ACCENT_DIM}; }}

columnview, listview, gridview, list {{ background: {BG1}; }}
columnview > header > button {{ background: {BG2}; border: none; border-right: 1px solid {LINE}; border-bottom: 1px solid {LINE};
    padding: 2px 6px; font-weight: 500; color: {TEXT_DIM}; border-radius: 0; min-height: 20px; }}
columnview > listview > row {{ min-height: 24px; padding: 0; }}
columnview > listview > row > cell {{ padding: 1px 6px; }}
listview > row, list > row {{ padding: 1px 4px; min-height: 22px; }}
row:selected, gridview > child:selected {{ background: {ACCENT_DIM}; color: #ffffff; }}
row:hover:not(:selected) {{ background: {BG2}; }}
gridview > child {{ padding: 5px; border-radius: 5px; }}
.label-chip {{ min-width: 10px; min-height: 10px; border-radius: 2px; }}
.thumb {{ background: {BG0}; border-radius: 3px; }}

.fx-section {{ border-bottom: 1px solid {LINE}; }}
.fx-header {{ background: {BG2}; padding: 2px 4px; min-height: 24px; }}
.fx-header label {{ font-weight: 600; }}
.fx-header .fx-toggle {{ font-style: italic; font-weight: 700; font-size: 11px; padding: 0 4px; min-height: 16px; min-width: 16px; color: {TEXT_FAINT}; }}
.fx-header .fx-toggle:checked {{ color: {ACCENT}; background: none; }}
.fx-row {{ padding: 1px 6px 1px 8px; min-height: 24px; }}
.fx-row .param-label {{ color: {TEXT}; }}
.kf-button {{ padding: 1px; min-height: 16px; min-width: 16px; color: {TEXT_FAINT}; }}
.kf-button:checked, .kf-button.on {{ color: {ACCENT}; }}
textview, textview text {{ background: {BG0}; color: {TEXT}; }}
textview {{ border: 1px solid {BG4}; border-radius: 4px; padding: 4px; }}
tooltip {{ background: {BG3}; color: {TEXT}; border: 1px solid {LINE}; border-radius: 4px; padding: 2px 6px; }}
paned > separator {{ background: {BG0}; min-width: 5px; min-height: 5px; }}
.workspace-switch button {{ background: none; border: none; box-shadow: none; color: {TEXT_DIM}; padding: 2px 12px; border-radius: 4px; font-weight: 500; }}
.workspace-switch button:checked {{ color: {TEXT}; background: {BG3}; }}
.dialog-body {{ padding: 16px; }}
button.suggested-action {{ background: {ACCENT_DIM}; color: #ffffff; border: none; }}
button.suggested-action:hover {{ background: {ACCENT}; color: #0b1a18; }}
button.destructive-action {{ background: {DANGER}; color: #ffffff; border: none; }}
.heading {{ font-weight: 700; font-size: 13px; }}
.launcher-side {{ background: {BG1}; border-right: 1px solid {LINE}; padding: 28px 22px 16px 22px; }}
.launcher-hero {{ font-size: 26px; font-weight: 700; color: {TEXT}; }}
.launcher-title {{ font-size: 18px; font-weight: 700; color: {TEXT}; }}
.launcher-footer {{ background: {BG1}; border-top: 1px solid {LINE}; padding: 10px 16px; }}
.launcher-action {{ min-height: 32px; font-weight: 600; }}
.choice-card {{ background: {BG1}; border: 2px solid {BG3}; border-radius: 8px; padding: 12px; }}
.choice-card:hover {{ border-color: {BG4}; background: {BG2}; }}
.choice-card label {{ font-weight: 400; }}
.choice-card label.heading {{ font-weight: 700; }}
.choice-card:checked {{ border-color: {ACCENT}; background: {BG2}; color: {TEXT}; }}
.step-dot {{ min-width: 8px; min-height: 8px; border-radius: 4px; background: {BG4}; }}
.step-dot.current {{ background: {ACCENT}; min-width: 20px; }}
.recent-list {{ background: transparent; }}
.recent-list > row.recent-row {{ padding: 8px 10px; border-radius: 6px; margin-bottom: 2px; }}
.error-text {{ color: {DANGER}; }}
.warning-text {{ color: {WARN}; }}
.key-cap {{ background: {BG3}; border: 1px solid {BG4}; border-radius: 4px; padding: 1px 6px; font-family: monospace; font-size: 11px; }}
.popout-bar {{ background: {BG2}; border-bottom: 1px solid {LINE}; }}
window.fullscreen-output, window.fullscreen-output .popout {{ background: #000000; }}
.caption-progress {{ min-height: 6px; }}
"""


def install():
    provider = Gtk.CssProvider()
    provider.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
