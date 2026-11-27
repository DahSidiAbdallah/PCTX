"""Theme tokens and styling for PCTX's two interface modes.

Classic     -- modelled on PCSX2's own Qt front-end: the stock Fusion style with
               a neutral dark palette, native-looking controls, and almost no
               custom QSS. Anything that already looks right natively is left
               alone on purpose.
Big Picture -- a fullscreen, controller-first layout in the spirit of Steam's
               Big Picture. The main screen is web/bigpicture.html; the QSS here
               only restyles the native dialogs (Install, Settings, ...) that
               open on top of it so they match and stay readable from a couch.

Both are flat: no gradients, glows, or drop shadows.
"""

from PySide6.QtGui import QColor, QPalette

MODE_CLASSIC = "classic"
MODE_BIG_PICTURE = "big_picture"

# --- Shared semantic colours (status text, badges) ---
COLOR_SUCCESS = "#3fb950"
COLOR_WARNING = "#d29922"
COLOR_DANGER = "#f85149"

# --- Classic palette (PCSX2's "Dark Fusion"-style look) ---
CLASSIC = {
    "window": "#353535",
    "base": "#232323",
    "alt_base": "#2b2b2b",
    "button": "#404040",
    "text": "#f0f0f0",
    "muted": "#a0a0a0",
    "disabled": "#808080",
    "border": "#555555",
    "highlight": "#2a82da",
    "chrome": "#2b2b2b",
    "chrome_hover": "#474747",
}

# --- Big Picture palette (dark slate + one bright accent, like Steam's) ---
BIG_PICTURE = {
    "bg": "#0e1319",
    "surface": "#18212b",
    "surface_alt": "#212d3a",
    "border": "#2c3b4d",
    "text": "#e6edf5",
    "muted": "#8d9db0",
    "accent": "#1a9fff",
    "accent_text": "#ffffff",
}

COLOR_ACCENT = CLASSIC["highlight"]

# --- Spacing / geometry tokens (px) ---
SPACING_XS = 4
SPACING_SM = 8
SPACING_MD = 12
SPACING_LG = 16

TITLE_BAR_HEIGHT = 34
RESIZE_MARGIN = 6

# Drawn when a game has no cover art (yet). Size is the base the art is scaled from.
COVER_WIDTH = 200
COVER_HEIGHT = 266
PLACEHOLDER = {"bg": "#30343a", "line": "#4b525b", "text": "#9aa3ad"}

# Game-grid cover sizes (width; height keeps a 3:4 box-art ratio).
GRID_COVER_MIN = 72
GRID_COVER_MAX = 240
GRID_COVER_DEFAULT = 128

# Object names used to target widgets from QSS.
OBJ_MUTED_LABEL = "mutedLabel"
OBJ_SUCCESS_BUTTON = "successButton"
OBJ_STATUS_SUCCESS = "statusSuccess"
OBJ_STATUS_WARNING = "statusWarning"
OBJ_STATUS_ERROR = "statusError"
OBJ_OVERLAY_LABEL = "overlayLabel"
OBJ_APP_FRAME = "appFrame"
OBJ_TITLE_BAR = "titleBar"
OBJ_TITLE_BAR_LABEL = "titleBarLabel"
OBJ_TITLE_BAR_BUTTON = "titleBarButton"
OBJ_TITLE_BAR_CLOSE_BUTTON = "titleBarCloseButton"


def _status_label_qss() -> str:
    return f"""
    QLabel#{OBJ_STATUS_SUCCESS} {{ color: {COLOR_SUCCESS}; font-weight: 600; }}
    QLabel#{OBJ_STATUS_WARNING} {{ color: {COLOR_WARNING}; font-weight: 600; }}
    QLabel#{OBJ_STATUS_ERROR}   {{ color: {COLOR_DANGER}; font-weight: 600; }}
    """


