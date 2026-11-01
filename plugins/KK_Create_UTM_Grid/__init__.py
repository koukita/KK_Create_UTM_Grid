# -*- coding: utf-8 -*-
"""QGIS plugin entry point for KK_Create_UTM_Grid."""


def classFactory(iface):
    from .kk_create_utm_grid import KKCreateUtmGridPlugin
    return KKCreateUtmGridPlugin(iface)
