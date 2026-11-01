# -*- coding: utf-8 -*-
"""
KK_Create_UTM_Grid
==================
QGIS 3.34以降 / QGIS 4.x 向けのDockWidget型プラグイン本体です。

このファイルの主な役割
----------------------
1. QGIS右側に「UTMグリッド作成」パネルを表示する。
2. ユーザーが選択したメッシュサイズと描画範囲を取得する。
3. 描画範囲をWGS84と各UTMゾーンへ座標変換する。
4. MGRS規則に従って10km、1km、100m、10m、1mグリッドを作る。
5. UTMゾーンをまたぐ場合はゾーンごとに別レイヤを作る。
6. ゾーン境界では東西に1列余分に作り、隙間ができにくいよう重複させる。
7. 作成予定数が200,000件を超える場合はユーザーへ確認する。
8. 同梱のmgrs_style.qmlを各出力レイヤへ適用する。

注意
----
・MGRSのUTM範囲である南緯80度～北緯84度を対象にしています。
・日付変更線をまたぐ範囲は対象外です。
・大量グリッドの生成はメモリを多く消費します。
"""

import logging
import math
import os
import traceback

# QtはQGIS同梱のラッパーから読み込みます。
# PyQt5/PyQt6を直接importしないことでQGIS 3/4双方への互換性を確保します。
from qgis.PyQt.QtCore import Qt, QVariant, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# QGISの主要クラスです。
# 座標変換、地物、ジオメトリ、レイヤ、ログなどに使用します。
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)

# QGIS標準の「範囲指定」GUI部品です。
from qgis.gui import QgsExtentGroupBox


logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# プラグイン共通定数
# -----------------------------------------------------------------------------

# メッセージバーやログへ表示するときのプラグイン名です。
PLUGIN_NAME = "KK_Create_UTM_Grid"
LOG_TAG = "KK_Create_UTM_Grid"

# この件数を超えると、グリッド生成前に確認ダイアログを表示します。
WARNING_FEATURE_COUNT = 200000

# 異常に大きな処理でQGISが長時間固まることを防ぐ安全上限です。
# 事前確認・実生成の双方で候補セル数がこの値を超えた場合は停止します。
MAX_CANDIDATE_CELL_COUNT = 2000000


# -----------------------------------------------------------------------------
# 共通補助関数
# -----------------------------------------------------------------------------

def message_level(name, legacy_name):
    """
    QGISのメッセージレベルをQGIS 3/4の差を吸収して取得します。

    QGISのバージョンによって列挙型の公開方法が異なる可能性があるため、
    まずスコープ付きQgis.MessageLevelを使い、存在しない環境では旧形式へ
    フォールバックします。
    """
    scoped = getattr(Qgis, "MessageLevel", None)
    if scoped is not None:
        value = getattr(scoped, name, None)
        if value is not None:
            return value

    value = getattr(Qgis, legacy_name, None)
    if value is not None:
        return value

    # 通常ここへ来ることはありませんが、ログ出力自体で例外になることを
    # 避けるためInfo相当を最後の候補として返します。
    fallback = getattr(Qgis, "Info", None)
    return fallback


# -----------------------------------------------------------------------------
# DockWidget
# -----------------------------------------------------------------------------

class UtmGridDockWidget(QDockWidget):
    """ユーザーがメッシュサイズと描画範囲を指定するDockWidgetです。"""

    # DockWidgetが閉じられたことをプラグイン本体へ伝える独自シグナルです。
    closing = pyqtSignal()

    def __init__(self, canvas, parent=None):
        super().__init__("UTMグリッド作成", parent)

        if canvas is None:
            raise ValueError("QGISのマップキャンバスを取得できませんでした。")

        self.setObjectName("KKCreateUTMGridDockWidget")
        self.canvas = canvas

        # DockWidgetの中身となる親Widgetを作ります。
        body = QWidget(self)
        layout = QVBoxLayout(body)

        # -----------------------------------------------------------------
        # メッシュサイズ選択
        # -----------------------------------------------------------------
        form = QFormLayout()
        self.size_combo = QComboBox()
        self.size_combo.addItems(["10km", "1km", "100m", "10m", "1m"])
        self.size_combo.setCurrentText("1km")
        form.addRow("メッシュサイズ:", self.size_combo)
        layout.addLayout(form)

        # -----------------------------------------------------------------
        # QGIS標準の描画範囲指定コントロール
        # -----------------------------------------------------------------
        self.extent_group = QgsExtentGroupBox(body)
        self.extent_group.setTitleBase("描画領域")
        self.extent_group.setCheckable(False)
        self.extent_group.setMapCanvas(self.canvas)

        # 起動時は現在のマップキャンバスのCRSと表示範囲を初期値にします。
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        if not canvas_crs.isValid():
            raise ValueError("マップキャンバスのCRSが正しくありません。")

        canvas_extent = QgsRectangle(self.canvas.extent())
        if canvas_extent.isEmpty():
            raise ValueError("マップキャンバスの表示範囲を取得できませんでした。")

        self.extent_group.setOutputCrs(canvas_crs)
        self.extent_group.setOriginalExtent(canvas_extent, canvas_crs)
        self.extent_group.setCurrentExtent(canvas_extent, canvas_crs)
        layout.addWidget(self.extent_group)

        # -----------------------------------------------------------------
        # 実行ボタン
        # -----------------------------------------------------------------
        self.create_button = QPushButton("グリッド作成")
        self.create_button.setMinimumHeight(34)
        layout.addWidget(self.create_button)

        # 処理状況やエラー概要をパネル内にも表示します。
        self.status_label = QLabel("描画領域を指定してください。")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)
        self.setWidget(body)

    def extent(self):
        """ユーザーが範囲コントロールで指定した矩形を返します。"""
        try:
            return QgsRectangle(self.extent_group.outputExtent())
        except Exception as exception:
            raise RuntimeError("描画領域を取得できませんでした。") from exception

    def output_crs(self):
        """描画範囲の座標参照系を返します。"""
        try:
            return self.extent_group.outputCrs()
        except Exception as exception:
            raise RuntimeError("描画領域のCRSを取得できませんでした。") from exception

    def synchronize_canvas_crs(self):
        """マップキャンバスのCRS変更を範囲コントロールへ反映します。"""
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        if not canvas_crs.isValid():
            raise ValueError("マップキャンバスのCRSが正しくありません。")
        self.extent_group.setOutputCrs(canvas_crs)

    def closeEvent(self, event):
        """パネルを閉じたときにclosingシグナルを送信します。"""
        self.closing.emit()
        super().closeEvent(event)


# -----------------------------------------------------------------------------
# MGRS / UTMグリッド作成処理
# -----------------------------------------------------------------------------

