# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ReplaceGeometry
                                 A QGIS plugin
 Replaces the geometry of one selected feature while preserving attributes.

 Fully autonomous refactor:
 - No dependency on the native QGIS Add Feature tool.
 - No temporary feature creation.
 - No attribute form suppression.
 - Uses QgsMapToolAdvancedDigitizing, QgsRubberBand and QgsSnapIndicator.
 **************************************************************************/
"""

import os.path

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QSettings, QTranslator, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QToolBar

from qgis.core import Qgis, QgsGeometry, QgsPointXY, QgsVectorLayer, QgsWkbTypes
from qgis.gui import QgsMapToolAdvancedDigitizing, QgsRubberBand, QgsSnapIndicator

from .resources import *  # noqa: F401,F403 - required to initialise Qt resources
from .replace_geometry_settings_dialog import ReplaceGeometrySettingsDialog


def _qt_enum(group_name, enum_name):
    """Return a Qt enum value in a way that works with Qt5 and Qt6."""
    from qgis.PyQt.QtCore import Qt

    value = getattr(Qt, enum_name, None)
    if value is not None:
        return value

    enum_group = getattr(Qt, group_name, None)
    if enum_group is not None:
        return getattr(enum_group, enum_name, None)

    return None


def _message_box_value(name):
    """Return QMessageBox enum values safely across PyQt5 and PyQt6."""
    standard_button = getattr(QMessageBox, "StandardButton", None)
    if standard_button is not None and hasattr(standard_button, name):
        return getattr(standard_button, name)
    return getattr(QMessageBox, name)


class ReplaceGeometryCaptureTool(QgsMapToolAdvancedDigitizing):
    """Canvas map tool used to capture replacement geometries with QGIS snapping."""

    geometryCaptured = pyqtSignal(QgsGeometry)
    captureCancelled = pyqtSignal()

    def __init__(
        self,
        canvas,
        target_wkb_type,
        cad_dock_widget=None,
        parent=None,
        preview_style="blue",
        preview_width=2,
        show_backspace_message=True,
    ):
        super().__init__(canvas, cad_dock_widget)

        self.canvas = canvas
        self.parent = parent
        self.target_wkb_type = target_wkb_type
        self.layer_geometry_type = QgsWkbTypes.geometryType(target_wkb_type)
        self.preview_style = preview_style
        self.preview_width = preview_width
        self.show_backspace_message = show_backspace_message

        self.points = []
        self.current_preview_point = None

        self.rubber_band = QgsRubberBand(canvas, self.layer_geometry_type)
        self.snap_indicator = QgsSnapIndicator(canvas)

        self._style_rubber_band()
        self.setAdvancedDigitizingAllowed(True)

        if hasattr(self, "setUseSnappingIndicator"):
            self.setUseSnappingIndicator(True)

    def _style_rubber_band(self):
        """Apply the configured rubber-band preview style."""
        if self.preview_style == "orange":
            line_color = QColor(230, 140, 40, 190)
            fill_color = QColor(230, 140, 40, 45)
        elif self.preview_style == "green":
            line_color = QColor(70, 170, 120, 190)
            fill_color = QColor(70, 170, 120, 45)
        else:
            line_color = QColor(0, 150, 255, 180)
            fill_color = QColor(0, 150, 255, 45)

        self.rubber_band.setColor(line_color)
        self.rubber_band.setWidth(int(self.preview_width))

        if self.layer_geometry_type == QgsWkbTypes.PolygonGeometry:
            self.rubber_band.setFillColor(fill_color)

    def activate(self):
        """Reset capture state when the tool becomes active."""
        super().activate()
        self.points = []
        self.current_preview_point = None
        self._reset_rubber_band()

    def deactivate(self):
        """Clean temporary canvas preview when the tool is deactivated."""
        self._reset_rubber_band()
        if self.snap_indicator is not None:
            self.snap_indicator.setVisible(False)
        super().deactivate()

    def cadCanvasPressEvent(self, event):
        """Handle snapped mouse press events from QGIS advanced digitising."""
        self._update_snap_indicator(event)

        left_button = _qt_enum("MouseButton", "LeftButton")
        right_button = _qt_enum("MouseButton", "RightButton")

        if event.button() == left_button:
            self._add_point(QgsPointXY(event.mapPoint()))
            return

        if event.button() == right_button:
            self._finish_capture()
            return

    def cadCanvasMoveEvent(self, event):
        """Update snap marker and live preview while the cursor moves."""
        self._update_snap_indicator(event)

        if self.layer_geometry_type == QgsWkbTypes.PointGeometry:
            return

        self.current_preview_point = QgsPointXY(event.mapPoint())
        self._update_preview(self.current_preview_point)

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts during capture."""
        escape_key = _qt_enum("Key", "Key_Escape")
        backspace_key = _qt_enum("Key", "Key_Backspace")
        return_key = _qt_enum("Key", "Key_Return")
        enter_key = _qt_enum("Key", "Key_Enter")

        key = event.key()

        if key == escape_key:
            self.cancel()
            return

        if key == backspace_key:
            self._remove_last_vertex()
            return

        if key in (return_key, enter_key):
            self._finish_capture()
            return

        super().keyPressEvent(event)

    def cancel(self):
        """Cancel the active capture session."""
        self.points = []
        self.current_preview_point = None
        self._reset_rubber_band()

        if self.snap_indicator is not None:
            self.snap_indicator.setVisible(False)

        self.captureCancelled.emit()

    def _add_point(self, point):
        """Add a clicked point to the capture."""
        if self.layer_geometry_type == QgsWkbTypes.PointGeometry:
            self.geometryCaptured.emit(QgsGeometry.fromPointXY(point))
            return

        self.points.append(point)
        self._update_preview(self.current_preview_point)

    def _remove_last_vertex(self):
        """Remove the last captured vertex and refresh the preview."""
        if not self.points:
            self._show_message("No vertices to remove.", level=Qgis.Info)
            return

        self.points.pop()
        self._update_preview(self.current_preview_point)

        if self.show_backspace_message:
            self._show_message("Last vertex removed.", level=Qgis.Info)

    def _finish_capture(self):
        """Finish line or polygon capture and emit the resulting geometry."""
        if self.layer_geometry_type == QgsWkbTypes.LineGeometry:
            if len(self.points) < 2:
                self._show_message("A line requires at least two points.", level=Qgis.Warning)
                return
            self.geometryCaptured.emit(QgsGeometry.fromPolylineXY(self.points))
            return

        if self.layer_geometry_type == QgsWkbTypes.PolygonGeometry:
            if len(self.points) < 3:
                self._show_message("A polygon requires at least three points.", level=Qgis.Warning)
                return

            polygon_points = list(self.points)
            if polygon_points[0] != polygon_points[-1]:
                polygon_points.append(polygon_points[0])

            self.geometryCaptured.emit(QgsGeometry.fromPolygonXY([polygon_points]))
            return

    def _update_preview(self, temporary_point=None):
        """Refresh the rubber-band preview, including live polygon fill."""
        self._reset_rubber_band()

        preview_points = list(self.points)
        if temporary_point is not None:
            preview_points.append(temporary_point)

        if not preview_points:
            return

        if self.layer_geometry_type == QgsWkbTypes.LineGeometry:
            for point in preview_points:
                self.rubber_band.addPoint(point, False)
            self.rubber_band.show()
            self.rubber_band.updatePosition()
            return

        if self.layer_geometry_type == QgsWkbTypes.PolygonGeometry:
            polygon_points = list(preview_points)
            if len(polygon_points) >= 3 and polygon_points[0] != polygon_points[-1]:
                polygon_points.append(polygon_points[0])

            for point in polygon_points:
                self.rubber_band.addPoint(point, False)
            self.rubber_band.show()
            self.rubber_band.updatePosition()

    def _reset_rubber_band(self):
        """Clear the current rubber-band geometry."""
        if self.rubber_band is not None:
            self.rubber_band.reset(self.layer_geometry_type)

    def _update_snap_indicator(self, event):
        """Update the snapping marker using the current snapped mouse match."""
        if self.snap_indicator is None:
            return
        if hasattr(event, "mapPointMatch"):
            self.snap_indicator.setMatch(event.mapPointMatch())

    def cleanup(self):
        """Remove snapping indicator and rubber-band from the canvas scene."""
        if self.snap_indicator is not None:
            self.snap_indicator.setVisible(False)
            self.snap_indicator = None

        if self.rubber_band is not None:
            scene = self.canvas.scene()
            if scene is not None:
                scene.removeItem(self.rubber_band)
            self.rubber_band = None

    def _show_message(self, message, level=Qgis.Info, duration=3):
        """Send capture feedback to the QGIS message bar."""
        if self.parent is not None and hasattr(self.parent, "_push_message"):
            self.parent._push_message(message, level=level, duration=duration)


