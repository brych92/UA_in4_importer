import re,os 
from qgis.core import Qgis, QgsMessageLog
from datetime import datetime
from qgis.core import QgsProject

from PyQt5.QtCore import QVariant
from qgis.core import (
    QgsVectorLayer,
    QgsFields,
    QgsField,
    QgsFeature,
    QgsPointXY,
    QgsGeometry,
    QgsProject,
    QgsWkbTypes,
)



logging = True

crs_list = [    
    7825, 7826, 7827, 7828, 7829, 7830, 7831,     # Pulkovo 1942 / CS63 zone X1–X7
    5562, 5563, 5564, 5565,                        # UCS-2000 / Gauss-Kruger zone 4–7
    9821,                                         # UCS-2000 / LCS-32 Kyiv region
    9831, 9832, 9833, 9834, 9835, 9836, 9837,
    9838, 9839,                                   # LCS-01,05,07,12,14,18,21,23,26
    9840, 9841, 9842, 9843, 9844, 9845, 9846,
    9847, 9848, 9849, 9850,                       # LCS-35,44,46,48,51,53,56,59,61,63,65
    9851, 9852, 9853, 9854, 9855, 9856, 9857,
    9858, 9859, 9860, 9861, 9862, 9863, 9864,
    9865,
]

def log(text: str, level: int = Qgis.Info) -> None: # type: ignore
    if logging:
        QgsMessageLog.logMessage(
            message = text, 
            tag = "in4 importer", 
            level = level)# type: ignore

