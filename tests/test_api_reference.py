from diana_mcp.api_reference import CACHE_FORMAT, ApiReference, ApiReferenceStore, version_slug, words

FUNCTIONS = {
    "createSheet": {
        "doc": "Creates a planar sheet from a set of input coordinates.\n    Returns: bool\n\n    Usage:\n"
               "    createSheet( name, points )\n\n    Parameter name : The name of the sheet.",
        "kind": "function",
    },
    "generateMesh": {
        "doc": "Generate a mesh for the given shapes.\n    Usage:\n    generateMesh( shapes = QStringList() )",
        "kind": "function",
    },
    "setDefaultMeshOrder": {"doc": "Sets the default mesh order.\n    Usage:\n    setDefaultMeshOrder( order )", "kind": "function"},
    "addMaterial": {"doc": "TODO\n    Usage:\n    addMaterial( material, materialClass = \"\" )", "kind": "function"},
    "runSolver": {
        "doc": "Executes the commands for a specified analysis.\n    Usage:\n    runSolver( analysisNames )\n\n"
               "    runSolver( analysisName )",
        "kind": "function",
    },
    "saveImage": {"doc": "Saves the current view of the 3D window to an image file.", "kind": "function"},
}


def reference():
    return ApiReference("Diana 10.5, Latest update: 2021-05-01", FUNCTIONS, {"MATERIAL": "MATERIAL"})


def test_words_split_identifiers():
    assert words("addMaterialSafetyFactors") == ["add", "material", "safety", "factors"]
    assert words("setViewPointIso1") == ["set", "view", "point", "iso", "1"]


def test_version_slug():
    assert version_slug("Diana 10.5, Latest update: 2021-05-01 07:33:44") == "10.5"
    assert version_slug("10.10") == "10.10"


def test_exact_name_ranks_first():
    assert reference().search("createSheet")[0].name == "createSheet"


def test_word_search_finds_mesh_commands():
    names = [e.name for e in reference().search("mesh")]
    assert names[:2] == ["generateMesh", "setDefaultMeshOrder"]


def test_description_words_are_searchable():
    assert reference().search("image file")[0].name == "saveImage"


def test_usages_include_overloads():
    entry = reference().get("runSolver")
    assert entry.usages == ["runSolver( analysisNames )", "runSolver( analysisName )"]
    assert entry.one_line() == "runSolver( analysisNames ) — Executes the commands for a specified analysis."


def test_get_is_case_insensitive_and_suggests_close_names():
    ref = reference()
    assert ref.get("createsheet").name == "createSheet"
    assert ref.get("createSheets") is None
    assert "createSheet" in ref.suggest("createSheets")


def test_store_generates_once_and_caches(tmp_path):
    calls = []

    def generate(path):
        calls.append(path)
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": CACHE_FORMAT, "diana_version": "Diana 10.5", "functions": FUNCTIONS}),
                        encoding="utf-8")

    store = ApiReferenceStore(tmp_path)
    assert store.get("Diana 10.5, Latest update: x", generate).get("addMaterial")
    assert ApiReferenceStore(tmp_path).get("10.5", generate).get("addMaterial")
    assert calls == [tmp_path / "diana-10.5.json"]
    assert store.cached_versions() == ["10.5"]
