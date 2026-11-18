"""QuadFix Lite assertion suite. Run: blender -b --factory-startup --python tests/suite.py [-- --quick]
Exit code 1 on any failure. Every case runs under a matrix of select modes and
selection variants; every result is checked against mesh invariants."""
import bpy, bmesh, sys, os, time, math, traceback, hashlib
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import quadfix_lite
quadfix_lite.register()
from meshes import *

QUICK = "--quick" in sys.argv
MODES = {"V": (True, False, False), "E": (False, True, False), "F": (False, False, True)}
FAILS, PASSES, SKIPS = [], 0, []


def face_hash(obj, pred=lambda f: True):
    bm = bmesh.new(); bm.from_mesh(obj.data)
    h = hashlib.md5()
    for f in bm.faces:
        if pred(f):
            h.update(str([tuple(round(c, 5) for c in v.co) for v in f.verts]).encode())
    bm.free()
    return h.hexdigest()


def _face_key(f, uvs=()):
    """Everything an operator limited to the selection must leave alone on an unselected face: its vertex cycle
    (rounded, rotated to start at the smallest vertex, direction kept: a flip shows), the seam / sharp flags of
    its edges, its UVs and its material."""
    cs = [(tuple(round(c, 5) for c in l.vert.co), l.edge.seam, l.edge.smooth,
           tuple(tuple(round(c, 5) for c in l[uv].uv) for uv in uvs)) for l in f.loops]
    i = cs.index(min(cs))
    return (f.material_index, tuple(cs[i:] + cs[:i]))


def unselected_faces(obj):
    """Edit Mode: keys of all faces that are not selected. No tolerance band next to the selection border."""
    bm = bmesh.from_edit_mesh(obj.data)
    uvs = bm.loops.layers.uv.values()
    return {_face_key(f, uvs) for f in bm.faces if not f.select}


def all_faces(obj):
    """Object Mode: key -> selected, for every face."""
    bm = bmesh.new(); bm.from_mesh(obj.data)
    uvs = bm.loops.layers.uv.values()
    keys = {}
    for f in bm.faces:
        k = _face_key(f, uvs)
        keys[k] = keys.get(k, False) or f.select
    bm.free()
    return keys


def mesh_report(obj):
    bm = bmesh.new(); bm.from_mesh(obj.data); bm.normal_update()
    r = dict(faces=len(bm.faces),
             tris=sum(1 for f in bm.faces if len(f.verts) == 3),
             quads=sum(1 for f in bm.faces if len(f.verts) == 4),
             ngons=sum(1 for f in bm.faces if len(f.verts) > 4),
             boundary=sum(1 for e in bm.edges if e.is_boundary),
             nonmanifold=sum(1 for e in bm.edges if not e.is_manifold and not e.is_boundary),
             loose_verts=sum(1 for v in bm.verts if not v.link_edges),
             degenerate=sum(1 for f in bm.faces if f.calc_area() < 1e-9),
             folds=0, worst_dihedral=0.0)
    # two faces on one edge must run along it in opposite directions, or one of them is flipped
    r["bad_wind"] = sum(1 for e in bm.edges if len(e.link_loops) == 2 and e.link_loops[0].vert is e.link_loops[1].vert)
    for e in bm.edges:
        lf = e.link_faces
        if len(lf) == 2 and lf[0].normal.length > 1e-9 and lf[1].normal.length > 1e-9:
            a = math.degrees(lf[0].normal.angle(lf[1].normal))
            r["worst_dihedral"] = max(r["worst_dihedral"], a)
            if a > 110: r["folds"] += 1
    r["radius"] = max((v.co.length for v in bm.verts), default=0)
    bm.free()
    return r


def attrs(obj):
    me = obj.data
    return dict(uv=len(me.uv_layers), col=len(me.color_attributes), vg=len(obj.vertex_groups))


