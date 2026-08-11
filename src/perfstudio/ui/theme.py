"""Application chrome: one dark palette for the window around the board.

Kept apart from ``view2d``'s theme block on purpose, because the two answer different
questions. ``view2d`` colours a PHYSICAL OBJECT -- FR4 green, tinned copper, solder grey --
and those colours are chosen to look like the thing on the bench, and to keep conductor
kinds apart at a glance. This file colours the APPLICATION, and its only job is to stay
out of the way: a board is the brightest, most saturated thing on screen, and every panel
around it is deliberately dimmer so the eye lands on the work rather than on the furniture.

Qt's default light chrome around a dark board is what made the promoted prototype look
unfinished -- a bright grey dock beside a dark viewport reads as two applications stapled
together.
"""

from __future__ import annotations

# Greys, dark to light. Named by role rather than shade so a change here does not require
# renaming anything: WINDOW is the furniture, PANEL the raised surfaces on it, and the
# board viewport is darker than both.
WINDOW = "#1b1d24"
PANEL = "#212430"
PANEL_ALT = "#262a38"
BORDER = "#333849"
TEXT = "#dfe3ec"
TEXT_DIM = "#98a0b4"
ACCENT = "#4c9dff"
ACCENT_DIM = "#2c5fa0"

#: Severity colours, shared with the DRC dock's own text so a warning reads the same
#: whether it appears in a tree, a status field or on the board.
ERROR = "#e5484d"
WARNING = "#e0a33c"
OK = "#3fb950"

STYLESHEET = f"""
QMainWindow, QWidget {{
    background: {WINDOW};
    color: {TEXT};
}}
QMenuBar {{
    background: {WINDOW};
    border-bottom: 1px solid {BORDER};
    padding: 2px 4px;
}}
QMenuBar::item {{
    padding: 5px 10px;
    border-radius: 4px;
}}
QMenuBar::item:selected {{ background: {PANEL_ALT}; }}
QMenu {{
    background: {PANEL};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 24px 6px 12px;
    border-radius: 4px;
}}
QMenu::item:selected {{ background: {ACCENT_DIM}; }}
QMenu::item:disabled {{ color: {TEXT_DIM}; }}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 6px;
}}

QToolBar {{
    background: {WINDOW};
    border-bottom: 1px solid {BORDER};
    padding: 4px 6px;
    spacing: 2px;
}}
QToolBar QToolButton {{
    padding: 5px 11px;
    border-radius: 5px;
    color: {TEXT};
}}
QToolBar QToolButton:hover {{ background: {PANEL_ALT}; }}
QToolBar QToolButton:pressed {{ background: {ACCENT_DIM}; }}
QToolBar QToolButton:checked {{
    background: {ACCENT_DIM};
    color: #ffffff;
}}
QToolBar QToolButton:disabled {{ color: {TEXT_DIM}; }}
QToolBar::separator {{
    width: 1px;
    background: {BORDER};
    margin: 4px 6px;
}}

QDockWidget {{
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
}}
QDockWidget::title {{
    background: {PANEL_ALT};
    padding: 6px 10px;
    border-bottom: 1px solid {BORDER};
    /* Left-aligned: a centred dock title over a wide panel reads as a heading for the
       whole window rather than a label for the panel it belongs to. */
    text-align: left;
}}

QTreeWidget {{
    background: {PANEL};
    alternate-background-color: {PANEL_ALT};
    border: none;
    outline: none;
}}
QTreeWidget::item {{ padding: 3px 4px; }}
QTreeWidget::item:selected {{ background: {ACCENT_DIM}; color: #ffffff; }}
QTreeWidget::item:hover {{ background: {PANEL_ALT}; }}
QHeaderView::section {{
    background: {WINDOW};
    color: {TEXT_DIM};
    padding: 5px 6px;
    border: none;
    border-bottom: 1px solid {BORDER};
}}

QStatusBar {{
    background: {WINDOW};
    border-top: 1px solid {BORDER};
}}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ padding: 2px 8px; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QScrollBar:vertical, QScrollBar:horizontal {{
    background: {WINDOW};
    border: none;
}}
QScrollBar:vertical {{ width: 11px; }}
QScrollBar:horizontal {{ height: 11px; }}
QScrollBar::handle {{
    background: {BORDER};
    border-radius: 5px;
    min-width: 24px;
    min-height: 24px;
}}
QScrollBar::handle:hover {{ background: {TEXT_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QMessageBox, QFileDialog {{ background: {WINDOW}; }}
QPushButton {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 6px 14px;
    color: {TEXT};
}}
QPushButton:hover {{ background: {BORDER}; }}
QPushButton:default {{ background: {ACCENT_DIM}; border-color: {ACCENT}; }}
QToolTip {{
    background: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 6px;
}}
"""