def read_in4_text_auto(path: str) -> str:
    """
    Читає IN4 як байти і пробує визначити кодування:
    спочатку UTF-8 (з BOM/без), потім cp1251/windows-1251.

    Повертає unicode-рядок.
    """
    with open(path, "rb") as f:
        raw = f.read()

    for enc in ("utf-8-sig", "utf-8", "cp1251", "windows-1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue

    # Якщо зовсім біда – пробуємо хоч якось витягнути UTF-8
    return raw.decode("utf-8", errors="replace")

def parse_value(raw: str):
    """
    Читає текст після '=' і чистить його.
    Значення після '=':
    - якщо в лапках -> текст без лапок;
    - якщо без лапок і виглядає як число -> int/float;
    - інакше -> сирий текст.
    """
    raw = raw.strip()
    if raw.endswith(','):
        raw = raw[:-1].rstrip()

    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        text =  raw[1:-1].replace('\\"', '"').strip()
        if text == "-":
            return None
        return text

    # тут теж можемо мати просто "-" без лапок
    if raw == "-":
        return None
    
    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            return raw
    if re.fullmatch(r"-?\d+\.\d*", raw):
        try:
            return float(raw)
        except ValueError:
            return raw

    return raw


def parse_line_to_pairs(line: str):
    """
    'N=1,NP="172",X=5539880.30,...'
    -> [('N','1'), ('NP','"172"'), ('X','5539880.30'), ...]
    (коми всередині лапок не ріжемо).
    """
    s = line.strip()
    pairs = []
    i = 0
    n = len(s)

    while i < n:
        while i < n and s[i] in " \t,":
            i += 1
        if i >= n:
            break

        eq = s.find('=', i)
        if eq == -1:
            break

        key = s[i:eq].strip()
        j = eq + 1
        in_quotes = False
        start_val = j

        while j < n:
            ch = s[j]
            if ch == '"':
                in_quotes = not in_quotes
                j += 1
                continue
            if ch == ',' and not in_quotes:
                break
            j += 1

        raw_val = s[start_val:j].strip()
        pairs.append((key, raw_val))

        if j < n and s[j] == ',':
            j += 1
        i = j

    return pairs


def _assign_attr(obj: dict, key: str, value):
    """
    Присвоєння атрибутів:
    - перше значення -> просто поле;
    - друге й далі -> робимо список значень.
    """
    if key not in obj:
        obj[key] = value
    else:
        existing = obj[key]
        if isinstance(existing, list):
            existing.append(value)
        else:
            obj[key] = [existing, value]


def parse_in4_text(text: str) -> dict:
    """
    Результат:

    {
        "service_lines": [...],
        "zones": [ { "nodes": [...], ... }, ... ],
        "quarters": [
            {
                "nodes": [...],
                "DS": "...",
                "SD": "...",
                "BC": "...",
                "parcels": [
                    {
                        "SC": "...",
                        "cadnum": "XXXXXXXXXX:YY:ZZZ:WWWW",
                        "nodes": [...],
                        "lands": [
                            {
                                "cadnum": "...",
                                "nodes": [...],
                                ...
                            },
                            ...
                        ],
                        "neighbours": [ {...}, ... ],
                        ...
                    },
                    ...
                ],
                ...
            },
            ...
        ]
    }

    Для cadnum:
    cadnum = f"{DS}:{SD}:{SC[:-4]}:{SC[-4:]}"
    (якщо SC має довжину > 4 і є DS та SD).
    """
    result = {
        "service_lines": [],
        "zones": [],
        "quarters": [],
        "orphan_nodes": [],
    }

    current_zone = None
    current_quarter = None
    current_parcel = None
    current_land = None
    last_parcel = None  
    current_neighbour = None
    current_block_type = None  # "zone" | "quarter" | "parcel" | "land" | "neighbour"

    geometry_found = False

    def start_zone():
        nonlocal current_zone, current_quarter, current_parcel, current_land, current_neighbour, current_block_type
        z = {"nodes": []}
        result["zones"].append(z)
        current_zone = z
        current_quarter = None
        current_parcel = None
        current_land = None
        current_neighbour = None
        current_block_type = "zone"

    def start_quarter():
        nonlocal current_zone, current_quarter, current_parcel, current_land, current_neighbour, current_block_type
        q = {"nodes": [], "parcels": []}
        result["quarters"].append(q)
        current_quarter = q
        current_parcel = None
        current_land = None
        current_neighbour = None
        current_block_type = "quarter"

    def start_parcel():
        nonlocal current_quarter, current_parcel, last_parcel, current_land, current_neighbour, current_block_type
        if current_quarter is None:
            start_quarter()
        p = {"nodes": [], "lands": [], "neighbours": []}
        current_quarter["parcels"].append(p)
        current_parcel = p
        last_parcel = p          # <– запам'ятовуємо, що хоча б одна ділянка існує
        current_land = None
        current_neighbour = None
        current_block_type = "parcel"

    def start_land():
        nonlocal current_parcel, current_land, current_neighbour, current_block_type
        if current_parcel is None:
            current_land = None
            current_block_type = None
            return
        lu = {"nodes": []}
        # наслідуємо cadnum від ділянки, якщо він вже сформований
        if "cadnum" in current_parcel:
            lu["cadnum"] = current_parcel["cadnum"]
        current_parcel["lands"].append(lu)
        current_land = lu
        current_neighbour = None
        current_block_type = "land"

    def start_neighbour():
        nonlocal current_parcel, last_parcel, current_neighbour, current_land, current_block_type

        # використовуємо поточну ділянку, а якщо немає – останню відому
        target_parcel = current_parcel or last_parcel

        if target_parcel is None:
            # взагалі не було SR -> NB нікуди логічно не чіпляти
            current_neighbour = None
            current_block_type = None
            log(
                "IN4: NB без жодної ділянки (немає current_parcel/last_parcel) — "
                "вузли цього суміжника підуть у orphan_nodes",
                level=Qgis.Warning, # type: ignore
            )
            return

        nb = {"nodes": []}
        target_parcel.setdefault("neighbours", []).append(nb)
        current_parcel = target_parcel
        current_neighbour = nb
        current_land = None
        current_block_type = "neighbour"

    def current_obj():
        if current_block_type == "zone":
            return current_zone
        if current_block_type == "quarter":
            return current_quarter
        if current_block_type == "parcel":
            return current_parcel
        if current_block_type == "land":
            return current_land
        if current_block_type == "neighbour":
            return current_neighbour
        return None

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        # службові рядки
        if line.startswith("#"):
            result["service_lines"].append(line)
            continue

        # маркери початку блоків
        if '=' not in line:
            desc = line.rstrip(',').strip()
            if desc == "BR":
                start_zone()
            elif desc == "BL":
                start_quarter()
            elif desc == "SR":
                start_parcel()
            elif desc == "CL":
                start_land()
            elif desc == "NB":
                start_neighbour()
            else:
                log(f"Невідомиий маркер блоку: {desc}(рядок {line_no})", level=Qgis.Warning) # type: ignore
                pass
            continue

        # рядок з KEY=VALUE,...
        pairs = parse_line_to_pairs(line)
        if not pairs:
            log(f"IN4: не вдалося розпарсити рядок: {line} (рядок {line_no})", level=Qgis.Warning) # type: ignore
            continue

        # метрична інформація вузлів
        if pairs[0][0] == "N":
            obj = current_obj()
            node = {k: parse_value(v) for k, v in pairs}

            if obj is None:
                log(
                    f"IN4: метрична інформація поза будь-яким блоком, вузол збережено в orphan_nodes: {node} (рядок {line_no})",
                    level=Qgis.Warning, # type: ignore
                )
                result["orphan_nodes"].append(node)
                geometry_found = True
                continue
            
            obj.setdefault("nodes", []).append(node)
            geometry_found = True
            continue

        obj = current_obj()
        if obj is None:
            log(f"IN4: атрибути {pairs} поза будь-яким блоком (рядок {line_no})", level=Qgis.Warning) # type: ignore
            continue

        for key, raw_val in pairs:
            value = parse_value(raw_val)

            # спеціальний випадок: формуємо cadnum при SC у ділянці
            if key == "SC" and current_block_type == "parcel":
                _assign_attr(obj, key, value)

                # намагаємось зібрати cadnum
                if current_quarter is not None:
                    ds = current_quarter.get("DS")
                    sd = current_quarter.get("SD")
                    sc_str = str(value)
                    if ds is not None and sd is not None and len(sc_str) > 4:
                        ds_str = str(ds)
                        sd_str = str(sd)
                        q_part = sc_str[:-4]   # номер кварталу в зоні
                        p_part = sc_str[-4:]   # номер ділянки в кварталі
                        cadnum = f"{ds_str}:{sd_str}:{q_part}:{p_part}"
                        obj["cadnum"] = cadnum

                continue  # SC вже обробили

            # усі інші атрибути — як раніше
            _assign_attr(obj, key, value)
    if not geometry_found:
        log("У файлі не знайдено жодної метричної інформації (рядків N=...)", level=Qgis.Warning)  # type: ignore

    return result

def group_orphan_nodes_into_rings(orphan_nodes: list) -> list:
    """
    Розбиває orphan_nodes (у порядку з файлу) на списки-в кільця,
    де кожне кільце – послідовність вузлів з зростаючим N.
    Нове кільце починається, коли N <= попереднього.

    Повертає список груп:
      [ [node1, node2, ...], [nodeX, ...], ... ]
    """
    groups = []
    current = []
    prev_n = None

    for node in orphan_nodes:
        n = node.get("N")

        # якщо N немає або не int – просто вважаємо, що це продовження поточного кільця
        if not isinstance(n, int):
            if not current:
                current = [node]
            else:
                current.append(node)
            continue

        if prev_n is None:
            # перший вузол
            current = [node]
        else:
            if prev_n is not None and n <= prev_n:
                # N «скинувся» або не зростає – починаємо новий полігон
                if current:
                    groups.append(current)
                current = [node]
            else:
                current.append(node)

        prev_n = n

    if current:
        groups.append(current)

    return groups

def parse_in4_file(path: str) -> dict:
    """
    Читає IN4 з авто-визначенням кодування та парсить його
    у нашу структуру (quarters → parcels → lands/neighbours).
    """
    text = read_in4_text_auto(path)
    return parse_in4_text(text)


def build_polygon_from_nodes(nodes, object_name = None, file_name = None):
    """
    Створює полігон з nodes:
    nodes = [
        {"N": 1, "X": ..., "Y": ...},
        {"N": 2, "X": ..., "Y": ...},
        ...
    ]
    """
    if not nodes:
        log("IN4: полігон без вузлів (nodes порожній)")
        return None

    # сортуємо за N, якщо є
    sorted_nodes = sorted(nodes, key=lambda n: n.get("N", 0))

    pts = []
    for n in sorted_nodes:
        x = n.get("X")
        y = n.get("Y")
        if x is None or y is None:
            log(f"Немає координат у вузлі {n} {object_name} {file_name}", level=Qgis.Warning)# type: ignore
            continue
        pts.append(QgsPointXY(float(y), float(x)))

    if len(pts) < 3:
        log(f"Недостатньо точок у полігоні {object_name} {file_name} - Геометрія не створена!!!", level=Qgis.Warning) # type: ignore
        return None

    # QGIS сам замкне полігон, якщо остання точка не повторює першу
    return QgsGeometry.fromPolygonXY([pts])


def infer_field_types(units, attr_keys):
    """
    Визначає типи полів QGIS (QVariant) на основі перших
    ненульових значень по кожному ключу.
    """
    types = {}
    for key in attr_keys:
        qtype = None
        for u in units:
            if key not in u:
                continue
            val = u[key]
            if val is None:
                continue

            # списки завжди зберігаємо як текст
            if isinstance(val, list):
                qtype = QVariant.String
                break

            if isinstance(val, int):
                qtype = QVariant.Int
            elif isinstance(val, float):
                qtype = QVariant.Double
            else:
                qtype = QVariant.String

            # як тільки знайшли щось окрім String — можна зупинятись
            if qtype != QVariant.String:
                break

        types[key] = qtype or QVariant.String

    return types


def create_memory_polygon_layer(name, attr_types, crs_authid=None):
    """
    Створює memory-layer з полігонами та заданими полями.
    attr_types: dict {field_name: QVariant.Type}
    """
    if crs_authid:
        uri = f"Polygon?crs={crs_authid}"
    else:
        uri = "Polygon"

    layer = QgsVectorLayer(uri, name, "memory")
    pr = layer.dataProvider()

    fields = QgsFields()
    for field_name in sorted(attr_types.keys()):
        fields.append(QgsField(field_name, attr_types[field_name]))

    pr.addAttributes(fields)
    layer.updateFields()
    return layer


def create_cadastre_layers(data, crs_authid=None, add_to_project=False):
    """
    Створює тимчасові шари:
      - кадзон (zones)
      - кадастрові квартали (quarters)
      - ділянки (parcels)
      - угіддя (lands)

    і додає їх у проект QGIS.

    Повертає словник з посиланнями на шари.
    """
    zones_data = data.get("zones", [])
    quarters_data = data.get("quarters", [])

    # ---------------------------
    # 1) Шар КАДЗОН (zones)
    # ---------------------------
    zone_attr_keys = set()
    for z in zones_data:
        for k in z.keys():
            if k == "nodes":
                continue
            zone_attr_keys.add(k)

    zone_attr_types = infer_field_types(zones_data, zone_attr_keys)
    zones_layer = create_memory_polygon_layer("zones", zone_attr_types, crs_authid)

    zone_pr = zones_layer.dataProvider()
    zone_feats = []

    for z in zones_data:
        geom = build_polygon_from_nodes(z.get("nodes", []))
        if geom is None:
            continue

        feat = QgsFeature(zones_layer.fields())
        feat.setGeometry(geom)

        for key in zone_attr_keys:
            val = z.get(key)
            # списки – у строку
            if isinstance(val, list):
                val = "|".join(str(v) for v in val)
            feat[key] = val
        zone_feats.append(feat)

    if zone_feats:
        zone_pr.addFeatures(zone_feats)
        zones_layer.updateExtents()
        if add_to_project:
            QgsProject.instance().addMapLayer(zones_layer)

    # ---------------------------
    # 2) Шар КВАРТАЛИ (quarters)
    # ---------------------------
    quarter_attr_keys = set()
    for q in quarters_data:
        for k in q.keys():
            if k in ("nodes", "parcels"):
                continue
            quarter_attr_keys.add(k)

    quarter_attr_types = infer_field_types(quarters_data, quarter_attr_keys)
    quarters_layer = create_memory_polygon_layer("quarters", quarter_attr_types, crs_authid)

    q_pr = quarters_layer.dataProvider()
    q_feats = []

    for q in quarters_data:
        geom = build_polygon_from_nodes(q.get("nodes", []))
        if geom is None:
            continue

        feat = QgsFeature(quarters_layer.fields())
        feat.setGeometry(geom)

        for key in quarter_attr_keys:
            val = q.get(key)
            if isinstance(val, list):
                val = "|".join(str(v) for v in val)
            feat[key] = val
        q_feats.append(feat)

    if q_feats:
        q_pr.addFeatures(q_feats)
        quarters_layer.updateExtents()
        if add_to_project:
            QgsProject.instance().addMapLayer(quarters_layer)

    # ---------------------------
    # 3) Шар ДІЛЯНКИ (parcels)
    # ---------------------------
    parcels_all = []
    for q in quarters_data:
        for p in q.get("parcels", []):
            parcels_all.append(p)

    parcel_attr_keys = set()
    for p in parcels_all:
        for k in p.keys():
            if k in ("nodes", "lands", "neighbours"):
                continue
            parcel_attr_keys.add(k)

    parcel_attr_types = infer_field_types(parcels_all, parcel_attr_keys)
    parcels_layer = create_memory_polygon_layer("parcels", parcel_attr_types, crs_authid)

    p_pr = parcels_layer.dataProvider()
    p_feats = []

    for p in parcels_all:
        geom = build_polygon_from_nodes(p.get("nodes", []))
        if geom is None:
            continue

        feat = QgsFeature(parcels_layer.fields())
        feat.setGeometry(geom)

        for key in parcel_attr_keys:
            val = p.get(key)
            if isinstance(val, list):
                val = "|".join(str(v) for v in val)
            feat[key] = val
        p_feats.append(feat)

    if p_feats:
        p_pr.addFeatures(p_feats)
        parcels_layer.updateExtents()
        if add_to_project:
            QgsProject.instance().addMapLayer(parcels_layer)

    # ---------------------------
    # 4) Шар УГІДДЯ (lands)
    # ---------------------------
    lands_all = []
    for q in quarters_data:
        for p in q.get("parcels", []):
            for lu in p.get("lands", []):
                lands_all.append(lu)

    land_attr_keys = set()
    for lu in lands_all:
        for k in lu.keys():
            if k == "nodes":
                continue
            land_attr_keys.add(k)

    land_attr_types = infer_field_types(lands_all, land_attr_keys)
    lands_layer = create_memory_polygon_layer("lands", land_attr_types, crs_authid)

    l_pr = lands_layer.dataProvider()
    l_feats = []

    for lu in lands_all:
        geom = build_polygon_from_nodes(lu.get("nodes", []))
        if geom is None:
            continue

        feat = QgsFeature(lands_layer.fields())
        feat.setGeometry(geom)

        for key in land_attr_keys:
            val = lu.get(key)
            if isinstance(val, list):
                val = "|".join(str(v) for v in val)
            feat[key] = val
        l_feats.append(feat)

    if l_feats:
        l_pr.addFeatures(l_feats)
        lands_layer.updateExtents()
        if add_to_project:
            QgsProject.instance().addMapLayer(lands_layer)

    # ---------------------------
    # 5) Шар СУМІЖНИКИ (NB / neighbours)
    # ---------------------------
    neighbours_all = []
    for q in quarters_data:
        for p in q.get("parcels", []):
            for nb in p.get("neighbours", []):
                neighbours_all.append(nb)

    neighbour_attr_keys = set()
    for nb in neighbours_all:
        for k in nb.keys():
            if k == "nodes":
                continue
            neighbour_attr_keys.add(k)

    neighbour_attr_types = infer_field_types(neighbours_all, neighbour_attr_keys)

    # якщо немає жодного атрибуту – теж ок, зробимо шар тільки з геометрією
    neighbours_layer = create_memory_line_layer("neighbours", neighbour_attr_types, crs_authid)

    n_pr = neighbours_layer.dataProvider()
    n_feats = []

    for nb in neighbours_all:
        geom = build_polyline_from_nodes(nb.get("nodes", []))
        if geom is None:
            continue

        feat = QgsFeature(neighbours_layer.fields())
        feat.setGeometry(geom)

        for key in neighbour_attr_keys:
            val = nb.get(key)
            if isinstance(val, list):
                val = "|".join(str(v) for v in val)
            feat[key] = val

        n_feats.append(feat)

    if n_feats:
        n_pr.addFeatures(n_feats)
        neighbours_layer.updateExtents()
        if add_to_project:
            QgsProject.instance().addMapLayer(neighbours_layer)
    else:
        neighbours_layer = None  # якщо суміжників немає – шар все одно повернемо як None

    # ---------------------------
    # 5) Orphan-вузли (точковий шар)
    # ---------------------------
    
    orphan_nodes = data.get("orphan_nodes", [])
    orphan_point_layer = None

    if orphan_nodes:
        orphan_point_layer = create_memory_point_layer("in4_orphan_nodes", crs_authid)
        pr = orphan_point_layer.dataProvider()
        feats = []

        for n in orphan_nodes:
            x = n.get("X")
            y = n.get("Y")
            if x is None or y is None:
                log(f"IN4: вузол без координат X/Y: {n}", level=Qgis.Warning)  # type: ignore
                continue
            try:
                x_f = float(x)
                y_f = float(y)
            except Exception:
                continue

            f = QgsFeature()
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(y_f, x_f)))
            feats.append(f)

        if feats:
            pr.addFeatures(feats)
            orphan_point_layer.updateExtents()
            if add_to_project:
                QgsProject.instance().addMapLayer(orphan_point_layer)

    # ---------------------------
    # 5) Orphan-вузли -> полігони
    # ---------------------------    
    orphan_nodes = data.get("orphan_nodes", [])
    orphan_layer = None

    if orphan_nodes:
        # групуємо вузли у кільця полігонів
        rings = group_orphan_nodes_into_rings(orphan_nodes)

        # полігональний шар без атрибутів
        if crs_authid:
            uri = f"Polygon?crs={crs_authid}"
        else:
            uri = "Polygon"

        orphan_layer = QgsVectorLayer(uri, "in4_orphan_polygons", "memory")
        pr = orphan_layer.dataProvider()

        feats = []
        for ring in rings:
            geom = build_polygon_from_nodes(ring)
            if geom is None:
                # тут якраз спрацює твоє логування "менше трьох точок" всередині build_polygon_from_nodes
                continue

            f = QgsFeature()
            f.setGeometry(geom)
            feats.append(f)

        if feats:
            pr.addFeatures(feats)
            orphan_layer.updateExtents()
            if add_to_project:
                QgsProject.instance().addMapLayer(orphan_layer)
        else:
            orphan_layer = None  # порожній шар нам не потрібен
    
    return {
        "orphan_points": orphan_point_layer,
        "neighbours": neighbours_layer,
        "orphan_nodes": orphan_layer,
        "lands": lands_layer,
        "parcels": parcels_layer,
        "quarters": quarters_layer,
        "zones": zones_layer,
    }