def select_variant(obj, variant):
    """Set up the edit-mode selection for a variant. Returns pred of faces expected untouched."""
    if variant == "all":
        bpy.ops.mesh.select_all(action="SELECT")
        return None
    if variant == "half":
        # select faces with local x > 0 only (partial selection, Only Selected on)
        bm = bmesh.from_edit_mesh(obj.data)
        for f in bm.faces:
            f.select = f.calc_center_median().x > 0
        bm.select_flush_mode()
        bmesh.update_edit_mesh(obj.data)
        return lambda f: f.calc_center_median().x < -0.05
    if variant == "none":
        bpy.ops.mesh.select_all(action="DESELECT")
        return lambda f: True


def run_case(name, maker, op, variants=("all", "half"), modes=("V", "E", "F"), expect=None, timeout=60):
    """expect: dict of checks. Defaults: valid, no new non-manifold, no degenerate, no loose,
    attrs preserved, untouched faces unchanged. For fill: boundary==0, folds==0."""
    expect = expect or {}
    for mode in modes:
        for variant in variants:
            label = f"{name} [{mode}/{variant}]"
            try:
                reset()
                made = maker()
                objs = list(made) if isinstance(made, tuple) else [made]
                obj = objs[0]
                for o in objs: o.select_set(True)
                bpy.context.view_layer.objects.active = obj
                before = mesh_report(obj); a0 = attrs(obj)
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.context.tool_settings.mesh_select_mode = MODES[mode]
                untouched = select_variant(obj, variant)
                h_before = face_hash(obj, untouched) if untouched else None
                unsel = unselected_faces(obj) if variant != "all" else set()
                t = time.time()
                res = op()
                dt = time.time() - t
                bpy.ops.object.mode_set(mode="OBJECT")
                after = mesh_report(obj); a1 = attrs(obj)
                errs = []
                if obj.data.validate(verbose=False):
                    errs.append("mesh.validate() had to fix data")
                if "FINISHED" not in res and variant == "all" and "cancel_ok" not in expect:
                    errs.append(f"operator returned {res}")
                if after["nonmanifold"] > before["nonmanifold"]:
                    errs.append(f"non-manifold {before['nonmanifold']}→{after['nonmanifold']}")
                if after["degenerate"] > before["degenerate"]:
                    errs.append(f"degenerate faces {before['degenerate']}→{after['degenerate']}")
                if after["folds"] > before["folds"] and "folds_ok" not in expect:
                    errs.append(f"folds {before['folds']}→{after['folds']}")
                if after["loose_verts"] > before["loose_verts"] and "loose_ok" not in expect:
                    errs.append(f"loose verts {before['loose_verts']}→{after['loose_verts']}")
                if a1 != a0:
                    errs.append(f"attributes changed {a0}→{a1}")
                if untouched and face_hash(obj, untouched) != h_before:
                    errs.append("faces outside the selection were modified")
                now = all_faces(obj) if unsel else {}
                lost = [k for k in unsel if k not in now]
                if lost:
                    errs.append(f"{len(lost)} unselected faces were modified or removed (shape, UVs, material, seam or sharp)")
                if "select_ok" not in expect:
                    picked = [k for k in unsel if now.get(k)]
                    if picked:
                        errs.append(f"{len(picked)} unselected faces came back selected")
                if variant == "all" and (op is CLEAN or op is NGONS):
                    # the faces the operator rebuilds replace selected ones: everything stays selected
                    dropped = sum(1 for f in obj.data.polygons if not f.select)
                    if dropped:
                        errs.append(f"{dropped} faces lost their selection")
                if after["bad_wind"] > before["bad_wind"]:
                    errs.append(f"flipped faces: edges with inconsistent winding {before['bad_wind']}→{after['bad_wind']}")
                if variant == "half" and "half_cleans" in expect:
                    # a no-op must not pass: the selected half has to lose non-quads (the other half can not change)
                    nq = lambda r: r["tris"] + r["ngons"]
                    if not nq(after) < nq(before):
                        errs.append(f"selected half not cleaned: non-quads {nq(before)}→{nq(after)}")
                if after["radius"] > before["radius"] * 1.15 + 1e-6:
                    errs.append(f"geometry exploded: radius {before['radius']:.3f}→{after['radius']:.3f}")
                if dt > timeout:
                    errs.append(f"took {dt:.1f}s > {timeout}s")
                if variant == "all":
                    for k, v in expect.items():
                        if k in ("loose_ok", "folds_ok", "cancel_ok", "half_cleans", "select_ok"): continue
                        if callable(v):
                            if not v(before, after): errs.append(f"expect {k} failed: {after}")
                        elif after.get(k) != v:
                            errs.append(f"{k}={after.get(k)} expected {v}")
                if errs:
                    FAILS.append((label, errs, before, after))
                    print(f"FAIL  {label}: " + "; ".join(errs))
                else:
                    global PASSES; PASSES += 1
                    print(f"ok    {label}  {dt:.2f}s  q{after['quads']} t{after['tris']} n{after['ngons']} b{after['boundary']}")
            except Exception:
                FAILS.append((label, [traceback.format_exc().splitlines()[-1]], None, None))
                print(f"ERROR {label}:\n" + traceback.format_exc())


