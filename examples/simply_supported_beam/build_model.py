# Simply supported beam, 2D plane stress, linear static analysis (DIANA 10.5 script).
#
# Run inside DianaIE: via the diana_execute MCP tool, the DianaIE command console, or
# "DianaIE.exe build_model.py". DianaIE has no __file__, so set PROJECT_DIR to an absolute path.
#
# Span 4 m, depth 0.4 m, thickness 0.2 m, E = 30 GPa, nu = 0.2, P = 10 kN at midspan (top face).
# Timoshenko beam theory, midspan deflection:
#   PL^3/(48EI) + PL/(4 kappa G A) = 0.4167 mm + 0.0120 mm = 0.4287 mm

PROJECT_DIR = r"F:/Diana-MCP/examples/simply_supported_beam"
NAME = "ss_beam"
L, H, T = 4.0, 0.4, 0.2
E, NU, P = 3.0e10, 0.2, 10000.0
MESH_SIZE = 0.05
ANALYSIS, COMMAND = "Linear static", "Structural linear static"

newProject(PROJECT_DIR + "/" + NAME, 10)
setModelAnalysisAspects(["STRUCT"])
setModelDimension("2D")
setDefaultMeshOrder("QUADRATIC")
setDefaultMesherType("HEXQUAD")

# attach() snaps to the nearest EXISTING vertex, so the midspan points are part of the outline.
createSheet("beam", [[0, 0, 0], [0, H, 0], [L / 2, H, 0], [L, H, 0], [L, 0, 0], [L / 2, 0, 0]])

# Element class, material and thickness must be assigned before meshing; otherwise nothing is meshed.
setElementClassType("SHAPE", ["beam"], "MEMBRA")  # plane stress
addMaterial("Concrete", "CONCR", "LEI", [])  # linear elastic isotropic
setParameter("MATERIAL", "Concrete", "LINEAR/ELASTI/YOUNG", E)
setParameter("MATERIAL", "Concrete", "LINEAR/ELASTI/POISON", NU)
assignMaterial("Concrete", "SHAPE", ["beam"])
addGeometry("Plate t=0.2", "SHEET", "MEMBRA", [])
setParameter("GEOMET", "Plate t=0.2", "THICK", T)
assignGeometry("Plate t=0.2", "SHAPE", ["beam"])

# Supports: pin at the left bottom corner, roller at the right bottom corner.
# attach() may return False even when it worked: check the exported .dat or a screenshot.
addSet("GEOMETRYSUPPORTSET", "Supports")
createPointSupport("Pin", "Supports")
setParameter("GEOMETRYSUPPORT", "Pin", "AXES", [1, 2])
setParameter("GEOMETRYSUPPORT", "Pin", "TRANSL", [1, 1])
attach("GEOMETRYSUPPORT", "Pin", "beam", [[0.0, 0.0]])
createPointSupport("Roller", "Supports")
setParameter("GEOMETRYSUPPORT", "Roller", "AXES", [1, 2])
setParameter("GEOMETRYSUPPORT", "Roller", "TRANSL", [0, 1])
attach("GEOMETRYSUPPORT", "Roller", "beam", [[L, 0.0]])

# Point load at midspan (top), downward.
addSet("GEOMETRYLOADSET", "Point load")
createPointLoad("P", "Point load")
setParameter("GEOMETRYLOAD", "P", "FORCE/VALUE", -P)
setParameter("GEOMETRYLOAD", "P", "FORCE/DIRECT", 2)
attach("GEOMETRYLOAD", "P", "beam", [[L / 2, H]])

# Node group at midspan (bottom) to read the deflection.
addSet("GEOMETRYNODEGROUP", "Mid_Bottom")
createPointNodeGroupItem("Mid_Bottom_Item", "Mid_Bottom")
attach("GEOMETRYNODEGROUPITEM", "Mid_Bottom_Item", "beam", [[L / 2, 0.0]])

setElementSize(["beam"], MESH_SIZE, 0.5, True)
setMesherType(["beam"], "HEXQUAD")
generateMesh([])

# Linear static analysis: default binary output (.dnb) plus a tabulated output (.tb) without page breaks
# (LINPAG 0) and with 6 significant digits (manual Appendix I.1.1.2).
addAnalysis(ANALYSIS)
addAnalysisCommand(ANALYSIS, "LINSTA", COMMAND)
addAnalysisCommandDetail(ANALYSIS, COMMAND, "OUTPUT(2)")
setAnalysisCommandDetail(ANALYSIS, COMMAND, "OUTPUT(2)/DEVICE", "TABULA")
addAnalysisCommandDetail(ANALYSIS, COMMAND, "OUTPUT(2)/LAYOUT")
setAnalysisCommandDetail(ANALYSIS, COMMAND, "OUTPUT(2)/LAYOUT/LINPAG", 0)
addAnalysisCommandDetail(ANALYSIS, COMMAND, "OUTPUT(2)/LAYOUT/DIGITS/RESULT")
setAnalysisCommandDetail(ANALYSIS, COMMAND, "OUTPUT(2)/LAYOUT/DIGITS/RESULT", 6)
setAnalysisCommandDetail(ANALYSIS, COMMAND, "OUTPUT(2)/SELTYP", "USER")
addAnalysisCommandDetail(ANALYSIS, COMMAND, "OUTPUT(2)/USER")
for quantity in ("DISPLA(1)/TOTAL/TRANSL/GLOBAL", "FORCE(1)/REACTI/TRANSL/GLOBAL", "STRESS(1)/TOTAL/CAUCHY/GLOBAL"):
    addAnalysisCommandDetail(ANALYSIS, COMMAND, "OUTPUT(2)/USER/" + quantity)

saveProject()
print("model ready:", currentProject())