def classic_qss() -> str:
    c = CLASSIC
    return f"""
    QToolTip {{
        background-color: {c['alt_base']}; color: {c['text']};
        border: 1px solid {c['border']}; padding: 3px 6px;
    }}
    QWidget#{OBJ_APP_FRAME} {{ background-color: {c['window']}; border: 1px solid #1c1c1c; }}
    QWidget#{OBJ_TITLE_BAR} {{ background-color: {c['chrome']}; border-bottom: 1px solid #1c1c1c; }}
    QLabel#{OBJ_TITLE_BAR_LABEL} {{ color: {c['text']}; font-weight: 600; }}
    QMenuBar {{ background: transparent; color: {c['text']}; padding: 0px; spacing: 1px; }}
    QMenuBar::item {{ padding: 4px 9px; margin: 0px; background: transparent; border-radius: 4px; }}
    QMenuBar::item:selected, QMenuBar::item:pressed {{ background-color: {c['chrome_hover']}; }}
    QToolBar QToolButton {{ border: 1px solid transparent; border-radius: 4px; padding: 3px 6px; }}
    QToolBar QToolButton:hover {{ background-color: #444444; border-color: #4c4c4c; }}
    QToolBar QToolButton:pressed {{ background-color: #2f2f2f; }}
    QToolBar QToolButton:checked {{ background-color: #2b4766; border-color: {c['highlight']}; }}
    QLineEdit {{ background-color: {c['base']}; border: 1px solid {c['border']}; border-radius: 4px; padding: 4px 7px;
        selection-background-color: {c['highlight']}; }}
    QLineEdit:focus {{ border-color: {c['highlight']}; }}
    QListWidget::item {{ border-radius: 4px; padding: 4px; }}
    QListWidget::item:hover {{ background-color: rgba(255, 255, 255, 18); }}
    QListWidget::item:selected {{ background-color: {c['highlight']}; color: #ffffff; }}
    QSlider::groove:horizontal {{ height: 4px; background: #262626; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {c['highlight']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: #e8e8e8; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
    QSlider::handle:horizontal:hover {{ background: #ffffff; }}
    QToolButton#{OBJ_TITLE_BAR_BUTTON} {{ background: transparent; border: none; border-radius: 3px; }}
    QToolButton#{OBJ_TITLE_BAR_BUTTON}:hover {{ background-color: {c['chrome_hover']}; }}
    QToolButton#{OBJ_TITLE_BAR_CLOSE_BUTTON} {{ background: transparent; border: none; border-radius: 3px; }}
    QToolButton#{OBJ_TITLE_BAR_CLOSE_BUTTON}:hover {{ background-color: #c42b1c; }}
    QLabel#{OBJ_MUTED_LABEL} {{ color: {c['muted']}; }}
    QLabel#{OBJ_OVERLAY_LABEL} {{ background-color: rgba(0, 0, 0, 150); color: white; }}
    QPushButton#{OBJ_SUCCESS_BUTTON} {{
        background-color: #2e7d32; border: 1px solid #1b5e20; border-radius: 3px; color: white; font-weight: 600; padding: 5px 16px;
    }}
    QPushButton#{OBJ_SUCCESS_BUTTON}:hover {{ background-color: #388e3c; }}
    QPushButton#{OBJ_SUCCESS_BUTTON}:disabled {{ background-color: {c['button']}; color: {c['disabled']}; border-color: {c['border']}; }}
    QToolBar {{ spacing: 4px; padding: 3px; border: none; border-bottom: 1px solid #1c1c1c; }}
    QStatusBar {{ border-top: 1px solid #1c1c1c; }}
    QStatusBar::item {{ border: none; }}
    """ + _status_label_qss()