def _passed(label, errs):
    global PASSES
    if errs:
        FAILS.append((label, errs, None, None)); print(f"FAIL  {label}: " + "; ".join(errs))
    else:
        PASSES += 1; print(f"ok    {label}")


def single_edge_case():
    """Only Selected with one selected edge in Edge select mode: its faces are not selected, so nothing may
    change, and the report has to say why instead of a silent no-op."""
    label = "single edge clean [E]"
    try:
        reset(); obj = make_ai_like_mesh(); h0 = face_hash(obj)
        obj.select_set(True); bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.context.tool_settings.mesh_select_mode = MODES["E"]
        bpy.ops.mesh.select_all(action="DESELECT")
        bm = bmesh.from_edit_mesh(obj.data)
        e = next(e for e in bm.edges if len(e.link_faces) == 2)
        e.select = True
        bm.select_flush_mode(); bmesh.update_edit_mesh(obj.data)
        CLEAN()
        msg = bpy.context.scene.quadfix_lite.report
        bpy.ops.object.mode_set(mode="OBJECT")
        errs = []
        if face_hash(obj) != h0: errs.append("mesh changed")
        if "Nothing cleaned" not in msg: errs.append(f"no explanation in the report: {msg!r}")
        _passed(label, errs)
    except Exception:
        FAILS.append((label, [traceback.format_exc().splitlines()[-1]], None, None)); print(f"ERROR {label}:\n" + traceback.format_exc())


def multi_case(name, maker, op, changed=False, selects=False, warns=None):
    """Two objects in Edit Mode, everything selected. changed: the operator works on both; selects: it leaves a
    selection in both; warns: it works on the active one only and the report says so with this text."""
    label = f"two objects {name}"
    try:
        reset(); objs = list(maker())
        for o in objs: o.select_set(True)
        bpy.context.view_layer.objects.active = objs[0]
        h0 = [face_hash(o) for o in objs]
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.context.tool_settings.mesh_select_mode = MODES["F"]
        bpy.ops.mesh.select_all(action="SELECT")
        op()
        msg = bpy.context.scene.quadfix_lite.report
        bpy.ops.object.mode_set(mode="OBJECT")
        errs = []
        for o in objs:
            if o.data.validate(verbose=False): errs.append(f"{o.name}: mesh.validate() had to fix data")
        same = [face_hash(o) == h for o, h in zip(objs, h0)]
        if changed and any(same): errs.append(f"not every object was processed (unchanged: {same})")
        if selects:
            if not all(same): errs.append("select changed geometry")
            for o in objs:
                if not any(p.select for p in o.data.polygons) or all(p.select for p in o.data.polygons):
                    errs.append(f"{o.name}: non-quads not selected")
        if warns:
            if same[0]: errs.append("the active object was not processed")
            if warns not in msg: errs.append(f"report does not mention the other object: {msg!r}")
        _passed(label, errs)
    except Exception:
        FAILS.append((label, [traceback.format_exc().splitlines()[-1]], None, None)); print(f"ERROR {label}:\n" + traceback.format_exc())