def create_memory_line_layer(name, attr_types, crs_authid=None):
    """
    Створює тимчасовий лінійний шар (LineString) з заданими полями.
    attr_types: dict {field_name: QVariant.Type}
    """
    if crs_authid:
        uri = f"LineString?crs={crs_authid}"
    else:
        uri = "LineString"

    layer = QgsVectorLayer(uri, name, "memory")
    pr = layer.dataProvider()

    fields = []
    for field_name, field_type in attr_types.items():
        fields.append(QgsField(field_name, field_type))

    pr.addAttributes(fields)
    layer.updateFields()
    return layer

def build_polyline_from_nodes(nodes):
    """
    Формує лінійну геометрію (QgsGeometry) із списку вузлів:
    [{'N': 1, 'X': ..., 'Y': ...}, ...].

    Використовується для NB (суміжники) та orphan-ліній.
    """
    if not nodes:
        log("IN4: polyline без вузлів (nodes порожній)", level=Qgis.Warning) # type: ignore
        return None

    # сортуємо за N, як і для полігонів
    sorted_nodes = sorted(nodes, key=lambda n: n.get("N", 0))

    pts = []
    for n in sorted_nodes:
        x = n.get("Y")
        y = n.get("X")
        if x is None or y is None:
            log(f"IN4: вузол без координат X/Y для polyline: {n}", level=Qgis.Warning) # type: ignore
            continue
        try:
            pts.append(QgsPointXY(float(x), float(y)))
        except Exception as e:
            log(f"IN4: не вдалося перетворити координати вузла {n} у float: {e}", level=Qgis.Warning) # type: ignore
            continue

    if len(pts) < 2:
        log(f"IN4: недостатньо точок для лінії (знайдено {len(pts)})", level=Qgis.Warning) # type: ignore
        return None

    return QgsGeometry.fromPolylineXY(pts)

