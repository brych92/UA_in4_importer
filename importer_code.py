import os

from qgis.core import Qgis, QgsProject, QgsCoordinateReferenceSystem, QgsUnitTypes
from qgis.gui import QgsProjectionSelectionDialog
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog

from .parser import load_in4_files_to_project, log, crs_list
from .ua_SPT import uaSPT

class in4Importer:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.folder_path=os.path.expanduser('~')
        
    def initGui(self):
        self.toolbar = uaSPT.getToolbar(self.iface)
        icon = QIcon(os.path.join(self.plugin_dir,"icon.png"))
        action = QAction(icon, "Імортувати файли IN4",self.iface.mainWindow())
        action.triggered.connect(self.run)
        action.setEnabled(True)
        self.toolbar.addAction(action)
        self.actions.append(action)
    
    def unload(self):
        for action in self.actions:
            self.toolbar.removeAction(action)
        
        if self.toolbar.children() == []:
            self.toolbar.deleteLater()

    def run(self):
        # 1. Вибір IN4-файлів
        paths = QFileDialog.getOpenFileNames(
            None,
            "Виберіть IN4 файл(и) для імпорту",
            self.folder_path,
            "Кадастровий IN4 (*.in4)"
        )[0]

        if not paths:
            log("Вибір IN4 файлів скасовано користувачем.", level=Qgis.Info)  # type: ignore
            return
        
        log(f"ВИбрано {len(paths)} IN4-файл(ів)", level=Qgis.Info)  # type: ignore

        self.folder_path = os.path.dirname(paths[0])
        
        # 2. Формуємо фільтр СК: тільки дійсні метричні (без градусних)
        crs_filter = []
        for epsg in crs_list:
            crs = QgsCoordinateReferenceSystem.fromEpsgId(epsg)
            if not crs.isValid():
                continue
            if crs.mapUnits() == QgsUnitTypes.DistanceDegrees: # type: ignore
                # відкидаємо географічні CRS у градусах
                continue
            crs_filter.append(crs.authid())  # типу 'EPSG:9837'

        if not crs_filter:
            msg = "Не вдалося сформувати список систем координат."
            self.iface.messageBar().pushMessage(
                "IN4-імпорт",
                msg,
                level=Qgis.Critical,# type: ignore
                duration=0,
            )
            log(msg, level=Qgis.Critical)# type: ignore
            return

        # 3. Діалог вибору СК
        crs_dialog = QgsProjectionSelectionDialog()
        crs_dialog.setOgcWmsCrsFilter(crsFilter=crs_filter)

        # встановлюємо поточну СК проекту як стартову
        crs_dialog.setCrs(QgsProject.instance().crs())

        if not crs_dialog.exec():
            msg = "Вибір системи координат скасовано."
            self.iface.messageBar().pushMessage(
                "IN4-імпорт",
                msg,
                level=Qgis.Info,# type: ignore
                duration=5,
            )
            log(msg, level=Qgis.Info)# type: ignore
            target_crs_authid = "EPSG:7825"
        else:
            new_crs = crs_dialog.crs()
            target_crs_authid = new_crs.authid()  # 'EPSG:XXXX'

        # 4. Завантаження IN4 у тимчасові шари та додавання в групу
        try:
            res = load_in4_files_to_project(paths, crs_authid=target_crs_authid, styles_path=os.path.join(self.plugin_dir, "styles"))
        except Exception as e:
            msg = f"Помилка імпорту IN4: {e}"
            self.iface.messageBar().pushMessage(
                "IN4-імпорт",
                msg,
                level=Qgis.Critical,# type: ignore
                duration=0,
            )
            log(msg, level=Qgis.Critical)# type: ignore
            raise

        if res is None:
            msg = "Не вдалося створити шари з файлів IN4."
            self.iface.messageBar().pushMessage(
                "IN4-імпорт",
                msg,
                level=Qgis.Warning,# type: ignore
                duration=8,
            )
            log(msg, level=Qgis.Warning)# type: ignore
            return

        group = res["group"]
        parcels_layer = res["parcels"]
        lands_layer = res["lands"]

        msg = (
            f"Імпортовано {parcels_layer.featureCount()} ділянок "
            f"і {lands_layer.featureCount()} угідь у групу «{group.name()}»."
        )

        self.iface.messageBar().pushMessage(
            "IN4-імпорт",
            msg,
            level=Qgis.Success,# type: ignore
            duration=8,
        )
        log(msg, level=Qgis.Success)# type: ignore

