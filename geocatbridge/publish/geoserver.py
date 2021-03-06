import os
import shutil
import json
import webbrowser
from zipfile import ZipFile
import sqlite3
import secrets
from bridgestyle import mapboxgl

from requests.exceptions import ConnectionError

from qgis.core import QgsProject, QgsDataSourceUri

from qgis.PyQt.QtCore import QCoreApplication, QByteArray, QBuffer, QIODevice

from qgis.PyQt.QtWidgets import QMessageBox

from bridgestyle.qgis import saveLayerStyleAsZippedSld, layerStylesAsMapboxFolder

from .exporter import exportLayer
from .serverbase import ServerBase
from ..utils.files import tempFilenameInTempFolder, tempFolderInTempFolder
from ..utils.services import addServicesForGeodataServer


class GeoserverServer(ServerBase):
    FILE_BASED = 0
    POSTGIS_MANAGED_BY_BRIDGE = 1
    POSTGIS_MANAGED_BY_GEOSERVER = 2

    def __init__(
        self,
        name,
        url="",
        authid="",
        storage=0,
        postgisdb=None,
        useOriginalDataSource=False,
        useVectorTiles=False
    ):
        super().__init__()
        self.name = name

        if url:
            if url.endswith("rest"):
                self.url = url.strip("/")
            else:
                self.url = url.strip("/") + "/rest"
        else:
            self.url = url

        self.authid = authid
        self.storage = storage
        self.postgisdb = postgisdb
        self.useOriginalDataSource = useOriginalDataSource
        self.useVectorTiles = useVectorTiles
        self._isMetadataCatalog = False
        self._isDataCatalog = True
        self._layersCache = {}

    @property
    def _workspace(self):
        path = QgsProject.instance().absoluteFilePath()
        if path:
            return os.path.splitext(os.path.basename(path))[0]
        else:
            return ""

    def prepareForPublishing(self, onlySymbology):
        if not onlySymbology:
            self.deleteWorkspace()
        self._ensureWorkspaceExists()
        self._uploadedDatasets = {}
        self._exportedLayers = {}
        self._postgisDatastoreExists = False
        self._publishedLayers = set()

    def closePublishing(self):        
        if self.useVectorTiles:
            folder = tempFolderInTempFolder()            
            warnings = layerStylesAsMapboxFolder(self._publishedLayers, folder)
            for w in warnings:
                self.logWarning(w)
            self._editMapboxFiles(folder)
            self.publishMapboxGLStyle(folder)
            self._publishOpenLayersPreview(folder)

    def _publishOpenLayersPreview(self, folder):
        styleFilename = os.path.join(folder, "style.mapbox")
        with open(styleFilename) as f:
            style = f.read()
        template = "var style = %s;\nvar map = olms.apply('map', style);" % style
        
        jsFilename = os.path.join(folder, "mapbox.js")
        with open(jsFilename, "w") as f:
            f.write(template)
        src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "openlayers", "index.html")
        dst = os.path.join(folder, "index.html")
        shutil.copyfile(src, dst)
        self.uploadResource("%s/index.html" % self._workspace, src)
        self.uploadResource("%s/mapbox.js" % self._workspace, jsFilename)

    def uploadResource(self, path, file):
        with open(file) as f:
            content = f.read()
        url = "%s/resource/%s" % (self.url, path)
        self.request(url, content, "put")

    def _editMapboxFiles(self, folder):        
        filename = os.path.join(folder, "style.mapbox")
        with open(filename) as f:
            mapbox = json.load(f)
        sources = mapbox["sources"]
        for name in sources.keys():
            url = ("%s/gwc/service/wmts?REQUEST=GetTile&SERVICE=WMTS"
                  "&VERSION=1.0.0&LAYER=%s:%s&STYLE=&TILEMATRIX=EPSG:900913:{z}"
                  "&TILEMATRIXSET=EPSG:900913&FORMAT=application/vnd.mapbox-vector-tile"
                  "&TILECOL={x}&TILEROW={y}" % (self.baseUrl(), self._workspace, name))
            sourcedef = {
                  "type": "vector",
                  "tiles": [url],
                  "minZoom": 0,
                  "maxZoom": 14
                }
            sources[name] = sourcedef
        with open(filename, "w") as f:
            json.dump(mapbox, f)

    def publishMapboxGLStyle(self, folder):
        name = "mb_" + self._workspace
        filename = os.path.join(folder, "style.mapbox")
        self._publishStyle(name, filename)

    def publishStyle(self, layer):
        self._publishedLayers.add(layer)
        styleFilename = tempFilenameInTempFolder(layer.name() + ".zip")
        warnings = saveLayerStyleAsZippedSld(layer, styleFilename)
        for w in warnings:
            self.logWarning(w)
        self.logInfo(QCoreApplication.translate("GeocatBridge", "Style for layer %s exported as zip file to %s")
                     % (layer.name(), styleFilename))
        self._publishStyle(layer.name(), styleFilename)
        return styleFilename

    def publishLayer(self, layer, fields=None):        
        if layer.type() == layer.VectorLayer:
            if layer.featureCount() == 0:
                self.logError("Layer contains zero features and cannot be published")
                return

            if layer.dataProvider().name() == "postgres" and self.useOriginalDataSource:
                from .postgis import PostgisServer
                uri = QgsDataSourceUri(layer.source())
                db = PostgisServer("temp", uri.authConfigId(), uri.host(), uri.port(), uri.schema(), uri.database())
                self._publishVectorLayerFromPostgis(layer, db)
            elif self.storage in [self.FILE_BASED, self.POSTGIS_MANAGED_BY_GEOSERVER]:
                if layer.source() not in self._exportedLayers:
                    if self.storage == self.POSTGIS_MANAGED_BY_GEOSERVER:
                        path = exportLayer(layer, fields, toShapefile=True, force=True, log=self)
                        basename = os.path.splitext(path)[0]
                        zipfilename = basename + ".zip"
                        with ZipFile(zipfilename, 'w') as z:
                            for ext in [".shp", ".shx", ".prj", ".dbf"]:
                                filetozip = basename + ext
                                z.write(filetozip, arcname=os.path.basename(filetozip))
                        self._exportedLayers[layer.source()] = zipfilename
                    else:
                        path = exportLayer(layer, fields, log=self)
                        self._exportedLayers[layer.source()] = path
                filename = self._exportedLayers[layer.source()]
                if self.storage == self.FILE_BASED:
                    self._publishVectorLayerFromFile(layer, filename)
                else:
                    self._publishVectorLayerFromFileToPostgis(layer, filename)
            elif self.storage == self.POSTGIS_MANAGED_BY_BRIDGE:
                try:
                    from .servers import allServers
                    db = allServers()[self.postgisdb]
                except KeyError:
                    raise Exception(
                        QCoreApplication.translate("GeocatBridge", "Cannot find the selected PostGIS database"))
                db.importLayer(layer, fields)
                self._publishVectorLayerFromPostgis(layer, db)
        elif layer.type() == layer.RasterLayer:
            if layer.source() not in self._exportedLayers:
                path = exportLayer(layer, fields, log=self)
                self._exportedLayers[layer.source()] = path
            filename = self._exportedLayers[layer.source()]
            self._publishRasterLayer(filename, layer.name())
        self._clearCache()

    def createPostgisDatastore(self):
        ws, name = self.postgisdb.split(":")
        if not self.datastoreExists(name):
            url = "%s/workspaces/%s/datastores/%s.json" % (self.url, ws, name)
            r = self.request(url)
            datastore = r.json()["dataStore"]
            newDatastore = {"dataStore": {"name": datastore["name"],
                                          "type": datastore["type"],
                                          "connectionParameters": datastore["connectionParameters"],
                                          "enabled": True}}
            url = "%s/workspaces/%s/datastores" % (self.url, self._workspace)
            r = self.request(url, newDatastore, "post")

    def testConnection(self):
        try:
            url = "%s/about/version" % self.url
            self.request(url)
            return True
        except:
            return False

    def unpublishData(self, layer):
        self.deleteLayer(layer.name())
        self.deleteStyle(layer.name())

    def baseUrl(self):
        return "/".join(self.url.split("/")[:-1])

    def _publishVectorLayerFromFile(self, layer, filename):
        self.logInfo("Publishing layer from file: %s" % filename)
        name = layer.name()
        isDataUploaded = filename in self._uploadedDatasets
        if not isDataUploaded:
            with open(filename, "rb") as f:
                self._deleteDatastore(name)
                url = "%s/workspaces/%s/datastores/%s/file.gpkg?update=overwrite" % (self.url, self._workspace, name)
                self.request(url, f.read(), "put")
            conn = sqlite3.connect(filename)
            cursor = conn.cursor()
            cursor.execute("SELECT table_name FROM gpkg_geometry_columns")
            tablename = cursor.fetchall()[0][0]
            self._uploadedDatasets[filename] = (name, tablename)
        datasetName, geoserverLayerName = self._uploadedDatasets[filename]
        url = "%s/workspaces/%s/datastores/%s/featuretypes/%s.json" % (
        self.url, self._workspace, datasetName, geoserverLayerName)
        r = self.request(url)
        ft = r.json()
        ft["featureType"]["name"] = name
        ft["featureType"]["title"] = name
        ext = layer.extent()
        ft["featureType"]["nativeBoundingBox"] = {
            "minx": round(ext.xMinimum(), 5),
            "maxx": round(ext.xMaximum(), 5),
            "miny": round(ext.yMinimum(), 5),
            "maxy": round(ext.yMaximum(), 5),
            "srs": layer.crs().authid()
        }
        if isDataUploaded:
            url = "%s/workspaces/%s/datastores/%s/featuretypes" % (self.url, self._workspace, datasetName)
            r = self.request(url, ft, "post")
        else:
            r = self.request(url, ft, "put")
        self.logInfo("Feature type correctly created from GPKG file '%s'" % filename)
        self._setLayerStyle(name, name)

    def _publishVectorLayerFromPostgis(self, layer, db):
        name = layer.name()
        username, password = db.getCredentials()

        def _entry(k, v):
            return {"@key": k, "$": v}

        ds = {
            "dataStore": {
                "name": name,
                "type": "PostGIS",
                "enabled": True,
                "connectionParameters": {
                    "entry": [
                        _entry("schema", db.schema),
                        _entry("port", str(db.port)),
                        _entry("database", db.database),
                        _entry("passwd", password),
                        _entry("user", username),
                        _entry("host", db.host),
                        _entry("dbtype", "postgis")
                    ]
                }
            }
        }
        dsUrl = "%s/workspaces/%s/datastores/" % (self.url, self._workspace)
        self.request(dsUrl, data=ds, method="post")
        ft = {
            "featureType": {
                "name": name,
                "srs": layer.crs().authid()
            }
        }
        ftUrl = "%s/workspaces/%s/datastores/%s/featuretypes" % (self.url, self._workspace, name)
        self.request(ftUrl, data=ft, method="post")
        self._setLayerStyle(name, name)

    def _publishVectorLayerFromFileToPostgis(self, layer, filename):
        self.logInfo("Publishing layer from file: %s" % filename)
        self.createPostgisDatastore()
        ws, datastoreName = self.postgisdb.split(":")
        name = layer.name()
        isDataUploaded = filename in self._uploadedDatasets
        if not isDataUploaded:
            _import = {
                "import": {
                    "targetStore": {
                        "dataStore": {
                            "name": datastoreName
                        }
                    },
                    "targetWorkspace": {
                        "workspace": {
                            "name": self._workspace
                        }
                    }
                }
            }
            url = "%s/imports" % (self.url)
            ret = self.request(url, _import, "post")
            importId = ret.json()["import"]["id"]
            url = "%s/imports/%s/tasks" % (self.url, importId)
            with open(filename, "rb") as f:
                files = {os.path.basename(filename): f}
                ret = self.request(url, method="post", files=files)
            taskId = ret.json()["task"]["id"]
            target = {"dataStore": {
                "name": datastoreName
            }
            }
            url = "%s/imports/%s/tasks/%s/target" % (self.url, importId, taskId)
            self.request(url, target, "put")
            url = "%s/imports/%s" % (self.url, importId)
            self.request(url, method="post")
            layername = os.path.splitext(os.path.basename(filename))[0]
            self._uploadedDatasets[filename] = (datastoreName, layername)
        datasetName, geoserverLayerName = self._uploadedDatasets[filename]
        url = "%s/workspaces/%s/datastores/%s/featuretypes/%s.json" % (
        self.url, self._workspace, datasetName, geoserverLayerName)
        r = self.request(url)
        ft = r.json()
        ft["featureType"]["name"] = name
        ft["featureType"]["title"] = name
        try:
            ftUrl = "%s/workspaces/%s/datastores/%s/featuretypes" % (
                self.url,
                self._workspace,
                datasetName,
            )
            r = self.request(ftUrl, ft, "post")
        except:
            r = self.request(url, ft, "put")
        self.logInfo("Feature type correctly created from GPKG file '%s'" % filename)
        self._setLayerStyle(name, name)

    def _publishRasterLayer(self, filename, layername):
        # feedback.setText("Publishing data for layer %s" % layername)
        self._ensureWorkspaceExists()
        with open(filename, "rb") as f:
            url = "%s/workspaces/%s/coveragestores/%s/file.geotiff" % (self.url, self._workspace, layername)
            self.request(url, f.read(), "put")
        self.logInfo("Feature type correctly created from Tiff file '%s'" % filename)
        self._setLayerStyle(layername, layername)

    def createGroups(self, groups, qgis_layers):
        for group in groups:
            self._publishGroup(group, qgis_layers)

    def _publishGroupMapBox(self, group, qgis_layers):
        name = group["name"]
        # compute actual style
        mbstylestring, warnings, obj, spriteSheet = mapboxgl.fromgeostyler.convertGroup(group, qgis_layers,
                                                                                        self.baseUrl(), self._workspace,
                                                                                        group["name"])

        # publish to geoserver
        self._ensureWorkspaceExists()
        styleExists = self.styleExists(name)
        if styleExists:
            self.deleteStyle(name)

        xml = "<style>" \
              + "<name>{0}</name>".format(name) \
              + "<workspace>{0}</workspace>".format(self._workspace) \
              + "<format>" \
              + "mbstyle" \
              + "</format>" \
              + "<filename>{0}.json</filename>".format(name) \
              + "</style>"

        url = self.url + "/workspaces/%s/styles" % (self._workspace)

        response = self.request(url, xml, "POST", {"Content-Type": "text/xml"})
        url = self.url + "/workspaces/%s/styles/%s?raw=true" % (self._workspace, name)

        headers = {"Content-Type": "application/vnd.geoserver.mbstyle+json"}
        response = self.request(url, mbstylestring, "PUT", headers)

        # save sprite sheet
        # get png -> bytes
        if spriteSheet:
            img_bytes = self.getImageBytes(spriteSheet["img"])
            img2x_bytes = self.getImageBytes(spriteSheet["img2x"])
            url = self.url + "/resource/workspaces/%s/styles/spriteSheet.png" % (self._workspace)
            r = self.request(url, img_bytes, "PUT")
            url = self.url + "/resource/workspaces/%s/styles/spriteSheet@2x.png" % (self._workspace)
            r = self.request(url, img2x_bytes, "PUT")
            url = self.url + "/resource/workspaces/%s/styles/spriteSheet.json" % (self._workspace)
            r = self.request(url, spriteSheet["json"], "PUT")
            url = self.url + "/resource/workspaces/%s/styles/spriteSheet@2x.json" % (self._workspace)
            r = self.request(url, spriteSheet["json2x"], "PUT")
            b = 1
        a = 1

    def getImageBytes(self, img):
        ba = QByteArray()
        buff = QBuffer(ba)
        buff.open(QIODevice.WriteOnly)
        img.save(buff, "PNG")
        img_bytes = ba.data()
        return img_bytes

    def _publishGroup(self, group, qgis_layers):
        self._publishGroupMapBox(group, qgis_layers)
        layers = []
        for layer in group["layers"]:
            if isinstance(layer, dict):
                layers.append({"@type": "layerGroup", "name": "%s:%s" % (self._workspace, layer["name"])})
                self._publishGroup(layer)
            else:
                layers.append({"@type": "layer", "name": "%s:%s" % (self._workspace, layer)})

        groupdef = {"layerGroup": {"name": group["name"],
                                   "title": group["title"],
                                   "abstractTxt": group["abstract"],
                                   "mode": "NAMED",
                                   "publishables": {"published": layers}}}

        url = "%s/workspaces/%s/layergroups" % (self.url, self._workspace)
        try:
            self.request(url+"/"+group["name"], method="delete")  # delete if it exists
        except:
            pass
        try:
            self.request(url, groupdef, "post")
        except:
            self.request(url, groupdef, "put")

        # make sure there is VT format tiling
        url = "%s/gwc/rest/layers/%s:%s.xml" % (self.url.replace("/rest", ""), self._workspace, group["name"])
        r = self.request(url)
        xml = r.text
        if "application/vnd.mapbox-vector-tile" not in xml:
            xml = xml.replace("<mimeFormats>", "<mimeFormats><string>application/vnd.mapbox-vector-tile</string>")
            r = self.request(url, xml, "PUT", {"Content-Type": "text/xml"})


        self.logInfo("Group %s correctly created" % group["name"])

    def deleteStyle(self, name):
        if self.styleExists(name):
            url = "%s/workspaces/%s/styles/%s?purge=true&recurse=true" % (self.url, self._workspace, name)
            r = self.request(url, method="delete")

    def _clearCache(self):
        self._layersCache = None

    def _exists(self, url, category, name):
        try:
            if category != "layer" or self._layersCache is None:
                r = self.request(url)
                root = r.json()["%ss" % category]
                if category in root:
                    items = [s["name"] for s in root[category]]
                    if category == "layer":
                        self._layersCache = items
                else:
                    return False
            else:
                items = self._layersCache
            return name in items
        except:
            return False

    def layerExists(self, name):
        url = "%s/workspaces/%s/layers.json" % (self.url, self._workspace)
        return self._exists(url, "layer", name)

    def layers(self):
        url = "%s/workspaces/%s/layers.json" % (self.url, self._workspace)
        r = self.request(url)
        root = r.json()["layers"]
        if "layer" in root:
            return [s["name"] for s in root["layer"]]
        else:
            return []

    def styleExists(self, name):
        url = "%s/workspaces/%s/styles.json" % (self.url, self._workspace)
        return self._exists(url, "style", name)

    def workspaceExists(self):
        url = "%s/workspaces.json" % (self.url)
        return self._exists(url, "workspace", self._workspace)

    def willDeleteLayersOnPublication(self, toPublish):
        if self.workspaceExists():
            layers = self.layers()
            toDelete = list(set(layers) - set(toPublish))
            return bool(toDelete)
        else:
            return False

    def datastoreExists(self, name):
        url = "%s/workspaces/%s/datastores.json" % (self.url, self._workspace)
        return self._exists(url, "dataStore", name)

    def _deleteDatastore(self, name):
        url = "%s/workspaces/%s/datastores/%s?recurse=true" % (self.url, self._workspace, name)
        try:
            r = self.request(url, method="delete")
        except:
            pass

    def deleteLayer(self, name, recurse=True):
        if self.layerExists(name):
            recurseParam = 'recurse=true' if recurse else ""
            url = "%s/workspaces/%s/layers/%s.json?%s" % (self.url, self._workspace, name, recurseParam)
            r = self.request(url, method="delete")

    def openPreview(self, names, bbox, srs):
        url = self.layerPreviewUrl(names, bbox, srs)
        webbrowser.open_new_tab(url)

    def layerPreviewUrl(self, names, bbox, srs):
        baseurl = self.baseUrl()
        names = ",".join(["%s:%s" % (self._workspace, name) for name in names])
        url = (
                    "%s/%s/wms?service=WMS&version=1.1.0&request=GetMap&layers=%s&format=application/openlayers&bbox=%s&srs=%s&width=800&height=600"
                    % (baseurl, self._workspace, names, bbox, srs))
        return url

    def fullLayerName(self, layerName):
        return "%s:%s" % (self._workspace, layerName)

    def layerWmsUrl(self, name):
        return "%s/wms?service=WMS&version=1.1.0&request=GetCapabilities" % (self.baseUrl())

    def layerWfsUrl(self):
        return "%s/wfs" % (self.baseUrl())

    def setLayerMetadataLink(self, name, url):
        layerUrl = "%s/workspaces/%s/layers/%s.json" % (self.url, self._workspace, name)
        r = self.request(layerUrl)
        resourceUrl = r.json()["layer"]["resource"]["href"]
        r = self.request(resourceUrl)
        layer = r.json()
        key = "featureType" if "featureType" in layer else "coverage"
        layer[key]["metadataLinks"] = {
            "metadataLink": [
                {
                    "type": "text/html",
                    "metadataType": "ISO19115:2003",
                    "content": url
                }
            ]
        }
        r = self.request(resourceUrl, data=layer, method="put")

    def deleteWorkspace(self):
        if self.workspaceExists():
            url = "%s/workspaces/%s?recurse=true" % (self.url, self._workspace)
            r = self.request(url, method="delete")
            self._clearCache()

    def _publishStyle(self, name, styleFilename):
        self._ensureWorkspaceExists()
        styleExists = self.styleExists(name)
        if styleExists:
            method = "put"
            url = self.url + "/workspaces/%s/styles/%s" % (self._workspace, name)
        else:
            url = self.url + "/workspaces/%s/styles?name=%s" % (self._workspace, name)
            method = "post"
        _, ext = os.path.splitext(styleFilename)
        if ext.lower() == ".zip":
            headers = {"Content-type": "application/zip"}
            with open(styleFilename, "rb") as f:
                self.request(url, f.read(), method, headers)
            self.logInfo(
                QCoreApplication.translate(
                    "GeocatBridge",
                    "Style %s correctly created from Zip file '%s'" % (name, styleFilename),
                )
            )
        elif ext.lower() == ".mapbox":
            headers = {"Content-type": "application/vnd.geoserver.mbstyle+json"}
            with open(styleFilename) as f:
                self.request(url, f.read(), method, headers)
            self.logInfo(
                QCoreApplication.translate(
                    "GeocatBridge",
                    "Style %s correctly created from mbstyle file '%s'" % (name, styleFilename),
                )
            )



    def _setLayerStyle(self, layername, stylename):
        url = "%s/workspaces/%s/layers/%s.json" % (self.url, self._workspace, layername)
        r = self.request(url)
        layer = r.json()
        styleUrl = "%s/workspaces/%s/styles/%s.json" % (self.url, self._workspace, stylename)
        layer["layer"]["defaultStyle"] = {
            "name": stylename,
            "href": styleUrl
        }
        r = self.request(url, data=layer, method="put")

    def _ensureWorkspaceExists(self):
        if not self.workspaceExists():
            url = "%s/workspaces" % self.url
            ws = {"workspace": {"name": self._workspace}}
            self.request(url, data=ws, method="post")

    def postgisDatastores(self):
        pg_datastores = []
        url = f"{self.url}/workspaces.json"
        res = self.request(url).json().get("workspaces", {})
        if not res:
            # There aren't any workspaces (and thus no dataStores)
            return pg_datastores
        for ws_url in (s.get("href") for s in res.get("workspace", [])):
            props = self.request(ws_url).json().get("workspace", {})
            ws_name, ds_list_url = props.get("name"), props.get("dataStores")
            res = self.request(ds_list_url).json().get("dataStores", {})
            if not res:
                # There aren't any dataStores for this workspace
                continue
            for ds_url in (s.get("href") for s in res.get("dataStore", [])):
                ds = self.request(ds_url).json().get("dataStore", {})
                ds_name, enabled, params = ds.get("name"), ds.get("enabled"), ds.get("connectionParameters", {})
                # Only add dataStore if it is enabled and the "dbtype" parameter equals "postgis"
                # Using the "type" property does not work in all cases (e.g. for JNDI connection pools)
                entries = {e["@key"]: e["$"] for e in params.get("entry", [])}
                if enabled and entries.get("dbtype") == "postgis":
                    pg_datastores.append(f"{ws_name}:{ds_name}")
        return pg_datastores
        
    def addPostgisDatastore(self, datastoreDef):        
        url = "%s/workspaces/%s/datastores/" % (self.url, self._workspace)
        self.request(url, data=datastoreDef, method="post")

    def addOGCServers(self):
        baseurl = "/".join(self.url.split("/")[:-1])
        addServicesForGeodataServer(self.name, baseurl, self.authid)

    # ensure that the geoserver we are dealing with is at least 2.13.2
    def checkMinGeoserverVersion(self, errors):
        try:
            url = "%s/about/version.json" % self.url
            result = self.request(url).json()['about']['resource']
        except:
            errors.add("Could not connect to Geoserver.  Please check the server settings (including password).")
            return
        try:
            ver = next((x["Version"] for x in result if x["@name"] == 'GeoServer'), None)
            if ver is None:
                return  # couldnt find version -- dev GS, lets say its ok
            ver_major, ver_minor, ver_patch = ver.split('.')

            if int(ver_minor) <= 13:  # old
                errors.add(
                    "Geoserver 2.14.0 or later is required.  Selected Geoserver is version '" + ver + "'.  Please see <a href='https://my.geocat.net/knowledgebase/100/Bridge-4-compatibility-with-Geoserver-2134-and-before.html'>Bridge 4 Compatibility with Geoserver 2.13.4 and before</a>")
        except:
            # version format might not be the expected. This is usually a RC or dev version, so we consider it ok
            pass

    def validateGeodataBeforePublication(self, errors, toPublish):
        path = QgsProject.instance().absoluteFilePath()
        if not path:
            errors.add("QGIS Project is not saved. Project must be saved before publishing layers to GeoServer")
        if "." in self._workspace:
            errors.add(
                "QGIS project name contains unsupported characters ('.'). Save with a different name and try again")
        if self.willDeleteLayersOnPublication(toPublish):
            ret = QMessageBox.question(None, "Workspace",
                                       "A workspace with that name exists and contains layers that are not going to be published.\nThose layers will be deleted.\nDo you want to proceed?",
                                       QMessageBox.Yes | QMessageBox.No)
            if ret == QMessageBox.No:
                errors.add("Cannot overwrite existing workspace")
        self.checkMinGeoserverVersion(errors)