def create_memory_point_layer(name, crs_authid=None):
    uri = "Point"
    if crs_authid:
        uri += f"?crs={crs_authid}"
    layer = QgsVectorLayer(uri, name, "memory")
    # без полів, тільки геометрія
    return layer


def parse_in4_files(paths) -> dict:
    """
    Парсить кілька IN4-файлів і зливає їх в одну структуру
    такого ж формату, як parse_in4_text/parse_in4_file.

    Для зручності в кожен елемент (zone, quarter, parcel, land, neighbour)
    додається поле `_in4_path` та `_in4_name`.
    """
    merged = {
        "service_lines": [],
        "zones": [],
        "quarters": [],
    }
    log("Парсимо IN4-файли...")
    for path in paths:
        in4_name = os.path.basename(path)
        log(f"Обробляємо {in4_name}...")
        data = parse_in4_file(path)

        # службові рядки просто додаємо в купу
        merged["service_lines"].extend(data.get("service_lines", []))

        # зони
        for z in data.get("zones", []):
            z["_in4_path"] = path
            z["_in4_name"] = in4_name
            merged["zones"].append(z)

        # квартали + вкладені ділянки/угіддя/суміжники
        for q in data.get("quarters", []):
            q["_in4_path"] = path
            q["_in4_name"] = in4_name

            for p in q.get("parcels", []):
                p["_in4_path"] = path
                p["_in4_name"] = in4_name

                for lu in p.get("lands", []):
                    lu["_in4_path"] = path
                    lu["_in4_name"] = in4_name

                for nb in p.get("neighbours", []):
                    nb["_in4_path"] = path
                    nb["_in4_name"] = in4_name

            merged["quarters"].append(q)

    return merged

