"""Model snapshot, taken INSIDE DianaIE (embedded Python 3.7): every item of the open project with its parameters.

Made for diffing: all values are plain JSON types and every section is keyed by item name. Where possible it uses
commands that DianaIE does not write to its command history, and the data model behind DianaIE's reports
(diana.output.report.query), which also resolves attachment targets into readable text such as
'"beam"的vertex (2, 0.4)' (in the GUI language) instead of internal ids.

Used by the MCP server through the bridge (take(globals(), out_path)) and by batch DianaIE runs on saved projects.
Keep this file compatible with Python 3.7.
"""
import json
import os
import re
import time

FORMAT = 1
MAX_SHAPE_DETAILS = 400  # per-shape geometry queries cost a few ms each and block the GUI meanwhile

# section, availableParameters type, lister command, readable locator of the report data model
ITEM_KINDS = (
    ("materials", "MATERIAL", "materials", "/materials?name="),
    ("geometries", "GEOMET", "geometries", "/element/geometries?name="),
    ("element_data", "DATA", "elementData", "/element/datas?name="),
    ("loads", "GEOMETRYLOAD", "geometryLoads", "/geoload?name="),
    ("supports", "GEOMETRYSUPPORT", "geometrySupports", "/geosupport?name="),
    ("tyings", "GEOMETRYTYING", "geometryTyings", None),
    ("boundary_conditions", "GEOMETRYBC", "geometryBoundaryConditions", None),
    ("fixed_potentials", "GEOMETRYFIXPOT", "geometryFixedPotentials", None),
    ("initial_fields", "GEOMETRYINIFIELD", "geometryInitialFields", None),
)
SHAPE_KINDS = ("regular", "reinfo", "pile", "anchor", "sheetpilewall", "interface")
# readable labels of a material's / geometry's class and model in the report data model
CLASS_ROWS = {"Mcname": "class", "Matmdl": "model", "Gcname": "class", "Geomdl": "model"}


