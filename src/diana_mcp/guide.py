"""The full usage guide behind diana_guide.

Clients cut long server instructions (Claude Code keeps the first 2048 characters), so the instructions hold only the
essentials and send the AI here once per conversation. Parts can be read one at a time.
"""
from __future__ import annotations

import sys

WORKFLOW = """\
Workflow:
1. diana_start: attach to a DianaIE that already runs the bridge, or launch a new DianaIE (optionally opening a .dpf
   project).
2. Look things up before using them; all references come from the local installation, so trust them over memory:
   - diana_api_search / diana_api_doc: DianaIE script commands and signatures.
   - diana_keywords: data-model keywords (material classes/models such as CONCR/TSCR, geometry and element classes such
     as SHEET/MEMBRA/PLSTRA/AXISYM, load types, parameters with units and allowed values). The manual does not list them.
   - diana_parameters(item_type, name): the exact setParameter paths and current values of an existing item.
   - diana_manual_search / diana_manual_read: the user's manual (theory, element/material libraries, analysis options,
     .dat/.dcf batch syntax in Appendices E-I, verification examples with complete .dcf files).
   - diana_knowledge_search: the experience handbook, the manual, the examples, the keywords and the script commands in
     one ranked list; Chinese questions work.
   - diana_command_log: exact calls behind the user's GUI actions (ask them to do a step once, then read it).
3. diana_execute runs Python 3.7 code in DianaIE's scripting namespace, where every DIANA command (newProject,
   createSheet, addMaterial, generateMesh, ...) is a global. Variables persist between calls; a final expression's value
   is returned.
4. Check the model with diana_model_summary and diana_analysis_commands. Then call diana_solve with no authorization:
   it does not calculate; it fits and shows the complete model in DianaIE and opens a modeless review panel containing
   units, validation findings, changes, the full snapshot and .dcf. Only the user's button click in that GUI can approve
   one debug/validation run. Call diana_review_status and rerun diana_solve with its review_id.
5. A "baseline" GUI decision first creates a candidate. It becomes trusted only after its validation solve ends
   normally, has no errors and every step converges. Parameter work starts with diana_parameter_study_prepare: the user
   approves parameter names, authorization-path prefixes, ranges, combinations and checks once. Variants inside that
   envelope may then use diana_solve(campaign_id=..., case_parameters=...) without per-case GUI approval. Poll
   diana_job_status; completion records post-solve quality evidence.
6. Read numbers with diana_read_tb (the .tb text output), diana_results_table (results loaded in DianaIE, 6 significant
   digits by default, digits=0 for full double precision, also reinforcement/pile/layer results) or diana_read_out
   (log, eigenfrequencies, factor of safety); view them with diana_load_results + diana_plot_result. Tables return 30
   rows inline by default: filter by node/element/step, or pass csv_path to get every row in a file.
7. diana_stop saves and/or closes when the work is done.
Existing .dat/.dcf pairs can be solved with diana_run_solver without DianaIE only after the model is a certified
baseline, or as a case in an approved parameter campaign (including a complete parameter map and change manifest).
This is the fast solver stage, not a modeling/review bypass; restarts on an existing .ff use restart_ff. Features with
only batch syntax must be established and reviewed in the baseline or explicitly authorized by the campaign.
"""

