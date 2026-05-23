# -*- coding: utf-8 -*-
"""Settings dialog for the Replace Geometry plugin."""

import os

from qgis.PyQt import uic
from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import QDialog


FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "replace_geometry_settings_dialog_base.ui")
)


class ReplaceGeometrySettingsDialog(QDialog, FORM_CLASS):
    """Persistent settings for the autonomous geometry replacement workflow."""

    SETTINGS_PREFIX = "replace_geometry"

    KEY_CONFIRM_REPLACEMENT = f"{SETTINGS_PREFIX}/confirm_replacement"
    KEY_SHOW_CAPTURE_HELP = f"{SETTINGS_PREFIX}/show_capture_help"
    KEY_PREVIEW_STYLE = f"{SETTINGS_PREFIX}/preview_style"
    KEY_PREVIEW_WIDTH = f"{SETTINGS_PREFIX}/preview_width"
    KEY_SHOW_BACKSPACE_MESSAGE = f"{SETTINGS_PREFIX}/show_backspace_message"
    KEY_RETURN_PREVIOUS_TOOL = f"{SETTINGS_PREFIX}/return_previous_tool"

    PREVIEW_BLUE = "blue"
    PREVIEW_ORANGE = "orange"
    PREVIEW_GREEN = "green"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)

        self._settings = QSettings()
        self._populate_combos()
        self.load_settings()

        self.buttonBox.accepted.connect(self.save_settings)
        self.buttonBox.rejected.connect(self.reject)

    def _populate_combos(self):
        """Populate combo boxes with stable internal values."""
        self.previewStyleCombo.clear()
        self.previewStyleCombo.addItem("Blue - default", self.PREVIEW_BLUE)
        self.previewStyleCombo.addItem("Orange - high contrast", self.PREVIEW_ORANGE)
        self.previewStyleCombo.addItem("Green - soft", self.PREVIEW_GREEN)

    def load_settings(self):
        """Load values from QSettings."""
        self.confirmReplacementCheck.setChecked(
            self._settings.value(self.KEY_CONFIRM_REPLACEMENT, True, type=bool)
        )
        self.showCaptureHelpCheck.setChecked(
            self._settings.value(self.KEY_SHOW_CAPTURE_HELP, True, type=bool)
        )
        self.showBackspaceMessageCheck.setChecked(
            self._settings.value(self.KEY_SHOW_BACKSPACE_MESSAGE, True, type=bool)
        )
        self.returnPreviousToolCheck.setChecked(
            self._settings.value(self.KEY_RETURN_PREVIOUS_TOOL, True, type=bool)
        )

        preview_style = self._settings.value(
            self.KEY_PREVIEW_STYLE,
            self.PREVIEW_BLUE,
            type=str,
        )
        self._set_combo_by_data(self.previewStyleCombo, preview_style)

        preview_width = self._settings.value(
            self.KEY_PREVIEW_WIDTH,
            2,
            type=int,
        )
        self.previewWidthSpin.setValue(preview_width)

    def save_settings(self):
        """Save values to QSettings and close the dialog."""
        self._settings.setValue(
            self.KEY_CONFIRM_REPLACEMENT,
            self.confirmReplacementCheck.isChecked(),
        )
        self._settings.setValue(
            self.KEY_SHOW_CAPTURE_HELP,
            self.showCaptureHelpCheck.isChecked(),
        )
        self._settings.setValue(
            self.KEY_SHOW_BACKSPACE_MESSAGE,
            self.showBackspaceMessageCheck.isChecked(),
        )
        self._settings.setValue(
            self.KEY_RETURN_PREVIOUS_TOOL,
            self.returnPreviousToolCheck.isChecked(),
        )
        self._settings.setValue(
            self.KEY_PREVIEW_STYLE,
            self.previewStyleCombo.currentData(),
        )
        self._settings.setValue(
            self.KEY_PREVIEW_WIDTH,
            self.previewWidthSpin.value(),
        )

        self.accept()

    @staticmethod
    def _set_combo_by_data(combo, value):
        """Set a combo box index by item data."""
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)
