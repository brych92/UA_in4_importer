def classFactory(iface):  # pylint: disable=invalid-name
    from .importer_code import in4Importer
    return in4Importer(iface)