def load_in4_files_to_project(paths, crs_authid=None, styles_path=None):
    """
    1) Парсить кілька IN4 (parse_in4_files)
    2) Створює тимчасові шари (create_cadastre_layers, без авто-додавання)
    3) Додає всі ці шари в нову групу in4-YYYYMMDD-HHMMSS у поточному проекті.

    Повертає dict:
    {
        "group": <QgsLayerTreeGroup>,
        "zones": <QgsVectorLayer>,
        "quarters": <QgsVectorLayer>,
        "parcels": <QgsVectorLayer>,
        "lands": <QgsVectorLayer>,
    }
    """
    if not paths:
        return None

    # 1. Збираємо все з багатьох IN4
    data = parse_in4_files(paths)

    # 2. Створюємо шари (поки що не додаємо їх у проект)
    layers = create_cadastre_layers(data, crs_authid=crs_authid, add_to_project=False)
    project = QgsProject.instance()
    root = project.layerTreeRoot()

    # 3. Назва групи типу in4-20251211-153045
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    group_name = f"in4-{ts}"
    group = root.insertGroup(0, group_name)

    # 4. Додаємо шари в проект під цю групу
    for key, layer in layers.items():
        if layer is None:
            continue

        if layer.dataProvider().featureCount() == 0:
            continue

        # встановлюємо стиль
        if styles_path: 
            layer.loadNamedStyle(os.path.join(styles_path, layer.name() + ".qml"))
        
        # додаємо шар без автоматичного показу в корені дерева
        project.addMapLayer(layer, False)
        group.addLayer(layer)

    result = {"group": group}
    result.update(layers)
    return result

# data = parse_in4_file(r"C:\Users\brych\OneDrive\Документы\01 Робота\01 ДПТ\Виноградів Копанська 256-Ж\00 Вихідні дані\Зйомка\для Вашкеби.in4")

# layers = create_cadastre_layers(data)

# # наприклад:
# parcels_layer = layers["parcels"]
# lands_layer = layers["lands"]