SCRIPTING = """\
DIANA 10.5 scripting facts (verified):
- DIANA has no fixed units: check unit(q) for each quantity (diana_model_summary lists them), keep one consistent system
  (manual Table 5.1; setUnit / setUnitSet), and express every value in it (gravity is -9.81 in m but -9810 in mm).
- setParameter takes the path below the item's model directory: availableParameters reports
  /MATERI(1)/MATCLS/CONCR/TSCR/TENSIL/TENSTR, setParameter needs "TENSIL/TENSTR" (geometry: "THICK"; loads and supports:
  below /POINT(1)/, e.g. "FORCE/VALUE"). Longer or shorter forms make it return False without an error and without
  changing anything, so check its return value; diana_parameters shows the settable form ("set as"). parameter()
  reads with any tail. availableParameters lists a parameter only once it has a value. Set a load's or support's type
  before attach().
- Setters take internal keywords (setModelDimension("2D"), setModelAnalysisAspects(["STRUCT"]), element class "MEMBRA" =
  plane stress), while getters in a localized DianaIE return translated names (e.g. modelDimensions() -> ['二维']).
- Assign setElementClassType, assignMaterial and (for sheets/lines) assignGeometry before generateMesh; otherwise
  generateMesh meshes nothing and returns [].
- attach(type, name, shape, [[x, y]]) snaps to the nearest EXISTING vertex/edge/face; it never creates a vertex. Put
  vertices where point loads, point supports or node groups belong (e.g. extra points in the createSheet outline).
- attach() can return False even when it worked; verify with exportModel(<path>.dat) ('SUPPOR', 'LOADS', 'GROUPS') or a
  screenshot.
- A TABULA output block (addAnalysisCommandDetail(analysis, command, "OUTPUT(n)"), DEVICE "TABULA", SELTYP "USER", then
  "OUTPUT(n)/USER/DISPLA(1)/TOTAL/TRANSL/GLOBAL" etc.) makes the solver write the .tb file that diana_read_tb reads.
- Nonlinear steps: setAnalysisCommandDetail(analysis, command, "EXECUT(1)/LOAD/STEPS/EXPLIC/SIZES", "0.25(12)") and
  "EXECUT(1)/ITERAT/MAXITE". Eigenvalue analysis: command type "EIGEN", modes via "EXECUT/NMODES"; mass needs a density,
  setParameter("MATERIAL", m, "LINEAR/MASS/DENSIT", rho). Inspect any analysis with saveAnalysisCommands(analysis,
  <path>.dcf) to see the command tree and how command-detail paths map to .dcf syntax.
- TABULA layout keywords (manual Appendix I.1.1.2): LINPAG 0 = no page breaks, COLLIN = line width (rows wider than
  130 characters are wrapped), DIGITS RESULT n (1-10, default 4 significant digits), COMBIN = one table for all items.
  Command-detail paths found by experiment: "OUTPUT(n)/LAYOUT" (add), "OUTPUT(n)/LAYOUT/LINPAG" 0,
  "OUTPUT(n)/LAYOUT/DIGITS/RESULT" (add, then set). diana_solve / diana_run_solver set LINPAG 0 and COLLIN 1000 by
  default (tb_no_paging) and optionally the digits (tb_digits) in the job's copy of the .dcf.
- DIANA writes results of non-converged steps as well, and after a divergence it writes the diverged step and then the
  last converged step again; check the job's not_converged_steps before using the last steps.
- Blocks that share one FILOS database (a *NONLIN followed by *REDUCT, *ELIQUE or *ECREEP; staggered flow-stress;
  phases) must be commands of the same DianaIE analysis, or run as a restart on the kept .ff.
- saveProjectAs needs an explicit .dpf extension (without it DIANA writes an extension-less file). Afterwards
  currentProject() still returns the old path, while projectName() and saveProject() use the new file.
"""

TRACKING = """\
Following the user's own work in the GUI:
- DianaIE writes every command, GUI clicks included, as a Python call to its history file. The bridge tags each part
  as the user's or yours and records DianaIE's undo counts, so undo steps can be traced to what they reverted.
- diana_changes: what the user did since you last looked (operations in order, undone ones marked, what each undo
  reverted) plus the net model changes with old -> new values from snapshots, attributed to user/ai where possible.
  Call it first when the user says they changed something, or before building on a model they may have edited. The
  baseline snapshot of each project is kept on disk, so it also covers edits made in an earlier DianaIE session.
  wait_seconds blocks until the user pauses after new edits.
- diana_command_log: the full history of a DianaIE session, also of earlier sessions and without a bridge.
- diana_model_snapshot / diana_model_diff: named checkpoints and comparisons, also of saved .dpf files.
- Follow mode (Claude Code): start `{python} -m diana_mcp watch --follow` with the Monitor tool (timeout 30 min, re-arm
  when it expires). Each notification is one batch of the user's edits, sent after they pause: summarize it for the
  user in their language and point out anything that looks wrong. Without Monitor, run `... watch` (one batch, then
  exit) as a background command.
- diana_autostart(action="enable") makes every DianaIE the user opens start the bridge by itself; it changes their
  Python user site folder, so only enable it when they ask.
"""