class MgrsGridBuilder:
    """座標計算とMGRSグリッド生成を担当するクラスです。"""

    # MGRS緯度帯文字です。IとOは数字との混同を避けるため使いません。
    LATITUDE_BANDS = "CDEFGHJKLMNPQRSTUVWX"

    # MGRS 100km正方形の北距方向で使う文字列です。
    NORTHING_LETTERS = "ABCDEFGHJKLMNPQRSTUV"

    # UTMゾーン番号を3で割った余りに応じた東距側の100km文字セットです。
    EASTING_SETS = {
        1: "ABCDEFGH",
        2: "JKLMNPQR",
        0: "STUVWXYZ",
    }

    # 表示名: (一辺の長さ[m], MGRS数値部の桁数)
    OPTIONS = {
        "10km": (10000, 1),
        "1km": (1000, 2),
        "100m": (100, 3),
        "10m": (10, 4),
        "1m": (1, 5),
    }

    def __init__(self, project):
        if project is None:
            raise ValueError("QGISプロジェクトを取得できませんでした。")

        self.project = project
        self.wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if not self.wgs84.isValid():
            raise RuntimeError("EPSG:4326を初期化できませんでした。")

        self.context = project.transformContext()

    @staticmethod
    def zone_for(longitude, latitude):
        """
        経度・緯度から使用するUTMゾーン番号(1～60)を返します。

        一般的な6度幅のUTMゾーンに加え、ノルウェーと
        スヴァールバルの特殊ゾーン規則も反映します。
        """
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            raise ValueError("経度または緯度が有限値ではありません。")

        longitude = ((longitude + 180.0) % 360.0) - 180.0
        zone = max(
            1,
            min(60, int(math.floor((longitude + 180.0) / 6.0)) + 1),
        )

        # ノルウェー特殊ゾーン
        if 56.0 <= latitude < 64.0 and 3.0 <= longitude < 12.0:
            zone = 32

        # スヴァールバル特殊ゾーン
        if 72.0 <= latitude < 84.0:
            if 0.0 <= longitude < 9.0:
                zone = 31
            elif 9.0 <= longitude < 21.0:
                zone = 33
            elif 21.0 <= longitude < 33.0:
                zone = 35
            elif 33.0 <= longitude < 42.0:
                zone = 37

        return zone

    @classmethod
    def latitude_band(cls, latitude):
        """緯度からMGRSの緯度帯文字を返します。"""
        if not math.isfinite(latitude):
            raise ValueError("緯度が有限値ではありません。")
        if latitude < -80.0 or latitude > 84.0:
            raise ValueError("MGRS対応緯度外です。")
        if latitude >= 72.0:
            return "X"

        index = int(math.floor((latitude + 80.0) / 8.0))
        return cls.LATITUDE_BANDS[max(0, min(19, index))]

    @classmethod
    def mgrs(cls, zone, latitude, easting, northing, precision):
        """
        1セルの左下UTM座標からMGRSコードを組み立てます。

        戻り値:
            code        空白なしMGRSコード
            spaced      読みやすく空白を入れたMGRSコード
            band        緯度帯
            square      100km正方形文字
            easting_id  東距の数値部
            northing_id 北距の数値部
        """
        if zone < 1 or zone > 60:
            raise ValueError(f"UTMゾーン番号が不正です: {zone}")
        if precision not in (1, 2, 3, 4, 5):
            raise ValueError(f"MGRS精度が不正です: {precision}")
        if not all(math.isfinite(v) for v in (latitude, easting, northing)):
            raise ValueError("MGRS計算に有限値ではない座標が渡されました。")

        # UTM東距を100km単位に区切り、対応する文字を決めます。
        column = int(math.floor(easting / 100000.0))
        if column < 1 or column > 8:
            raise ValueError(f"UTM東距がMGRS有効範囲外です: {easting}")

        column_letter = cls.EASTING_SETS[zone % 3][column - 1]

        # 北距方向の文字は20文字周期です。
        row = int(math.floor(northing / 100000.0)) % 20
        if zone % 2 == 0:
            row = (row + 5) % 20
        square = column_letter + cls.NORTHING_LETTERS[row]

        # 100km正方形内の東距・北距を必要な桁数に丸めます。
        divisor = 10 ** (5 - precision)
        easting_id = (int(math.floor(easting + 0.000001)) % 100000) // divisor
        northing_id = (int(math.floor(northing + 0.000001)) % 100000) // divisor
        easting_text = f"{easting_id:0{precision}d}"
        northing_text = f"{northing_id:0{precision}d}"
        band = cls.latitude_band(latitude)

        code = f"{zone:02d}{band}{square}{easting_text}{northing_text}"
        spaced = f"{zone:02d}{band} {square} {easting_text} {northing_text}"
        return code, spaced, band, square, easting_id, northing_id

    @staticmethod
    def rectangle(x_min, y_min, x_max, y_max):
        """4辺が座標軸に平行な矩形ポリゴンを作ります。"""
        if not all(math.isfinite(v) for v in (x_min, y_min, x_max, y_max)):
            raise ValueError("矩形作成用の座標が有限値ではありません。")
        if x_max <= x_min or y_max <= y_min:
            raise ValueError("矩形の幅または高さが0以下です。")

        # ポリゴンの外周は開始点と終了点を同じにして閉じます。
        ring = [
            QgsPointXY(x_min, y_min),
            QgsPointXY(x_max, y_min),
            QgsPointXY(x_max, y_max),
            QgsPointXY(x_min, y_max),
            QgsPointXY(x_min, y_min),
        ]
        geometry = QgsGeometry.fromPolygonXY([ring])
        if geometry.isEmpty():
            raise RuntimeError("矩形ジオメトリを作成できませんでした。")
        return geometry

    def _prepare_extent(self, extent, source_crs):
        """
        ユーザー指定範囲をWGS84へ変換し、後続処理に必要な情報を整理します。
        """
        if extent is None or extent.isEmpty():
            raise ValueError("描画範囲が空です。")
        if extent.width() <= 0 or extent.height() <= 0:
            raise ValueError("描画範囲の幅または高さが0以下です。")
        if source_crs is None or not source_crs.isValid():
            raise ValueError("描画範囲のCRSが正しくありません。")

        try:
            to_wgs84 = QgsCoordinateTransform(
                source_crs,
                self.wgs84,
                self.context,
            )
            extent_geometry = QgsGeometry.fromRect(extent)
            extent_geometry.transform(to_wgs84)
        except Exception as exception:
            raise RuntimeError(
                "描画範囲をEPSG:4326へ座標変換できませんでした。"
            ) from exception

        if extent_geometry.isEmpty():
            raise RuntimeError("座標変換後の描画範囲が空になりました。")

        bbox = extent_geometry.boundingBox()
        min_lon = bbox.xMinimum()
        max_lon = bbox.xMaximum()
        min_lat = bbox.yMinimum()
        max_lat = bbox.yMaximum()

        if not all(
            math.isfinite(v)
            for v in (min_lon, max_lon, min_lat, max_lat)
        ):
            raise ValueError("座標変換後の描画範囲に不正な数値があります。")

        if min_lat < -80.0 or max_lat > 84.0:
            raise ValueError(
                "指定範囲はMGRS対応緯度（南緯80度～北緯84度）を超えています。"
            )
        if max_lon - min_lon > 180.0:
            raise ValueError("日付変更線をまたぐ範囲には対応していません。")

        # 範囲中央緯度を使い、東西端が属する標準UTMゾーンを調べます。
        center_lat = (min_lat + max_lat) / 2.0
        start_zone = self.zone_for(min_lon, center_lat)
        end_zone = self.zone_for(max_lon, center_lat)
        zones = list(
            range(
                min(start_zone, end_zone),
                max(start_zone, end_zone) + 1,
            )
        )

        # ノルウェー・スヴァールバルの特殊ゾーン候補も追加します。
        # 実際に地物を作るかどうかは後段の中心点判定で決まります。
        for special in (31, 32, 33, 35, 37):
            special_min = (special - 1) * 6 - 180
            special_max = special * 6 - 180
            if (
                special not in zones
                and special_min <= max_lon
                and special_max >= min_lon
            ):
                zones.append(special)
        zones.sort()

        if not zones:
            raise RuntimeError("処理対象のUTMゾーンを決定できませんでした。")

        # 北半球・南半球でUTM EPSGコードが異なるので分けて処理します。
        # True=北半球、False=南半球です。
        hemispheres = []
        if min_lat < 0.0:
            hemispheres.append(False)
        if max_lat >= 0.0:
            hemispheres.append(True)
        if not hemispheres:
            raise RuntimeError("処理対象の半球を決定できませんでした。")

        return (
            extent_geometry,
            min_lon,
            max_lon,
            min_lat,
            max_lat,
            zones,
            hemispheres,
        )

    def _grid_part(self, extent_geometry, zone, north, grid_m):
        """
        1つのUTMゾーン・半球について、グリッド走査範囲を求めます。

        北半球はEPSG:326xx、南半球はEPSG:327xxです。
        """
        if grid_m <= 0:
            raise ValueError("グリッドサイズは0より大きい必要があります。")
        if zone < 1 or zone > 60:
            raise ValueError(f"UTMゾーン番号が不正です: {zone}")

        epsg = (32600 if north else 32700) + zone
        utm_crs = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
        if not utm_crs.isValid():
            raise ValueError(f"EPSG:{epsg}を作成できません。")

        try:
            wgs84_to_utm = QgsCoordinateTransform(
                self.wgs84,
                utm_crs,
                self.context,
            )
            utm_to_wgs84 = QgsCoordinateTransform(
                utm_crs,
                self.wgs84,
                self.context,
            )
            utm_extent = QgsGeometry(extent_geometry)
            utm_extent.transform(wgs84_to_utm)
        except Exception as exception:
            raise RuntimeError(
                f"UTMゾーン{zone}（EPSG:{epsg}）への座標変換に失敗しました。"
            ) from exception

        if utm_extent.isEmpty():
            raise RuntimeError(f"UTMゾーン{zone}の変換後範囲が空です。")

        bbox = utm_extent.boundingBox()

        # グリッド線がgrid_mの整数倍に揃うよう、外側へ切り下げ・切り上げます。
        x_min = int(math.floor(bbox.xMinimum() / grid_m) * grid_m)
        x_max = int(math.ceil(bbox.xMaximum() / grid_m) * grid_m)
        y_min = int(math.floor(bbox.yMinimum() / grid_m) * grid_m)
        y_max = int(math.ceil(bbox.yMaximum() / grid_m) * grid_m)

        if x_max <= x_min or y_max <= y_min:
            raise RuntimeError(f"UTMゾーン{zone}のグリッド走査範囲が不正です。")

        return epsg, utm_to_wgs84, x_min, x_max, y_min, y_max

    def _row_columns_for_zone(
        self,
        zone,
        north,
        grid_m,
        utm_to_wgs84,
        x_min,
        x_max,
        y_value,
        min_lon,
        max_lon,
        min_lat,
        max_lat,
    ):
        """
        1行分について、指定UTMゾーンで生成するX座標の一覧を返します。

        重要な処理:
        UTMゾーンの境界は、地図上へ投影するとグリッド線に対して斜めに
        見える場合があります。中心点が自ゾーン内にあるセルだけを採用すると、
        2ゾーンの間に細い三角形状の空白ができる可能性があります。

        そのため各行で自ゾーンに属する最西列・最東列を見つけ、さらに
        西へ1列、東へ1列追加します。隣接UTMゾーン同士でグリッドが
        重なりますが、境界の空白を防ぐことを優先しています。
        """
        if x_max <= x_min:
            return []

        zone_columns = []
        x_value = x_min

        while x_value < x_max:
            try:
                center = utm_to_wgs84.transform(
                    QgsPointXY(
                        x_value + grid_m / 2.0,
                        y_value + grid_m / 2.0,
                    )
                )
            except Exception as exception:
                raise RuntimeError(
                    f"UTMゾーン{zone}のセル中心座標変換に失敗しました。"
                ) from exception

            if not math.isfinite(center.x()) or not math.isfinite(center.y()):
                x_value += grid_m
                continue

            # 中心点がユーザー範囲の内側か、また北/南半球が一致するかを調べます。
            inside_extent = (
                min_lat <= center.y() <= max_lat
                and min_lon <= center.x() <= max_lon
                and ((center.y() >= 0.0) == north)
            )

            if inside_extent and self.zone_for(center.x(), center.y()) == zone:
                zone_columns.append(x_value)

            x_value += grid_m

        # この緯度行では対象ゾーンに属するセルが無い場合です。
        if not zone_columns:
            return []

        # 本来の範囲の東西へ1セルずつ余分に追加します。
        first_x = min(zone_columns) - grid_m
        last_x = max(zone_columns) + grid_m

        result = []
        x_value = first_x
        while x_value <= last_x:
            result.append(x_value)
            x_value += grid_m

        return result

    def estimate_feature_count(self, extent, source_crs, label, stop_after=None):
        """
        実生成前に、おおよその作成地物数を本処理と同じ走査方法で数えます。

        stop_afterを指定した場合、その件数を超えた時点で終了します。
        200,000件を超えるか確認するだけなら、全件を数える必要がないためです。
        """
        if label not in self.OPTIONS:
            raise ValueError(f"未対応のメッシュサイズです: {label}")
        if stop_after is not None and stop_after < 0:
            raise ValueError("stop_afterは0以上で指定してください。")

        grid_m = self.OPTIONS[label][0]
        (
            extent_geometry,
            min_lon,
            max_lon,
            min_lat,
            max_lat,
            zones,
            hemispheres,
        ) = self._prepare_extent(extent, source_crs)

        count = 0
        checked_candidates = 0

        for zone in zones:
            for north in hemispheres:
                try:
                    (
                        _epsg,
                        utm_to_wgs84,
                        x_min,
                        x_max,
                        y_min,
                        y_max,
                    ) = self._grid_part(extent_geometry, zone, north, grid_m)
                except Exception as exception:
                    raise RuntimeError(
                        f"UTMゾーン{zone}の事前件数計算を開始できませんでした。"
                    ) from exception

                y_value = y_min
                while y_value < y_max:
                    row_columns = self._row_columns_for_zone(
                        zone,
                        north,
                        grid_m,
                        utm_to_wgs84,
                        x_min,
                        x_max,
                        y_value,
                        min_lon,
                        max_lon,
                        min_lat,
                        max_lat,
                    )

                    for _x_value in row_columns:
                        checked_candidates += 1
                        count += 1

                        # 異常な巨大範囲に対する安全弁です。
                        if checked_candidates > MAX_CANDIDATE_CELL_COUNT:
                            return MAX_CANDIDATE_CELL_COUNT + 1

                        # 警告判定に必要な数を超えた時点で早期終了します。
                        if stop_after is not None and count > stop_after:
                            return count

                    y_value += grid_m

        return count

    @staticmethod
    def output_fields():
        """出力グリッドレイヤの属性フィールド定義を作成します。"""
        fields = QgsFields()

        # MGRSコード関係
        fields.append(QgsField("mgrs", QVariant.String, len=24))
        fields.append(QgsField("mgrs_space", QVariant.String, len=30))

        # メッシュサイズ関係
        fields.append(QgsField("grid_size", QVariant.String, len=10))
        fields.append(QgsField("grid_m", QVariant.Int))
        fields.append(QgsField("precision", QVariant.Int))

        # UTM/MGRS位置情報
        fields.append(QgsField("zone", QVariant.Int))
        fields.append(QgsField("band", QVariant.String, len=1))
        fields.append(QgsField("square", QVariant.String, len=2))
        fields.append(QgsField("easting_id", QVariant.Int))
        fields.append(QgsField("northing_id", QVariant.Int))
        fields.append(QgsField("utm_epsg", QVariant.Int))
        fields.append(QgsField("easting", QVariant.Int))
        fields.append(QgsField("northing", QVariant.Int))

        return fields

    def build(self, extent, source_crs, label, progress_callback=None):
        """
        実際のMGRSグリッドを作成します。

        UTMゾーンをまたぐ場合はゾーンごとに別レイヤを作り、
        例: 55_10m_UTMグリッド のような名前を付けます。
        """
        if label not in self.OPTIONS:
            raise ValueError(f"未対応のメッシュサイズです: {label}")

        grid_m, precision = self.OPTIONS[label]
        (
            extent_geometry,
            min_lon,
            max_lon,
            min_lat,
            max_lat,
            zones,
            hemispheres,
        ) = self._prepare_extent(extent, source_crs)

        fields = self.output_fields()
        layers = []
        total_feature_count = 0

        # 進捗率計算用。ゾーン×半球を1処理単位とします。
        parts = max(1, len(zones) * len(hemispheres))
        part_number = 0
        checked_candidates = 0

        for zone in zones:
            # ゾーンごとに独立したメモリレイヤを作ります。
            layer = QgsVectorLayer(
                "Polygon?crs=EPSG:4326",
                f"{zone:02d}_{label}_UTMグリッド",
                "memory",
            )
            if not layer.isValid():
                raise RuntimeError(
                    f"UTMゾーン{zone}の一時レイヤを作成できませんでした。"
                )

            provider = layer.dataProvider()
            if provider is None:
                raise RuntimeError(
                    f"UTMゾーン{zone}のデータプロバイダを取得できませんでした。"
                )

            # 属性フィールドをメモリレイヤへ追加します。
            add_fields_result = provider.addAttributes(list(fields))
            if add_fields_result is False:
                raise RuntimeError(
                    f"UTMゾーン{zone}の属性フィールド追加に失敗しました。"
                )
            layer.updateFields()

            # 5000件単位でまとめてproviderへ書き込むためのバッファです。
            features = []

            # 同一ゾーン内で同一MGRSコードを重複作成しないための集合です。
            used_codes = set()

            for north in hemispheres:
                part_number += 1

                try:
                    (
                        epsg,
                        utm_to_wgs84,
                        x_min,
                        x_max,
                        y_min,
                        y_max,
                    ) = self._grid_part(extent_geometry, zone, north, grid_m)
                except Exception as exception:
                    raise RuntimeError(
                        f"UTMゾーン{zone}のグリッド範囲計算に失敗しました。"
                    ) from exception

                rows = max(1, (y_max - y_min) // grid_m)
                processed_rows = 0
                y_value = y_min

                # 南から北へ1行ずつ処理します。
                while y_value < y_max:
                    processed_rows += 1

                    # この行で対象ゾーンに属する列と、境界用の東西追加列を取得します。
                    row_columns = self._row_columns_for_zone(
                        zone,
                        north,
                        grid_m,
                        utm_to_wgs84,
                        x_min,
                        x_max,
                        y_value,
                        min_lon,
                        max_lon,
                        min_lat,
                        max_lat,
                    )

                    for x_value in row_columns:
                        checked_candidates += 1

                        # 生成候補が過大ならメモリ不足や長時間停止を避けるため中断します。
                        if checked_candidates > MAX_CANDIDATE_CELL_COUNT:
                            raise ValueError(
                                "推定候補セル数が2,000,000件を超えました。"
                                "範囲を狭くするか、粗いメッシュを選択してください。"
                            )

                        try:
                            # セル中心を緯度経度へ変換します。
                            center = utm_to_wgs84.transform(
                                QgsPointXY(
                                    x_value + grid_m / 2.0,
                                    y_value + grid_m / 2.0,
                                )
                            )

                            if not math.isfinite(center.x()) or not math.isfinite(center.y()):
                                continue

                            # MGRS属性を計算します。
                            mgrs_data = self.mgrs(
                                zone,
                                center.y(),
                                x_value,
                                y_value,
                                precision,
                            )
                            code = mgrs_data[0]

                            # 同じMGRSコードを既に作成済みならスキップします。
                            if code in used_codes:
                                continue

                            # UTMメートル座標上で正方形を作り、WGS84へ変換します。
                            geometry = self.rectangle(
                                x_value,
                                y_value,
                                x_value + grid_m,
                                y_value + grid_m,
                            )
                            geometry.transform(utm_to_wgs84)

                            if geometry.isEmpty():
                                continue

                            # 追加した境界列が指定範囲から完全に外れている場合は除外します。
                            if not geometry.intersects(extent_geometry):
                                continue

                            feature = QgsFeature(layer.fields())
                            feature.setGeometry(geometry)
                            feature.setAttributes([
                                mgrs_data[0],       # mgrs
                                mgrs_data[1],       # mgrs_space
                                label,              # grid_size
                                grid_m,             # grid_m
                                precision,          # precision
                                zone,               # zone
                                mgrs_data[2],       # band
                                mgrs_data[3],       # square
                                mgrs_data[4],       # easting_id
                                mgrs_data[5],       # northing_id
                                epsg,               # utm_epsg
                                x_value,            # easting
                                y_value,            # northing
                            ])

                            if not feature.hasGeometry():
                                continue

                            features.append(feature)
                            used_codes.add(code)

                            # 一度に大量のQgsFeatureを保持しないよう5000件ごとに書き込みます。
                            if len(features) >= 5000:
                                added = provider.addFeatures(features)
                                if added is False:
                                    raise RuntimeError(
                                        f"UTMゾーン{zone}の地物追加に失敗しました。"
                                    )
                                features.clear()

                        except ValueError:
                            # MGRSとして有効ではないUTM位置は単一セルだけスキップします。
                            # それ以外の予期しない例外は握り潰さず上位へ送ります。
                            continue
                        except Exception as exception:
                            raise RuntimeError(
                                f"UTMゾーン{zone}、E={x_value}、N={y_value}の"
                                "セル作成中にエラーが発生しました。"
                            ) from exception

                    # DockWidgetへ処理進捗を通知します。
                    if progress_callback:
                        try:
                            progress_value = int(
                                (
                                    (part_number - 1 + processed_rows / rows)
                                    / parts
                                )
                                * 100
                            )
                            progress_callback(max(0, min(100, progress_value)))
                        except Exception as exception:
                            raise RuntimeError("進捗表示の更新に失敗しました。") from exception

                    y_value += grid_m

            # 5000件未満でバッファに残った地物を書き込みます。
            if features:
                added = provider.addFeatures(features)
                if added is False:
                    raise RuntimeError(
                        f"UTMゾーン{zone}の最終地物追加に失敗しました。"
                    )
                features.clear()

            layer.updateExtents()

            # 地物が1件もないゾーンはプロジェクトへ追加しません。
            zone_feature_count = layer.featureCount()
            if zone_feature_count > 0:
                layers.append(layer)
                total_feature_count += zone_feature_count

        if not layers:
            raise RuntimeError("指定範囲内に作成可能なMGRSグリッドがありませんでした。")

        return layers, total_feature_count


# -----------------------------------------------------------------------------
# QGISプラグイン本体
# -----------------------------------------------------------------------------

class KKCreateUtmGridPlugin:
    """QGISから呼び出されるプラグイン本体クラスです。"""

    def __init__(self, iface):
        if iface is None:
            raise ValueError("QGIS ifaceを取得できませんでした。")

        self.iface = iface
        self.canvas = iface.mapCanvas()
        if self.canvas is None:
            raise RuntimeError("QGISマップキャンバスを取得できませんでした。")

        # __file__があるフォルダがプラグインフォルダです。
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # initGuiより前はまだQAction/DockWidgetを作りません。
        self.action = None
        self.dock = None

    def initGui(self):
        """QGISへツールバーアクションを登録します。"""
        try:
            icon_path = os.path.join(self.plugin_dir, "icon.svg")

            # アイコンがなくてもQAction自体は作れるため、ログで警告して継続します。
            if os.path.isfile(icon_path):
                icon = QIcon(icon_path)
            else:
                icon = QIcon()
                QgsMessageLog.logMessage(
                    f"アイコンファイルが見つかりません: {icon_path}",
                    LOG_TAG,
                    message_level("Warning", "Warning"),
                )

            self.action = QAction(
                icon,
                "UTMグリッド作成",
                self.iface.mainWindow(),
            )
            self.action.setObjectName("KKCreateUTMGridAction")
            self.action.triggered.connect(self.run)

            # QGISプラグインツールバーとVectorメニューへ登録します。
            self.iface.addToolBarIcon(self.action)
            self.iface.addPluginToVectorMenu("KK_Create_UTM_Grid", self.action)

        except Exception as exception:
            # initGui中はDockWidgetが無い可能性があるため、ログとメッセージバーへ通知します。
            self.report_error("プラグインの初期化に失敗しました。", exception)

    def run(self):
        """ツールバーの「UTMグリッド作成」が押されたときにパネルを表示します。"""
        try:
            # 初回だけDockWidgetを作り、2回目以降は同じDockWidgetを再表示します。
            if self.dock is None:
                self.dock = UtmGridDockWidget(
                    self.canvas,
                    self.iface.mainWindow(),
                )

                self.iface.addDockWidget(
                    Qt.DockWidgetArea.RightDockWidgetArea,
                    self.dock,
                )

                # GUIイベントをプラグイン処理へ接続します。
                self.dock.create_button.clicked.connect(self.create_grid)
                self.dock.closing.connect(self._dock_closed)
                self.canvas.destinationCrsChanged.connect(
                    self._canvas_crs_changed
                )

            # 現在のキャンバスCRSと範囲コントロールを同期して表示します。
            self.dock.synchronize_canvas_crs()
            self.dock.show()
            self.dock.raise_()

        except Exception as exception:
            self.report_error("パネルの表示に失敗しました。", exception)

    def _canvas_crs_changed(self):
        """ユーザーがプロジェクトCRSを変更したときに呼ばれます。"""
        if self.dock is None:
            return

        try:
            self.dock.synchronize_canvas_crs()
        except Exception as exception:
            self.report_error("キャンバスCRSの同期に失敗しました。", exception)

    def confirm_large_feature_count(self, predicted_count, grid_label):
        """
        200,000地物を超えると予測される場合、ユーザーへ続行確認します。

        「はい」ならTrue、「いいえ」ならFalseを返します。
        """
        if predicted_count < 0:
            raise ValueError("予測地物数が0未満です。")

        if predicted_count <= WARNING_FEATURE_COUNT:
            return True

        text = (
            "描画範囲が大きく作成に時間がかかります。"
            "このままの範囲で描画しますか？\n\n"
            f"選択メッシュ: {grid_label}\n"
            f"確認基準: {WARNING_FEATURE_COUNT:,}個超"
        )

        # QMessageBox.question()を使用し、Yes/Noボタンを明示します。
        # 禁止条件のPython組み込み実行関数は使用しません。
        try:
            answer = QMessageBox.question(
                self.iface.mainWindow(),
                "描画範囲の確認",
                text,
                (
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                ),
                QMessageBox.StandardButton.No,
            )
        except Exception as exception:
            raise RuntimeError("確認ダイアログを表示できませんでした。") from exception

        return answer == QMessageBox.StandardButton.Yes

    @staticmethod
    def _required_grid_fields():
        """既存レイヤへ追加するために必要な属性フィールド名を返します。"""
        return (
            "mgrs",
            "mgrs_space",
            "grid_size",
            "grid_m",
            "precision",
            "zone",
            "band",
            "square",
            "easting_id",
            "northing_id",
            "utm_epsg",
            "easting",
            "northing",
        )

    def _find_existing_grid_layer(self, layer_name):
        """
        プロジェクト内から完全に同じ名前の互換レイヤを探します。

        同名レイヤが複数ある場合、必要フィールドを持つ最初の
        ポリゴンベクタレイヤを使用し、曖昧さをログへ記録します。
        """
        project = QgsProject.instance()
        if project is None:
            raise RuntimeError("QGISプロジェクトを取得できませんでした。")

        same_name_layers = project.mapLayersByName(layer_name)
        if not same_name_layers:
            return None

        required_fields = set(self._required_grid_fields())
        compatible_layers = []

        for candidate in same_name_layers:
            if not isinstance(candidate, QgsVectorLayer):
                continue
            if not candidate.isValid():
                continue
            # QGIS 3.34とQGIS 4の列挙型差を避けるため、
            # QgsWkbTypes.PolygonGeometryを基準に判定します。
            if candidate.geometryType() != QgsWkbTypes.PolygonGeometry:
                continue

            candidate_fields = {field.name() for field in candidate.fields()}
            if required_fields.issubset(candidate_fields):
                compatible_layers.append(candidate)

        if not compatible_layers:
            raise RuntimeError(
                f"同名レイヤ「{layer_name}」は存在しますが、"
                "プラグインが必要とするポリゴン形状または属性フィールドを"
                "備えていません。既存レイヤ名を変更してから再実行してください。"
            )

        if len(compatible_layers) > 1:
            QgsMessageLog.logMessage(
                f"同名の互換レイヤ「{layer_name}」が複数あります。"
                f"最初のレイヤID={compatible_layers[0].id()}へ追加します。",
                LOG_TAG,
                message_level("Warning", "Warning"),
            )

        return compatible_layers[0]

    @staticmethod
    def _feature_grid_key(feature):
        """
        グリッド位置を一意に識別するキーを返します。

        MGRS文字列だけでなくUTM EPSG、東距、北距、メッシュサイズも
        含めることで、同じ位置・同じサイズのセルを確実に判定します。
        """
        try:
            return (
                int(feature["utm_epsg"]),
                int(feature["easting"]),
                int(feature["northing"]),
                int(feature["grid_m"]),
            )
        except (KeyError, TypeError, ValueError) as exception:
            raise RuntimeError(
                "既存または追加予定地物からグリッド位置キーを取得できませんでした。"
            ) from exception

    def _existing_grid_keys(self, layer):
        """既存レイヤに保存されている全グリッド位置キーを集合で返します。"""
        keys = set()
        try:
            for feature in layer.getFeatures():
                keys.add(self._feature_grid_key(feature))
        except Exception as exception:
            raise RuntimeError(
                f"既存レイヤ「{layer.name()}」の地物を読み取れませんでした。"
            ) from exception
        return keys

    def _copy_feature_to_layer(self, source_feature, source_layer, target_layer):
        """
        生成レイヤの地物を既存レイヤのフィールド順・CRSへ合わせて複製します。
        """
        target_feature = QgsFeature(target_layer.fields())
        geometry = QgsGeometry(source_feature.geometry())

        if geometry.isEmpty():
            raise ValueError("追加予定地物のジオメトリが空です。")

        # 通常は双方EPSG:4326ですが、既存レイヤが別CRSの場合にも対応します。
        if source_layer.crs() != target_layer.crs():
            if not target_layer.crs().isValid():
                raise ValueError(
                    f"既存レイヤ「{target_layer.name()}」のCRSが正しくありません。"
                )
            try:
                coordinate_transform = QgsCoordinateTransform(
                    source_layer.crs(),
                    target_layer.crs(),
                    QgsProject.instance().transformContext(),
                )
                geometry.transform(coordinate_transform)
            except Exception as exception:
                raise RuntimeError(
                    f"追加地物を既存レイヤ「{target_layer.name()}」のCRSへ"
                    "変換できませんでした。"
                ) from exception

        target_feature.setGeometry(geometry)

        source_field_names = {field.name() for field in source_layer.fields()}
        attributes = []
        for target_field in target_layer.fields():
            field_name = target_field.name()
            if field_name in source_field_names:
                attributes.append(source_feature[field_name])
            else:
                attributes.append(None)
        target_feature.setAttributes(attributes)
        return target_feature

    def _append_only_new_features(self, generated_layer, existing_layer):
        """
        生成レイヤから既存レイヤに無い位置の地物だけを追加します。

        戻り値は今回追加した地物数です。既存レイヤが編集状態でなければ
        このメソッド内で編集開始・コミットします。既に編集状態の場合は、
        ユーザーの編集中セッションを維持したまま追加します。
        """
        existing_keys = self._existing_grid_keys(existing_layer)
        was_editable = existing_layer.isEditable()
        started_editing_here = False

        if not was_editable:
            if not existing_layer.startEditing():
                raise RuntimeError(
                    f"既存レイヤ「{existing_layer.name()}」を編集モードにできません。"
                )
            started_editing_here = True

        added_count = 0
        pending_features = []

        try:
            for source_feature in generated_layer.getFeatures():
                key = self._feature_grid_key(source_feature)

                # 同じ位置のグリッドが既にある場合は追加しません。
                if key in existing_keys:
                    continue

                target_feature = self._copy_feature_to_layer(
                    source_feature,
                    generated_layer,
                    existing_layer,
                )
                pending_features.append(target_feature)
                existing_keys.add(key)

                # 大量追加時のメモリ使用量を抑えるため5000件ずつ追加します。
                if len(pending_features) >= 5000:
                    if not existing_layer.addFeatures(pending_features):
                        raise RuntimeError(
                            f"既存レイヤ「{existing_layer.name()}」への地物追加に失敗しました。"
                        )
                    added_count += len(pending_features)
                    pending_features.clear()

            if pending_features:
                if not existing_layer.addFeatures(pending_features):
                    raise RuntimeError(
                        f"既存レイヤ「{existing_layer.name()}」への最終地物追加に失敗しました。"
                    )
                added_count += len(pending_features)
                pending_features.clear()

            if started_editing_here:
                if not existing_layer.commitChanges():
                    errors = "; ".join(existing_layer.commitErrors())
                    existing_layer.rollBack()
                    raise RuntimeError(
                        f"既存レイヤ「{existing_layer.name()}」の変更を保存できませんでした。"
                        + (f" 詳細: {errors}" if errors else "")
                    )

            existing_layer.updateExtents()
            existing_layer.triggerRepaint()
            return added_count

        except Exception:
            # このメソッドで編集を開始した場合だけロールバックします。
            # 元から編集中の場合は、ユーザーの既存編集内容を消さないため
            # 自動ロールバックせず例外を上位へ送ります。
            if started_editing_here and existing_layer.isEditable():
                existing_layer.rollBack()
            raise

    def create_grid(self):
        """「グリッド作成」ボタンから呼ばれるメイン処理です。"""
        if self.dock is None:
            self.report_error(
                "グリッド作成に失敗しました。",
                RuntimeError("DockWidgetが初期化されていません。"),
            )
            return

        # 二重クリックによる処理の重複起動を防ぐため、できるだけ早く無効化します。
        self.dock.create_button.setEnabled(False)

        try:
            # -----------------------------------------------------------------
            # 1. ユーザー入力の検証
            # -----------------------------------------------------------------
            extent = self.dock.extent()
            extent_crs = self.dock.output_crs()

            if extent.isEmpty() or extent.width() <= 0 or extent.height() <= 0:
                raise ValueError("有効な描画領域が指定されていません。")
            if not extent_crs.isValid():
                raise ValueError("描画領域の座標参照系が正しくありません。")

            label = self.dock.size_combo.currentText()
            if label not in MgrsGridBuilder.OPTIONS:
                raise ValueError(f"未対応のメッシュサイズです: {label}")

            # -----------------------------------------------------------------
            # 2. 事前件数確認
            # -----------------------------------------------------------------
            project = QgsProject.instance()
            if project is None:
                raise RuntimeError("QGISプロジェクトを取得できませんでした。")

            builder = MgrsGridBuilder(project)
            self.dock.status_label.setText("作成予定のグリッド数を確認中です...")

            predicted_count = builder.estimate_feature_count(
                extent,
                extent_crs,
                label,
                stop_after=WARNING_FEATURE_COUNT,
            )

            QgsMessageLog.logMessage(
                f"作成前確認: {label}, 判定用予測地物数={predicted_count:,}",
                LOG_TAG,
                message_level("Info", "Info"),
            )

            if not self.confirm_large_feature_count(predicted_count, label):
                self.dock.status_label.setText("グリッド作成を中止しました。")
                QgsMessageLog.logMessage(
                    "ユーザー操作によりグリッド作成を中止しました。",
                    LOG_TAG,
                    message_level("Info", "Info"),
                )
                return

            # -----------------------------------------------------------------
            # 3. グリッド候補を一時レイヤとして生成
            # -----------------------------------------------------------------
            self.dock.status_label.setText("グリッドを作成中です...")
            generated_layers, _generated_count = builder.build(
                extent,
                extent_crs,
                label,
                self.update_progress,
            )

            if not generated_layers:
                raise RuntimeError("一時レイヤを作成できませんでした。")

            # -----------------------------------------------------------------
            # 4. QMLスタイルの存在確認
            # -----------------------------------------------------------------
            qml_path = os.path.join(self.plugin_dir, "mgrs_style.qml")
            if not os.path.isfile(qml_path):
                raise FileNotFoundError(
                    f"mgrs_style.qml がプラグインフォルダにありません: {qml_path}"
                )

            # -----------------------------------------------------------------
            # 5. 同名レイヤへ追加、または新規レイヤとして登録
            # -----------------------------------------------------------------
            # 同名レイヤがある場合:
            #   既存地物の位置キーと比較し、未作成セルだけを追加します。
            # 同名レイヤがない場合:
            #   生成レイヤへQMLを適用し、新規レイヤとして登録します。
            added_new_layer_ids = []
            affected_layer_names = []
            total_added_count = 0
            skipped_count = 0

            try:
                for generated_layer in generated_layers:
                    existing_layer = self._find_existing_grid_layer(
                        generated_layer.name()
                    )

                    if existing_layer is not None:
                        generated_count = generated_layer.featureCount()
                        added_count = self._append_only_new_features(
                            generated_layer,
                            existing_layer,
                        )
                        total_added_count += added_count
                        skipped_count += max(0, generated_count - added_count)
                        affected_layer_names.append(existing_layer.name())

                        QgsMessageLog.logMessage(
                            f"既存レイヤ「{existing_layer.name()}」へ"
                            f"{added_count:,}件追加し、重複{generated_count - added_count:,}件を"
                            "スキップしました。",
                            LOG_TAG,
                            message_level("Info", "Info"),
                        )
                        continue

                    # 同名レイヤが無い場合だけ、生成レイヤへスタイルを適用します。
                    style_result = generated_layer.loadNamedStyle(qml_path)
                    style_ok = True
                    style_message = ""
                    if isinstance(style_result, tuple):
                        if len(style_result) >= 1:
                            style_message = str(style_result[0])
                        if len(style_result) >= 2:
                            style_ok = bool(style_result[1])
                    elif isinstance(style_result, bool):
                        style_ok = style_result

                    if not style_ok:
                        raise RuntimeError(
                            f"{generated_layer.name()}へのmgrs_style.qmlの読み込みに失敗しました。"
                            + (f" QGISメッセージ: {style_message}" if style_message else "")
                        )

                    if hasattr(generated_layer, "setLabelsEnabled"):
                        generated_layer.setLabelsEnabled(True)

                    added_layer = project.addMapLayer(generated_layer)
                    if added_layer is None:
                        raise RuntimeError(
                            f"{generated_layer.name()}をQGISプロジェクトへ追加できませんでした。"
                        )

                    added_new_layer_ids.append(generated_layer.id())
                    affected_layer_names.append(generated_layer.name())
                    total_added_count += generated_layer.featureCount()
                    generated_layer.triggerRepaint()

            except Exception:
                # 新規追加したレイヤだけをロールバックします。
                # 既存レイヤへの追加は_append_only_new_features()側で管理します。
                for layer_id in added_new_layer_ids:
                    try:
                        project.removeMapLayer(layer_id)
                    except Exception as rollback_exception:
                        QgsMessageLog.logMessage(
                            "エラー後の新規レイヤ削除にも失敗しました: "
                            + str(rollback_exception),
                            LOG_TAG,
                            message_level("Warning", "Warning"),
                        )
                raise

            # -----------------------------------------------------------------
            # 6. 完了通知
            # -----------------------------------------------------------------
            unique_names = list(dict.fromkeys(affected_layer_names))
            layer_names = ", ".join(unique_names)

            if total_added_count == 0:
                success_text = (
                    "指定範囲のグリッドはすべて既存レイヤに存在するため、"
                    f"新しい地物は追加しませんでした。 重複: {skipped_count:,}件"
                )
            else:
                success_text = (
                    f"{len(unique_names)}レイヤへ合計{total_added_count:,}件を追加しました。"
                    f" 重複スキップ: {skipped_count:,}件。"
                    f" 対象: {layer_names}"
                )

            self.dock.status_label.setText(success_text)
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME,
                success_text,
                level=message_level("Success", "Success"),
                duration=5,
            )
            QgsMessageLog.logMessage(
                success_text,
                LOG_TAG,
                message_level("Info", "Info"),
            )

        except Exception as exception:
            # 例外は握り潰さず、ログ・メッセージバー・DockWidgetへ通知します。
            self.report_error("グリッド作成に失敗しました。", exception)

        finally:
            # 成功・失敗・ユーザー中止のどの場合でもボタンを必ず再有効化します。
            if self.dock is not None:
                self.dock.create_button.setEnabled(True)

    def update_progress(self, value):
        """グリッド生成中の進捗率をDockWidgetへ表示します。"""
        if self.dock is None:
            return

        try:
            value = max(0, min(100, int(value)))
            self.dock.status_label.setText(
                f"グリッドを作成中です... {value}%"
            )
        except Exception as exception:
            # 進捗表示だけの失敗でグリッド生成を壊さないよう、ログへ記録します。
            QgsMessageLog.logMessage(
                "進捗表示の更新に失敗しました: " + str(exception),
                LOG_TAG,
                message_level("Warning", "Warning"),
            )

    def report_error(self, message, exception):
        """例外内容をQGISログ、メッセージバー、DockWidgetへ通知します。"""
        error_text = str(exception) if exception is not None else "不明なエラー"
        detail = message + "\n" + error_text

        # traceback.format_exc()はexcept節外では"NoneType: None"になるため、
        # その場合は詳細文字列だけをログへ出します。
        trace_text = traceback.format_exc()
        if trace_text.strip() == "NoneType: None":
            log_text = detail
        else:
            log_text = detail + "\n" + trace_text

        try:
            QgsMessageLog.logMessage(
                log_text,
                LOG_TAG,
                message_level("Critical", "Critical"),
            )
        except Exception as exception:
            logger.warning(
                "QGISメッセージログへのエラー通知に失敗しました",
                exc_info=True,
            )

        try:
            message_bar = self.iface.messageBar()
            if message_bar is not None:
                message_bar.pushMessage(
                    PLUGIN_NAME,
                    detail,
                    level=message_level("Critical", "Critical"),
                    duration=10,
                )
        except Exception as exception:
            logger.warning(
                "QGISメッセージバーへのエラー通知に失敗しました",
                exc_info=True,
            )

        if self.dock is not None:
            try:
                self.dock.status_label.setText(detail)
            except Exception as exception:
                logger.warning(
                    "DockWidgetステータスへのエラー通知に失敗しました",
                    exc_info=True,
                )

    def _dock_closed(self):
        """DockWidgetが閉じられたときのフックです。現在は追加処理不要です。"""
        return

    def unload(self):
        """
        プラグイン無効化時に、登録したシグナル・DockWidget・QActionを解放します。

        QGIS終了時など、既にQtオブジェクトが破棄されている場合があるため、
        個別に例外処理して可能な範囲で最後までクリーンアップします。
        """
        cleanup_errors = []

        # キャンバスCRS変更シグナルを解除します。
        if self.canvas is not None:
            try:
                self.canvas.destinationCrsChanged.disconnect(
                    self._canvas_crs_changed
                )
            except (TypeError, RuntimeError) as exception:
                logger.debug(
                    "キャンバスCRS変更シグナルは既に切断済みです。"
                    + str(exception),
                    exc_info=True,
                )
            except Exception as exception:
                cleanup_errors.append(
                    "destinationCrsChanged解除: " + str(exception)
                )

        # DockWidget関連のシグナルを解除してDockWidgetを削除します。
        if self.dock is not None:
            try:
                self.dock.create_button.clicked.disconnect(self.create_grid)
            except (TypeError, RuntimeError) as exception:
                logger.debug(
                    "create_buttonシグナルは既に切断済みです。"
                    + str(exception),
                    exc_info=True,
                )
            except Exception as exception:
                cleanup_errors.append("create_button解除: " + str(exception))

            try:
                self.dock.closing.disconnect(self._dock_closed)
            except (TypeError, RuntimeError) as exception:
                logger.debug(
                    "DockWidget closingシグナルは既に切断済みです。"
                    + str(exception),
                    exc_info=True,
                )
            except Exception as exception:
                cleanup_errors.append("closing解除: " + str(exception))

            try:
                self.iface.removeDockWidget(self.dock)
                self.dock.deleteLater()
            except Exception as exception:
                cleanup_errors.append("DockWidget削除: " + str(exception))
            finally:
                self.dock = None

        # QActionをツールバーとVectorメニューから外して削除します。
        if self.action is not None:
            try:
                self.action.triggered.disconnect(self.run)
            except (TypeError, RuntimeError) as exception:
                logger.debug(
                    "QActionシグナルは既に切断済みです。"
                    + str(exception),
                    exc_info=True,
                )
            except Exception as exception:
                cleanup_errors.append("actionシグナル解除: " + str(exception))

            try:
                self.iface.removeToolBarIcon(self.action)
            except Exception as exception:
                cleanup_errors.append("ツールバー解除: " + str(exception))

            try:
                self.iface.removePluginVectorMenu(
                    "KK_Create_UTM_Grid",
                    self.action,
                )
            except Exception as exception:
                cleanup_errors.append("Vectorメニュー解除: " + str(exception))

            try:
                self.action.deleteLater()
            except Exception as exception:
                cleanup_errors.append("QAction削除: " + str(exception))
            finally:
                self.action = None

        # cleanup中の問題はプラグイン終了を妨げずログだけへ残します。
        if cleanup_errors:
            try:
                QgsMessageLog.logMessage(
                    "unload中に一部の解放処理で問題が発生しました:\n"
                    + "\n".join(cleanup_errors),
                    LOG_TAG,
                    message_level("Warning", "Warning"),
                )
            except Exception as exception:
                logger.warning(
                    "プラグイン終了時のQGISログ出力に失敗しました",
                    exc_info=True,
                )