def big_picture_qss() -> str:
    b = BIG_PICTURE
    return f"""
    QWidget {{ background-color: {b['bg']}; color: {b['text']}; font-size: 15px; }}
    QDialog, QMessageBox {{ background-color: {b['bg']}; }}
    QLabel {{ background: transparent; }}
    QLabel#{OBJ_MUTED_LABEL} {{ color: {b['muted']}; font-size: 13px; }}
    QLabel#{OBJ_OVERLAY_LABEL} {{ background-color: rgba(0, 0, 0, 150); color: white; }}
    QToolTip {{ background-color: {b['surface_alt']}; color: {b['text']}; border: 1px solid {b['border']}; padding: 4px 8px; }}
    QPushButton {{
        background-color: {b['surface_alt']}; color: {b['text']};
        border: 1px solid {b['border']}; border-radius: 4px;
        padding: 10px 22px; min-height: 22px; font-weight: 600;
    }}
    QPushButton:hover, QPushButton:focus {{ background-color: {b['accent']}; border-color: {b['accent']}; color: {b['accent_text']}; }}
    QPushButton:pressed {{ background-color: #1486dd; }}
    QPushButton:disabled {{ background-color: {b['surface']}; color: {b['muted']}; border-color: {b['border']}; }}
    QPushButton#{OBJ_SUCCESS_BUTTON} {{ background-color: #2f8f3a; border-color: #2f8f3a; color: white; }}
    QPushButton#{OBJ_SUCCESS_BUTTON}:hover, QPushButton#{OBJ_SUCCESS_BUTTON}:focus {{ background-color: #3fb950; border-color: #3fb950; }}
    QPushButton#{OBJ_SUCCESS_BUTTON}:disabled {{ background-color: {b['surface']}; color: {b['muted']}; border-color: {b['border']}; }}
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox {{
        background-color: {b['surface']}; border: 1px solid {b['border']};
        border-radius: 4px; padding: 8px 10px; selection-background-color: {b['accent']};
    }}
    QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {b['accent']}; }}
    QComboBox QAbstractItemView {{ background-color: {b['surface']}; selection-background-color: {b['accent']}; border: 1px solid {b['border']}; }}
    QGroupBox {{ border: 1px solid {b['border']}; border-radius: 6px; margin-top: 14px; padding-top: 12px; font-weight: 600; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {b['muted']}; }}
    QListWidget, QTreeWidget, QTableWidget {{
        background-color: {b['surface']}; border: 1px solid {b['border']}; border-radius: 4px;
        alternate-background-color: {b['surface_alt']};
    }}
    QListWidget::item, QTreeWidget::item {{ padding: 8px 6px; }}
    QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected {{ background-color: {b['accent']}; color: white; }}
    QHeaderView::section {{ background-color: {b['surface_alt']}; color: {b['muted']}; border: none; padding: 8px; font-weight: 600; }}
    QCheckBox, QRadioButton {{ spacing: 10px; }}
    QCheckBox::indicator {{ width: 20px; height: 20px; border: 1px solid {b['border']}; border-radius: 4px; background-color: {b['surface']}; }}
    QCheckBox::indicator:checked {{ background-color: {b['accent']}; border-color: {b['accent']}; }}
    QProgressBar {{ border: 1px solid {b['border']}; border-radius: 4px; text-align: center; background-color: {b['surface']}; }}
    QProgressBar::chunk {{ background-color: {b['accent']}; }}
    QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
    QScrollBar::handle:vertical {{ background-color: {b['border']}; border-radius: 6px; min-height: 28px; }}
    QScrollBar::handle:vertical:hover {{ background-color: {b['accent']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QMenu {{ background-color: {b['surface']}; border: 1px solid {b['border']}; padding: 4px; }}
    QMenu::item {{ padding: 8px 22px; border-radius: 3px; }}
    QMenu::item:selected {{ background-color: {b['accent']}; color: white; }}
    """ + _status_label_qss()


def qss_for(mode: str) -> str:
    return big_picture_qss() if mode == MODE_BIG_PICTURE else classic_qss()


def palette_for(mode: str) -> QPalette:
    p = QPalette()
    if mode == MODE_BIG_PICTURE:
        b = BIG_PICTURE
        window, base, alt, text = b["bg"], b["surface"], b["surface_alt"], b["text"]
        button, highlight, disabled = b["surface_alt"], b["accent"], b["muted"]
        muted = b["muted"]
    else:
        c = CLASSIC
        window, base, alt, text = c["window"], c["base"], c["alt_base"], c["text"]
        button, highlight, disabled = c["button"], c["highlight"], c["disabled"]
        muted = c["muted"]
    p.setColor(QPalette.Window, QColor(window))
    p.setColor(QPalette.WindowText, QColor(text))
    p.setColor(QPalette.Base, QColor(base))
    p.setColor(QPalette.AlternateBase, QColor(alt))
    p.setColor(QPalette.ToolTipBase, QColor(alt))
    p.setColor(QPalette.ToolTipText, QColor(text))
    p.setColor(QPalette.Text, QColor(text))
    p.setColor(QPalette.PlaceholderText, QColor(muted))
    p.setColor(QPalette.Button, QColor(button))
    p.setColor(QPalette.ButtonText, QColor(text))
    p.setColor(QPalette.BrightText, QColor("#ffffff"))
    p.setColor(QPalette.Link, QColor(highlight))
    p.setColor(QPalette.Highlight, QColor(highlight))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(disabled))
    return p


def apply_theme(app, mode: str) -> None:
    """Install the palette + stylesheet for `mode` application-wide, so every
    top-level dialog (not just children of the main window) picks it up.

    Re-applying an application stylesheet re-polishes every widget, which is
    the slowest single step of startup, so asking for the mode that's already
    installed is a no-op."""
    if app.property("pctx_theme") == mode:
        return
    app.setProperty("pctx_theme", mode)
    app.setStyle("Fusion")
    app.setPalette(palette_for(mode))
    app.setStyleSheet(qss_for(mode))