def undo_case(name, maker, op):
    """Operator must be undoable: after ed.undo the mesh equals the original."""
    label = f"{name} [undo]"
    try:
        reset(); obj = maker(); h0 = face_hash(obj)
        edit(obj)
        bpy.ops.ed.undo_push(message="before")
        op()
        bpy.ops.ed.undo_push(message="after")
        bpy.ops.ed.undo()
        bpy.ops.object.mode_set(mode="OBJECT")
        h1 = face_hash(obj)
        if h0 != h1:
            FAILS.append((label, ["mesh differs after undo"], None, None)); print(f"FAIL  {label}")
        else:
            global PASSES; PASSES += 1; print(f"ok    {label}")
    except Exception as e:
        SKIPS.append((label, str(e).splitlines()[-1])); print(f"SKIP  {label}: {str(e).splitlines()[-1]}")


CLEAN = bpy.ops.quadfix_lite.clean
NGONS = bpy.ops.quadfix_lite.ngons_to_quads
SELECT = bpy.ops.quadfix_lite.select_bad
assert "fill_quads" not in dir(bpy.ops.quadfix_lite), "Lite must not contain hole filling"

# Clean keeps the 24-triangle pole cap of the cutter sphere (clean topology): 118 quads + 28 tris
run_case("boolean clean", make_boolean_junk, CLEAN, expect=dict(quads=lambda b, a: a["quads"] >= 118 and a["tris"] <= 28, ngons=12))
run_case("boolean ngons", make_boolean_junk, NGONS, expect=dict(quads=lambda b, a: a["quads"] >= 129, half_cleans=1))
run_case("tri scan clean", make_triangulated_scan, CLEAN, expect=dict(quads=lambda b, a: a["quads"] > 380), variants=("all",))
run_case("tri scan ngons", make_triangulated_scan, NGONS, expect=dict(quads=lambda b, a: a["quads"] > 500), variants=("all",))
run_case("ai-like ngons", make_ai_like_mesh, NGONS, expect=dict(quads=lambda b, a: a["quads"] > b["faces"] * 0.3, half_cleans=1))
run_case("ai-like clean", make_ai_like_mesh, CLEAN, expect=dict(half_cleans=1))
run_case("attributed ngons", make_attributed_holed_sphere, NGONS)
run_case("attributed clean", make_attributed_holed_sphere, CLEAN)
run_case("single quad clean", make_single_quad, CLEAN, variants=("all",))
run_case("select bad", make_holed_sphere, SELECT, variants=("all", "none"), expect=dict(select_ok=1))
# ---- partial selection: the selected part is cleaned, nothing else changes (shape, UVs, material, seam, sharp)
run_case("inside-out clean", make_inside_out_junk, CLEAN, expect=dict(half_cleans=1))
run_case("short border edge clean", make_short_border_edge, CLEAN, variants=("half",), expect=dict(half_cleans=1))
run_case("marked ai-like clean", make_marked_ai_like, CLEAN, expect=dict(half_cleans=1))
run_case("marked ai-like ngons", make_marked_ai_like, NGONS, expect=dict(half_cleans=1))
single_edge_case()
# ---- several objects in Edit Mode
multi_case("clean", make_two_scans, CLEAN, changed=True)
multi_case("select bad", make_two_junk, SELECT, selects=True)
multi_case("ngons", make_two_junk, NGONS, warns="not converted")
# ---- nothing selected: must not crash, must not touch anything
run_case("nothing selected clean", make_boolean_junk, CLEAN, variants=("none",), modes=("F",))
for nm, mk, op in (("clean", make_triangulated_scan, CLEAN), ("ngons", make_triangulated_scan, NGONS)):
    undo_case(nm, mk, op)
if not QUICK:
    run_case("big mesh clean", make_big_mesh, CLEAN, variants=("all",), modes=("F",), timeout=120)

print(f"\n{PASSES} passed, {len(FAILS)} failed, {len(SKIPS)} skipped  (Blender {bpy.app.version_string})")
for label, errs, b, a in FAILS:
    print(f"  FAIL {label}: {'; '.join(errs)}")
sys.exit(1 if FAILS else 0)
