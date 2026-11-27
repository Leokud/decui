"""Styles shared by the generated desktop views."""

DESKTOP_STYLE = """
QMainWindow, QDialog, QScrollArea > QWidget > QWidget {
    background: #f5f7fb;
    color: #172b46;
}
QMainWindow QWidget, QDialog QWidget { color: #172b46; }
QLabel[heading="true"] { font-size: 22px; font-weight: 700; }
QPushButton {
    background: #ffffff;
    border: 1px solid #cbd6e6;
    border-radius: 7px;
    padding: 7px 12px;
    min-height: 19px;
}
QPushButton[compact="true"] { padding-left: 0; padding-right: 0; }
QPushButton:hover { background: #ebf2fd; border-color: #7b9ecf; }
QPushButton:pressed { background: #dce9fa; }
QPushButton:focus { border-color: #2563ae; }
QPushButton[primary="true"] {
    background: #225da6;
    border-color: #225da6;
    color: white;
    font-weight: 600;
}
QPushButton[primary="true"]:hover { background: #194d90; }
QPushButton[primary="true"]:pressed { background: #153c70; }
QPushButton[primary="true"]:focus { border-color: #0b2649; }
QPushButton:disabled { background: #eef1f6; border-color: #dce3ed; color: #65748a; }
QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox {
    background: white;
    border: 1px solid #cbd6e6;
    border-radius: 6px;
    min-height: 24px;
    padding: 4px 6px;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {
    border-color: #2563ae;
}
QLineEdit:read-only, QPlainTextEdit:read-only, QAbstractSpinBox:read-only {
    background: #edf2f9;
}
QTableView, QTableWidget {
    background: #ffffff;
    alternate-background-color: #f5f8fd;
    border: 1px solid #d6dfec;
    border-radius: 6px;
    selection-background-color: #d6e8ff;
    selection-color: #142945;
}
QLabel#emptyResults {
    background: #ffffff;
    border: 1px solid #d6dfec;
    border-radius: 6px;
    font-size: 14px;
    padding: 16px;
}
QLabel#reportNotice {
    background: #fff6de;
    color: #5a430e;
    border: 1px solid #e5c77e;
    border-radius: 6px;
    padding: 8px 12px;
}
QHeaderView::section {
    background: #eaf0f8;
    color: #243c5d;
    border: 0;
    border-bottom: 1px solid #d6dfec;
    padding: 7px 8px;
    font-weight: 600;
}
QTreeWidget#navigation {
    background: #eaf0f8;
    border: none;
    border-right: 1px solid #d6dfec;
    padding: 8px 6px;
    selection-background-color: #ccdef5;
    selection-color: #163761;
}
QTreeWidget#navigation::item { min-height: 32px; }
QStatusBar { background: #eaf0f8; border-top: 1px solid #d6dfec; }
QSplitter::handle { background: #d6dfec; }
"""