class ReplaceGeometry:
    """QGIS plugin implementation."""

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

    TOOLBAR_NAME = "Replace Geometry Toolbar"
    MESSAGE_TITLE = "Replace Geometry Plugin"

    def __init__(self, iface):
        """Constructor."""
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)

        self.translator = None
        self.actions = []
        self.menu = self.tr("&Replace Geometry")

        self.toolbar = None
        self.toolbar_created_by_plugin = False
        self.replace_action = None
        self.settings_action = None

        self.capture_tool = None
        self.previous_map_tool = None

        self.target_layer = None
        self.target_feature_id = None
        self.first_start = None

        self._install_translator()
        self.plugin_version = self._read_plugin_version()

    def _install_translator(self):
        """Install the plugin translator if a matching translation file exists."""
        locale = QSettings().value("locale/userLocale", "", type=str)
        if not locale:
            return

        locale_path = os.path.join(
            self.plugin_dir,
            "i18n",
            f"ReplaceGeometry_{locale[0:2]}.qm",
        )

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            if self.translator.load(locale_path):
                QCoreApplication.installTranslator(self.translator)

    def tr(self, message):
        """Return a translated string using the Qt translation API."""
        return QCoreApplication.translate("ReplaceGeometry", message)

    def initGui(self):
        """Create the menu entries and toolbar controls inside the QGIS GUI."""
        self.first_start = True
        self._create_toolbar()
        self._create_replace_action()
        self._create_settings_action()

    def unload(self):
        """Remove plugin GUI, signal connections and custom map tool."""
        self._clear_capture_tool(restore_previous_tool=False)
        toolbar_valid = self._qt_object_is_valid(self.toolbar)

        for action in self.actions:
            try:
                self.iface.removePluginMenu(self.menu, action)
            except RuntimeError:
                pass

            if toolbar_valid:
                try:
                    self.toolbar.removeAction(action)
                except RuntimeError:
                    pass

            if self._qt_object_is_valid(action):
                action.deleteLater()

        self.actions.clear()

        if self._qt_object_is_valid(self.settings_action):
            try:
                self.iface.removePluginMenu(self.menu, self.settings_action)
            except RuntimeError:
                pass

            try:
                self.settings_action.triggered.disconnect(self.open_settings)
            except (TypeError, RuntimeError):
                pass

            self.settings_action.deleteLater()

        self.settings_action = None

        if toolbar_valid and self.toolbar_created_by_plugin:
            try:
                self.iface.mainWindow().removeToolBar(self.toolbar)
                self.toolbar.deleteLater()
            except RuntimeError:
                pass

        self.toolbar = None
        self.toolbar_created_by_plugin = False
        self.target_layer = None
        self.target_feature_id = None

    def _read_plugin_version(self):
        """Read plugin version from metadata.txt for display in the action label."""
        metadata_path = os.path.join(self.plugin_dir, "metadata.txt")
        if not os.path.exists(metadata_path):
            return "unknown"

        try:
            with open(metadata_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.lower().startswith("version="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            return "unknown"

        return "unknown"

    def _create_toolbar(self):
        """Create or reuse the plugin toolbar."""
        self.toolbar = self.iface.mainWindow().findChild(QToolBar, self.TOOLBAR_NAME)

        if self.toolbar is None:
            self.toolbar = self.iface.addToolBar(self.TOOLBAR_NAME)
            self.toolbar.setObjectName(self.TOOLBAR_NAME)
            self.toolbar_created_by_plugin = True
        else:
            self.toolbar_created_by_plugin = False

        self.toolbar.setToolTip(self.tr("Replace geometries while preserving attributes"))

    def _create_replace_action(self):
        """Create the main Replace Geometry action."""
        icon_path = ":/plugins/replace_geometry/icon.png"
        self.replace_action = self.add_action(
            icon_path=icon_path,
            text=self.tr(f"Replace Geometry - (v.{self.plugin_version})"),
            callback=self.run,
            parent=self.iface.mainWindow(),
            add_to_toolbar=True,
            add_to_menu=True,
            status_tip=self.tr(
                "Replace the selected feature geometry with a newly captured geometry."
            ),
        )

    def _create_settings_action(self):
        """Create the settings action. This is available from the plugin menu."""
        icon_path = ":/plugins/replace_geometry/icon.png"
        self.settings_action = QAction(
            QIcon(icon_path),
            self.tr("Settings"),
            self.iface.mainWindow(),
        )
        self.settings_action.setStatusTip(self.tr("Configure Replace Geometry."))
        self.settings_action.triggered.connect(self.open_settings)
        self.iface.addPluginToMenu(self.menu, self.settings_action)

    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None,
    ):
        """Add an action to the plugin toolbar and/or plugin menu."""
        action = QAction(QIcon(icon_path), text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip is not None:
            action.setStatusTip(status_tip)
        if whats_this is not None:
            action.setWhatsThis(whats_this)

        if add_to_toolbar and self.toolbar is not None:
            self.toolbar.addAction(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)

        self.actions.append(action)
        return action

    def open_settings(self):
        """Open the plugin settings dialog."""
        dialog = ReplaceGeometrySettingsDialog(self.iface.mainWindow())
        if dialog.exec():
            self._push_message(self.tr("Settings updated."), level=Qgis.Info, duration=3)

    def _setting(self, key, default, value_type=None):
        """Read a plugin setting safely."""
        settings = QSettings()
        if value_type is None:
            return settings.value(key, default)
        return settings.value(key, default, type=value_type)

    def run(self):
        """Start the autonomous replacement capture workflow."""
        self._clear_capture_tool(restore_previous_tool=True)

        layer = self.iface.activeLayer()
        if not self._validate_target_layer(layer):
            return

        selected_feature = layer.selectedFeatures()[0]
        selected_geometry = selected_feature.geometry()

        if self._is_empty_geometry(selected_geometry):
            self._show_warning(self.tr("The selected feature has no valid geometry to replace."))
            return

        if not self._confirm_if_outside_current_extent(selected_geometry):
            return

        self.target_layer = layer
        self.target_feature_id = selected_feature.id()
        self._start_capture_tool(layer)

    def _start_capture_tool(self, layer):
        """Activate the custom map tool for geometry capture."""
        canvas = self.iface.mapCanvas()
        self.previous_map_tool = canvas.mapTool()

        preview_style = self._setting(self.KEY_PREVIEW_STYLE, self.PREVIEW_BLUE, str)
        preview_width = self._setting(self.KEY_PREVIEW_WIDTH, 2, int)
        show_backspace_message = self._setting(
            self.KEY_SHOW_BACKSPACE_MESSAGE,
            True,
            bool,
        )

        self.capture_tool = ReplaceGeometryCaptureTool(
            canvas=canvas,
            target_wkb_type=layer.wkbType(),
            cad_dock_widget=self.iface.cadDockWidget(),
            parent=self,
            preview_style=preview_style,
            preview_width=preview_width,
            show_backspace_message=show_backspace_message,
        )

        self.capture_tool.geometryCaptured.connect(self._on_geometry_captured)
        self.capture_tool.captureCancelled.connect(self._on_capture_cancelled)
        canvas.setMapTool(self.capture_tool)

        if self._setting(self.KEY_SHOW_CAPTURE_HELP, True, bool):
            self._show_information(
                self.tr(
                    "Replacement capture started. Left click to add vertices, "
                    "right click or Enter to finish, Backspace removes the last vertex, "
                    "Esc cancels."
                )
            )

    def _on_geometry_captured(self, geometry):
        """Handle the geometry captured by the custom map tool."""
        self._clear_capture_tool(restore_previous_tool=True)

        if self.target_layer is None or self.target_feature_id is None:
            self._show_warning(self.tr("No target feature is available."))
            return

        layer = self.target_layer
        feature_id = self.target_feature_id

        if self._is_empty_geometry(geometry):
            self._show_warning(self.tr("The captured geometry is empty."))
            return

        if not self._geometry_is_compatible_with_layer(layer, geometry):
            self._show_warning(
                self.tr(
                    "The replacement geometry is not compatible with the active layer geometry type."
                )
            )
            return

        if not self._confirm_replacement(layer, feature_id, geometry):
            return

        self._replace_geometry(layer, feature_id, geometry)

    def _on_capture_cancelled(self):
        """Handle capture cancellation."""
        self._clear_capture_tool(restore_previous_tool=True)
        self._show_information(self.tr("Replacement capture cancelled."))

    def _clear_capture_tool(self, restore_previous_tool=True):
        """Deactivate and remove the custom capture tool safely."""
        canvas = self.iface.mapCanvas()

        if self.capture_tool is not None:
            try:
                self.capture_tool.geometryCaptured.disconnect(self._on_geometry_captured)
            except (TypeError, RuntimeError):
                pass

            try:
                self.capture_tool.captureCancelled.disconnect(self._on_capture_cancelled)
            except (TypeError, RuntimeError):
                pass

            return_previous_tool = self._setting(
                self.KEY_RETURN_PREVIOUS_TOOL,
                True,
                bool,
            )

            try:
                if canvas.mapTool() == self.capture_tool:
                    if restore_previous_tool and return_previous_tool and self.previous_map_tool is not None:
                        canvas.setMapTool(self.previous_map_tool)
                    else:
                        canvas.unsetMapTool(self.capture_tool)
            except RuntimeError:
                pass

            try:
                self.capture_tool.cleanup()
            except RuntimeError:
                pass

            self.capture_tool = None

        self.previous_map_tool = None

    def _validate_target_layer(self, layer):
        """Validate the active target layer and selected feature state."""
        if layer is None:
            self._show_warning(self.tr("No active layer selected."))
            return False

        if not isinstance(layer, QgsVectorLayer):
            self._show_warning(self.tr("The active layer is not a vector layer."))
            return False

        if QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.NullGeometry:
            self._show_warning(self.tr("The active layer does not contain geometries."))
            return False

        if not layer.isEditable():
            self._show_warning(self.tr("The active layer is not editable."))
            return False

        selected_count = layer.selectedFeatureCount()
        if selected_count == 0:
            self._show_warning(self.tr("No feature selected. Please select one feature."))
            return False

        if selected_count > 1:
            self._show_warning(
                self.tr("Multiple features selected. Please select only one feature.")
            )
            return False

        return True

    def _confirm_if_outside_current_extent(self, geometry):
        """Ask for confirmation if the selected geometry is outside the map extent."""
        canvas_extent = self.iface.mapCanvas().extent()
        if canvas_extent.intersects(geometry.boundingBox()):
            return True

        yes = _message_box_value("Yes")
        no = _message_box_value("No")
        response = QMessageBox.warning(
            self.iface.mainWindow(),
            self.tr(self.MESSAGE_TITLE),
            self.tr(
                "The selected geometry is outside the current map extent.\n\n"
                "Do you want to continue?"
            ),
            yes | no,
            no,
        )
        return response == yes

    def _geometry_is_compatible_with_layer(self, layer, geometry):
        """Return True if the replacement geometry is compatible with the target layer."""
        if self._is_empty_geometry(geometry):
            return False

        layer_geometry_type = QgsWkbTypes.geometryType(layer.wkbType())
        replacement_geometry_type = QgsWkbTypes.geometryType(geometry.wkbType())
        if layer_geometry_type != replacement_geometry_type:
            return False

        layer_is_multi = QgsWkbTypes.isMultiType(layer.wkbType())
        geometry_is_multi = QgsWkbTypes.isMultiType(geometry.wkbType())
        if geometry_is_multi and not layer_is_multi:
            return False

        return True

    def _is_empty_geometry(self, geometry):
        """Return True when a geometry is None, null or empty."""
        return geometry is None or geometry.isNull() or geometry.isEmpty()

    def _confirm_replacement(self, layer, feature_id, geometry):
        """Ask the user to confirm the final geometry replacement."""
        if not self._setting(self.KEY_CONFIRM_REPLACEMENT, True, bool):
            return True

        multipart_text = self.tr("Yes") if QgsWkbTypes.isMultiType(geometry.wkbType()) else self.tr("No")

        message = self.tr(
            "The new geometry is ready to replace the selected feature.\n\n"
            "Target layer: {layer_name}\n"
            "Feature ID: {feature_id}\n"
            "Multipart result: {multipart}\n\n"
            "Do you want to replace the selected geometry?"
        ).format(
            layer_name=layer.name(),
            feature_id=feature_id,
            multipart=multipart_text,
        )

        yes = _message_box_value("Yes")
        no = _message_box_value("No")
        response = QMessageBox.question(
            self.iface.mainWindow(),
            self.tr(self.MESSAGE_TITLE),
            message,
            yes | no,
            yes,
        )
        return response == yes

    def _replace_geometry(self, layer, feature_id, geometry):
        """Replace the original feature geometry."""
        if not isinstance(layer, QgsVectorLayer):
            self._show_warning(self.tr("The target layer is no longer valid."))
            return

        if not layer.isEditable():
            self._show_warning(self.tr("The active layer is no longer editable."))
            return

        layer.beginEditCommand(self.tr("Replace Geometry"))

        try:
            if not layer.changeGeometry(feature_id, geometry):
                raise RuntimeError(self.tr("Could not replace the selected feature geometry."))
        except Exception as exc:  # noqa: BLE001 - report all QGIS/provider failures to user
            layer.destroyEditCommand()
            self._show_critical(self.tr("Geometry replacement failed:\n\n{}").format(str(exc)))
            return

        layer.endEditCommand()
        layer.triggerRepaint()

        self.iface.setActiveLayer(layer)
        layer.selectByIds([feature_id])
        self._show_information(self.tr("Geometry replaced successfully."))

    def _show_warning(self, message):
        """Show a warning message."""
        QMessageBox.warning(self.iface.mainWindow(), self.tr(self.MESSAGE_TITLE), message)

    def _show_critical(self, message):
        """Show a critical error message."""
        QMessageBox.critical(self.iface.mainWindow(), self.tr(self.MESSAGE_TITLE), message)

    def _show_information(self, message):
        """Show a non-blocking information message."""
        self._push_message(message, level=Qgis.Info, duration=5)

    def _qt_object_is_valid(self, obj):
        """Return True if a Qt wrapper still points to a live C++ object."""
        return obj is not None and not sip.isdeleted(obj)

    def _push_message(self, message, level=Qgis.Info, duration=4):
        """Show a non-blocking message in the QGIS message bar."""
        self.iface.messageBar().pushMessage(
            self.tr(self.MESSAGE_TITLE),
            message,
            level=level,
            duration=duration,
        )
