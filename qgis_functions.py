from qgis.utils import iface
from qgis.core import QgsFeature, QgsFields, QgsField, QgsPoint, QgsGeometry

def createLayer(layerName, layerType, layerFields):
    layer = iface.addVectorLayer(layerType, layerName, "memory")

def make_feature(feature_dict: dict, type: str) -> QgsFeature:
    QMetaTypes_list = {
        'int': 2,
        'float': 6,
        'str': 10
    }
    qgsPoint_list = []
    for point in feature_dict['geometry']:
        qgsPoint = QgsPoint(point[0], point[1])
        qgsPoint_list.append(qgsPoint)
    if type == 'polygon':
        geometry = QgsGeometry.fromPolygonXY([qgsPoint_list])
    elif type == 'polyline':
        geometry = QgsGeometry.fromPolylineXY(qgsPoint_list)
    
    fields = QgsFields()
    for key, value in feature_dict.items():
        if key != 'geometry':
            fields.append(QgsField(key))
            

    feature = QgsFeature(fields)
    feature.setGeometry(geometry)
    for key, value in feature_dict.items():
        if key != 'geometry':
            feature[key] = value
    
    return feature