def plain(value):
    """JSON-safe copy of a DIANA return value (tuples, SWIG vectors, numpy-like scalars)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return float("%.12g" % value)
    if isinstance(value, dict):
        return dict((str(k), plain(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    try:
        return [plain(v) for v in list(value)]
    except Exception:
        return str(value)


def key_of(column):
    """'Element size [m]' -> 'element_size'."""
    column = re.sub(r"\[.*?\]", "", str(column)).strip().lower()
    return re.sub(r"\W+", "_", column).strip("_")


class Snapshotter(object):
    def __init__(self, ns):
        self.ns = ns
        self.errors = {}
        try:
            import diana.output.report as report
            self._query = report.query
        except Exception:
            self._query = None

    def call(self, name, *args):
        return self.ns[name](*args)

    def has(self, name):
        return callable(self.ns.get(name))

    def query(self, locator):
        """Rows of the report data model as dicts (first row is the header), or [] if unavailable."""
        if self._query is None:
            return []
        rows = self._query(locator)
        if not rows:
            return []
        header = [str(h) for h in rows[0]]
        return [dict(zip(header, [plain(v) for v in row])) for row in rows[1:]]

    def section(self, name, func):
        try:
            return func()
        except Exception as exc:
            self.errors[name] = "%s: %s" % (type(exc).__name__, exc)
            return None

    # --- sections -------------------------------------------------------------------------------------------

    def project(self):
        out = {}
        for key, command in (("name", "projectName"), ("path", "currentProject"),
                             ("dimension", "modelDimensions"), ("aspects", "modelAnalysisAspects")):
            if self.has(command):
                out[key] = plain(self.call(command))
        # After saveProjectAs, currentProject() keeps the old path, even in the saved file; projectName() follows.
        path, name = out.get("path"), out.get("name")
        if path and name and os.path.splitext(os.path.basename(path))[0] != name:
            out["stored_path"] = path
            out["path"] = os.path.dirname(path).replace("\\", "/") + "/" + name + ".dpf"
        return out

    def units(self):
        return dict((q, plain(self.call("unit", q))) for q in self.call("quantities"))

    def parameters(self, kind, name):
        out = {}
        for path in self.call("availableParameters", kind, name):
            short = re.sub(r"^/[A-Z]+\(\d+\)/", "", str(path))  # drop the item's own root, e.g. /MATERI(1)/
            try:
                out[short] = plain(self.call("parameter", kind, name, path))
            except Exception as exc:
                out[short] = "<error: %s>" % exc
        return out

    @staticmethod
    def readable(rows):
        """{label: 'value unit'} of an item's report table rows (directory rows already removed)."""
        out = {}
        for row in rows:
            label, data, unit = row.get("Name"), row.get("Data"), row.get("Unit") or ""
            if label is not None and data not in (None, ""):
                out[str(label)] = ("%s %s" % (data, unit)).strip() if unit else data
        return out

    def items(self, section, kind, lister, locator):
        if not self.has(lister):
            return {}
        out = {}
        for name in self.call(lister):
            name = str(name)
            entry = {"parameters": self.parameters(kind, name)}
            if locator:
                rows = [r for r in self.query(locator + name) if r.get("Type") != "Directory"]
                for row in rows:
                    if row.get("Name") in CLASS_ROWS and row.get("Data"):
                        entry[CLASS_ROWS[row["Name"]]] = row["Data"]
                if section in ("loads", "supports"):
                    entry["readable"] = self.readable(rows)
                    self.resolve_targets(entry, rows)
            out[name] = entry
        return out

    @staticmethod
    def resolve_targets(entry, rows):
        """Replace attachment target ids by the report's readable text when they pair up one to one."""
        targets = [p for p in sorted(entry["parameters"]) if p.split("/")[-1] == "TARGET"]
        texts = [str(r.get("Data")) for r in rows if r.get("Type") == "list<int>" and '"' in str(r.get("Data"))]
        if targets and len(targets) == len(texts):
            for path, text in zip(targets, texts):
                entry["parameters"][path] = text

    def memberships(self, lister, locator):
        """{item: set} from the per-set tables, e.g. which load case each load belongs to."""
        out = {}
        if not self.has(lister):
            return out
        for set_name in self.call(lister):
            for row in self.query(locator + str(set_name)):
                if row.get("Name") is not None:
                    out[str(row["Name"])] = str(set_name)
        return out

    def shapes(self):
        out = {}
        for kind in SHAPE_KINDS:
            for row in self.query("/shapes/assignments?type=" + kind):
                name = row.pop("Name", None)
                if name is None:
                    continue
                entry = {"kind": kind}
                for column, value in row.items():
                    entry[key_of(column)] = value
                out[str(name)] = entry
        names = [str(n) for n in self.call("shapes")] if self.has("shapes") else []
        for name in names:
            out.setdefault(name, {"kind": "other"})
        for name in names[:MAX_SHAPE_DETAILS]:
            entry = out[name]
            try:
                body = str(self.call("bodyType", name))
                entry["body"] = body
                entry["bbox"] = plain(list(self.call("boundingBox", name)))
                measure = {"SOLID": "volumeOf", "SHEET": "areaOf", "WIRE": "lengthOf"}.get(body)
                if measure and self.has(measure):
                    entry["size"] = plain(self.call(measure, name))
                for key, command in (("vertices", "vertexCount"), ("edges", "edgeCount"), ("faces", "faceCount")):
                    entry[key] = plain(self.call(command, name))
            except Exception as exc:
                entry["detail_error"] = str(exc)
        if len(names) > MAX_SHAPE_DETAILS:
            self.errors["shapes"] = "geometry details skipped for %d shapes" % (len(names) - MAX_SHAPE_DETAILS)
        return out

    def keyed_table(self, locator, key="Name"):
        out = {}
        for row in self.query(locator):
            name = row.pop(key, None)
            if name is not None:
                out[str(name)] = dict((key_of(k), v) for k, v in row.items())
        return out

    def analyses(self):
        out = {}
        for name in self.call("analyses"):
            name = str(name)
            entry = {"commands": plain(self.call("analysisCommands", name))}
            rows = self._query("/analysiscommands?name=" + name) if self._query else None
            if rows:
                entry["dcf"] = [str(r[0]) for r in rows[1:] if r and str(r[0]).strip()]
            if self.has("phaseNames"):
                phases = plain(self.call("phaseNames", name))
                if phases:
                    entry["phases"] = phases
            out[name] = entry
        return out

    def mesh(self):
        return {"nodes": len(self.call("allNodeIds")), "elements": len(self.call("allElementIds"))}

    def take(self):
        snap = {"format": FORMAT, "taken": round(time.time(), 3)}
        snap["project"] = self.section("project", self.project)
        snap["units"] = self.section("units", self.units)
        snap["definitions"] = self.section("definitions", lambda: dict(
            (str(r.get("Name")), r.get("Value")) for r in self.query("/reference/definitions")))
        snap["directions"] = self.section("directions", lambda: self.keyed_table("/reference/directions"))
        snap["shapes"] = self.section("shapes", self.shapes)
        for section, kind, lister, locator in ITEM_KINDS:
            snap[section] = self.section(section, lambda s=section, k=kind, l=lister, loc=locator: self.items(s, k, l, loc))
        cases = self.section("load_cases", lambda: self.memberships("geometryLoadSets", "/geoloadcases?name="))
        for name, case in (cases or {}).items():
            if name in (snap.get("loads") or {}):
                snap["loads"][name]["load_case"] = case
        sets = self.section("support_sets", lambda: self.memberships("geometrySupportSets", "/geosupportsets?name="))
        for name, set_name in (sets or {}).items():
            if name in (snap.get("supports") or {}):
                snap["supports"][name]["support_set"] = set_name
        snap["load_cases"] = self.section("load_cases", lambda: plain(self.call("geometryLoadSets")))
        snap["support_sets"] = self.section("support_sets", lambda: plain(self.call("geometrySupportSets")))
        snap["load_combinations"] = self.section("load_combinations", lambda: self.load_combinations())
        snap["node_groups"] = self.section("node_groups", lambda: plain(self.call("geometryNodeGroups")))
        snap["node_group_items"] = self.section("node_group_items", lambda: plain(self.call("geometryNodeGroupItems")))
        snap["element_sets"] = self.section("element_sets", lambda: self.keyed_table("/elementsets"))
        snap["reinforcement_sets"] = self.section("reinforcement_sets", lambda: self.keyed_table("/reinfosets"))
        snap["functions"] = self.section("functions", lambda: plain(self.call("functions")))
        snap["mesh"] = self.section("mesh", self.mesh)
        snap["analyses"] = self.section("analyses", self.analyses)
        if self.errors:
            snap["errors"] = self.errors
        return snap

    def load_combinations(self):
        """{combination: {load case: factor}} from the report's combination matrix."""
        rows = self._query("/geoloadcombinations") if self._query else None
        if not rows or len(rows) < 2:
            return {}
        header = [str(h) for h in rows[0]]
        out = {}
        for row in rows[1:]:
            out[str(row[0])] = dict((header[i], plain(row[i])) for i in range(1, min(len(header), len(row))))
        return out


def take(ns, out_path=None):
    """Snapshot of the project open in DianaIE; written as UTF-8 JSON to ``out_path`` when given."""
    snap = Snapshotter(ns).take()
    if out_path:
        folder = os.path.dirname(out_path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, out_path)
    return snap