KNOWLEDGE = """\
Knowledge base and experience handbook (the project's knowledge/ folder, a git repository):
- diana_knowledge_search searches, in one call, the experience handbook, the user's manual, the verification examples,
  the data-model keywords and the script commands. Chinese questions work: DIANA terms are translated with DianaIE's
  official GUI glossary (diana_glossary looks terms up in both directions, e.g. the Chinese GUI label the user
  mentions), and a semantic (embedding) retriever joins the ranking when one is configured. Search before modeling
  something unfamiliar, and whenever DIANA reports an error or a result looks wrong.
- Experiences marked 已验证 were approved by the user. 未验证 ones (drafts, also those seeded from old logs) are
  hints: say so when you rely on one, and check it.
- Record what you learn with diana_experience_add (always a draft): a solved error, a pitfall, a working API usage,
  a convergence fix, with its evidence (job, file, date). It refuses near-duplicates: update the existing entry.
- When a draft actually solved the user's problem, add the new evidence (diana_experience_update add_evidence) and ask
  the user whether to approve it. Set status "verified" only after the user explicitly agrees in the conversation,
  passing their words as approval. Mark wrong entries "disputed" and outdated ones "obsolete", with a reason.
- When a search result solved the problem, report it with diana_knowledge_feedback; the log becomes the evaluation
  set for improving retrieval.
- Tool results bring matching experience by themselves: related_experience in diana_job_status / diana_read_out
  (matched on error codes and messages), in diana_model_summary (validation issues) and in diana_execute errors.
  Read them before debugging from scratch.
- Learning happens automatically and always as drafts:
  * a solver job that succeeds after a problem run of the same case gets a draft built from the difference of the
    two runs' inputs (job status: learned.draft); explain the cause with diana_experience_update, then ask the user;
  * drafts recommended during that problem appear as learned.approval_candidates: if they really were the fix, add the
    evidence and ask the user to approve them;
  * a DIANA command that failed and then worked with other arguments is drafted as api-usage (diana_execute
    auto_drafts); explain it, then ask.
- diana_experience_mine_logs turns a folder of old .out files into drafts, one per DIANA message kind.
- Keeping the handbook healthy:
  * diana_knowledge_feedback also takes unhelpful refs: report recommended entries that were wrong, so they sink;
  * diana_experience_review lists what to bring to the user (drafts that proved useful, drafts missing a cause,
    similar entries to merge, failed or outdated verifications); use it at the end of a work session or when asked;
  * entries can carry a "## 验证脚本" (python, run in a batch DianaIE with WORKDIR); diana_experience_verify re-runs
    them, e.g. after a DIANA upgrade (verified entries that fail become disputed) or before proposing a draft;
  * diana_experience_export writes the readable handbook (HTML + Markdown) for the user and colleagues;
  * the most useful verified entries come with the first tool result of a session (handbook_tips) and at the end of
    this guide.
"""

RULES = """\
Rules:
- Use absolute paths: DianaIE's working directory is not the project folder and __file__ is undefined.
- Build models in small steps and verify each step; the user may be watching the GUI.
- Code runs on DianaIE's GUI thread and blocks it. Direct runSolver/runStabor calls are forbidden; use the gated
  diana_solve / diana_run_solver workflow.
- Never call pause() or anything that opens a dialog: it blocks the bridge until the user closes it.
- newProject, openProject, importModel and closeProject discard unsaved changes of the open project; save first or ask.
- When the user refers to "this face/shape", read their selection with selectedShapes() / selectedFaces() etc.
- Keep tool output small: filter tables (nodes, elements, steps) instead of reading everything, and write big tables to
  CSV files.
"""

SECTIONS = {"workflow": WORKFLOW, "scripting": SCRIPTING, "tracking": TRACKING, "knowledge": KNOWLEDGE, "rules": RULES}
TOPICS = tuple(SECTIONS)


def text(topic: str = "all", tips: str = "") -> str:
    """The guide, or one part of it; ``tips`` (the handbook's verified lessons) is appended to "all" and
    "knowledge"."""
    if topic != "all" and topic not in SECTIONS:
        raise KeyError(f"unknown topic {topic!r}; use all or one of {', '.join(TOPICS)}")
    parts = list(SECTIONS.values()) if topic == "all" else [SECTIONS[topic]]
    if tips and topic in ("all", "knowledge"):
        parts.append(tips.strip() + "\n")
    return "\n".join(parts).replace("{python}", sys.executable)
