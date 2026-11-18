# QuadFix Lite - N-gons to quads and mesh cleanup for Blender.
# Copyright (C) 2026 Mendbox Studio
# SPDX-License-Identifier: GPL-3.0-or-later
import bpy
import bmesh
from math import radians
from bpy.props import FloatProperty, BoolProperty, PointerProperty
from bpy.types import Operator, PropertyGroup


# ---------------------------------------------------------------- helpers

def _bm_from_edit(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    return bm


def _update(obj, bm):
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)


def _stats(bm):
    tris = sum(1 for f in bm.faces if len(f.verts) == 3)
    quads = sum(1 for f in bm.faces if len(f.verts) == 4)
    ngons = len(bm.faces) - tris - quads
    return tris, quads, ngons


def _edit_objects(context):
    """Every mesh object in Edit Mode (one per mesh datablock), the active one first."""
    act = context.edit_object
    objs = [o for o in getattr(context, "objects_in_mode_unique_data", []) if o.type == "MESH"]
    if act is not None and act.type == "MESH":
        objs = [act] + [o for o in objs if o.data != act.data]
    return objs


def _others_note(context, what):
    """Text for the report of a tool that works on the active object only, when more objects are in Edit Mode."""
    n = len(_edit_objects(context)) - 1
    if n < 1:
        return ""
    return f" ({n} more object{'s' if n != 1 else ''} in Edit Mode not {what}: this tool works on the active object)"


def _orient_patch(bm, faces):
    """Winding of a part of the mesh that was rebuilt while the faces around it stayed as they are.
    Every connected patch is first made consistent in itself, then compared with all its neighbours outside
    the part: it is turned over only when most of the shared edges disagree. One flipped neighbour (common in
    imported meshes) can not turn a correct patch, and no "outside" is guessed for an open patch.
    A patch with no neighbour gets a plain normals recalc."""
    part = {f for f in faces if f.is_valid}

    def agrees(f, g, e):
        lf = next(l for l in f.loops if l.edge is e)
        lg = next(l for l in g.loops if l.edge is e)
        return lf.vert is not lg.vert

    todo = set(part)
    alone = []
    while todo:
        seed = todo.pop()
        patch, stack = [seed], [seed]
        while stack:
            f = stack.pop()
            for e in f.edges:
                lf = e.link_faces
                if len(lf) != 2:
                    continue                      # border or non-manifold edge: says nothing about the winding
                g = lf[0] if lf[1] is f else lf[1]
                if g in todo:
                    todo.discard(g)
                    if not agrees(f, g, e):
                        g.normal_flip()
                    patch.append(g)
                    stack.append(g)
        yes = no = 0
        for f in patch:
            for e in f.edges:
                lf = e.link_faces
                if len(lf) != 2:
                    continue
                g = lf[0] if lf[1] is f else lf[1]
                if g not in part:
                    if agrees(f, g, e): yes += 1
                    else: no += 1
        if yes + no == 0:
            alone += patch
        elif no > yes:
            for f in patch:
                f.normal_flip()
    if alone:
        bmesh.ops.recalc_face_normals(bm, faces=alone)
    bm.normal_update()


def _to_quads_keep_selection(bm, faces, angle_face, angle_shape, aggressive):
    """Triangulate and join `faces`. In Face and Edge select mode the faces made here come back unselected, so the
    selection would shrink with every run: what replaces selected faces is selected again. Selected and
    unselected faces (Only Selected off) are converted apart, so nothing unselected ends up selected."""
    for chosen in (True, False):
        group = [f for f in faces if f.is_valid and f.select == chosen]
        if not group:
            continue
        tri = bmesh.ops.triangulate(bm, faces=group, quad_method="BEAUTY", ngon_method="BEAUTY")
        made = _tris_to_quads(bm, tri["faces"], angle_face, angle_shape, aggressive)
        if chosen:
            for f in list(tri["faces"]) + list(made):
                if f.is_valid:
                    f.select = True


def _is_planar(f, tol):
    """True if all verts lie within tol*face_size of the face plane."""
    if len(f.verts) <= 4:
        return True
    n = f.normal
    if n.length == 0:
        return False
    c = f.calc_center_median()
    size = max((v.co - c).length for v in f.verts) or 1.0
    return all(abs((v.co - c).dot(n)) <= tol * size for v in f.verts)


def _tris_to_quads(bm, faces, angle_face, angle_shape, aggressive=False):
    if aggressive:
        angle_face = angle_shape = radians(180)
    res = bmesh.ops.join_triangles(
        bm, faces=[f for f in faces if len(f.verts) == 3],
        angle_face_threshold=angle_face, angle_shape_threshold=angle_shape,
        cmp_seam=False, cmp_sharp=False, cmp_uvs=False, cmp_vcols=False, cmp_materials=False)
    return res.get("faces", [])


def _pole_fan(f):
    """A triangle of the cap around a pole (the top of a UV sphere, the end of a cylinder): every face around one
    of its vertices is a triangle, six or more of them, ringed by quads. That is clean topology, not junk."""
    if len(f.verts) != 3:
        return False
    for v in f.verts:
        fan = v.link_faces
        if len(fan) < 6 or any(len(g.verts) != 3 for g in fan):
            continue
        rim = [e for g in fan for e in g.edges if v not in e.verts]
        if all(any(len(h.verts) != 3 for h in e.link_faces) for e in rim):
            return True
    return False


# ---------------------------------------------------------------- properties

class QuadFixLiteProps(PropertyGroup):
    merge_dist: FloatProperty(name="Merge Distance", default=0.0001, min=0.0, precision=5,
                              description="Merge vertices closer than this")
    angle_face: FloatProperty(name="Face Angle", default=radians(90), min=0, max=radians(180), subtype="ANGLE",
                              description="Max angle between triangle normals to join into a quad")
    angle_shape: FloatProperty(name="Shape Angle", default=radians(90), min=0, max=radians(180), subtype="ANGLE",
                               description="Max deviation from a rectangle to accept a quad")
    keep_sharp: BoolProperty(name="Keep Sharp Edges", default=True,
                             description="Do not dissolve edges marked sharp")
    dissolve_angle: FloatProperty(name="Dissolve Angle", default=radians(5), min=0, max=radians(90), subtype="ANGLE",
                                  description="Limited dissolve angle for boolean cleanup")
    only_selected: BoolProperty(name="Only Selected", default=True,
                                description="Work on the selected faces only; off = the whole visible mesh. Faces that "
                                            "are not selected never change: Clean & Quad leaves the vertices and "
                                            "edges on the border of the selection as they are")
    keep_planar: BoolProperty(name="Keep Planar N-gons", default=True,
                              description="Leave flat n-gons untouched (clean on flat surfaces); only convert curved n-gons and triangles")
    aggressive: BoolProperty(name="Aggressive Quads", default=False,
                             description="Join any two triangles into a quad regardless of angle (fewest triangles, less regular shapes)")
    report: bpy.props.StringProperty(name="Report", default="")


def register_props():
    bpy.types.Scene.quadfix_lite = PointerProperty(type=QuadFixLiteProps)


def unregister_props():
    del bpy.types.Scene.quadfix_lite


# ---------------------------------------------------------------- operators

class QUADFIXLITE_OT_ngons_to_quads(Operator):
    """Convert selected n-gons and triangles into quads"""
    bl_idname = "quadfix_lite.ngons_to_quads"
    bl_label = "N-gons to Quads"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "EDIT_MESH" and context.edit_object

    def execute(self, context):
        p = context.scene.quadfix_lite
        obj = context.edit_object
        bm = _bm_from_edit(obj)
        bm.normal_update()
        # hidden faces are never worked on (Only Selected off = the whole visible mesh)
        wanted = lambda f: ((f.select or not p.only_selected) and not f.hide and len(f.verts) != 4
                            and not (p.keep_planar and len(f.verts) > 4 and _is_planar(f, 0.01)))
        faces = [f for f in bm.faces if wanted(f)]
        note = _others_note(context, "converted")
        if not faces:
            p.report = "Nothing to convert (all quads or flat n-gons)" + note
            self.report({"WARNING" if note else "INFO"}, p.report)
            return {"CANCELLED"}
        before = _stats(bm)
        if faces:
            _to_quads_keep_selection(bm, faces, p.angle_face, p.angle_shape, p.aggressive)
        _update(obj, bm)
        after = _stats(bm)
        p.report = f"n-gons {before[2]}→{after[2]}, tris {before[0]}→{after[0]}, quads {before[1]}→{after[1]}" + note
        self.report({"WARNING" if note else "INFO"}, p.report)
        return {"FINISHED"}


class QUADFIXLITE_OT_clean(Operator):
    """Clean boolean/imported mesh: merge doubles, remove degenerate & loose geometry, limited dissolve, quads, normals"""
    bl_idname = "quadfix_lite.clean"
    bl_label = "Clean & Quad"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "EDIT_MESH" and context.edit_object

    def execute(self, context):
        p = context.scene.quadfix_lite
        objs = _edit_objects(context)
        tot = [0] * 10          # verts before/after, tris, quads, n-gons before/after, selected faces, selected edges
        for obj in objs:
            tot = [a + b for a, b in zip(tot, self._clean(obj, p))]
        nv0, nv1, t0, q0, n0, t1, q1, n1, sel_faces, sel_edges = tot
        p.report = f"verts {nv0}→{nv1}, quads {q0}→{q1}, tris {t0}→{t1}, n-gons {n0}→{n1}"
        if len(objs) > 1:
            p.report = f"{len(objs)} objects: " + p.report
        if p.only_selected and not sel_faces and sel_edges and (nv0, t0, q0, n0) == (nv1, t1, q1, n1):
            # Only Selected never changes a face that is not selected: single selected edges or vertices
            # (no face selected as a whole) leave nothing to do. Say so instead of doing nothing silently
            p.report = (f"Nothing cleaned: {sel_edges} selected edge{'s' if sel_edges != 1 else ''} but no selected "
                        "face. Select the faces around them or turn off Only Selected")
        self.report({"INFO"}, p.report)
        return {"FINISHED"}

    @staticmethod
    def _clean(obj, p):
        bm = _bm_from_edit(obj)
        only = p.only_selected
        # what the operator may work on: the selection, or with Only Selected off everything that is not hidden.
        # A face outside of that is never changed. A vertex or edge on the border is "in" too (it is selected,
        # or visible), but merging or dissolving it would change the faces outside around it: a merged vertex is
        # lost from them or moves, a dissolved edge joins them into an n-gon. `inner` = no face outside around it
        use = (lambda g: g.select) if only else (lambda g: not g.hide)
        inner = lambda g: all(use(f) for f in g.link_faces)
        sel_faces = sum(1 for f in bm.faces if f.select)
        sel_edges = sum(1 for e in bm.edges if e.select)
        before = _stats(bm)
        nv0 = len(bm.verts)
        # the selection as it came in: faces that were not selected never become selected
        outside = {f for f in bm.faces if not f.select}
        out_verts = set() if only else {v for f in outside for v in f.verts}

        def reselect():
            # the merge and the dissolve return their faces unselected in Face and Edge select mode: select them
            # again when all their vertices are selected, so "Only Selected" keeps working and the selection does
            # not shrink. A face that was unselected before stays out, even when its neighbours make all its
            # vertices selected; with Only Selected off unselected faces are rebuilt too, and what is made of
            # them (all its vertices come from unselected faces) stays out as well
            nonlocal outside, out_verts
            outside = {f for f in outside if f.is_valid}
            out_verts = {v for v in out_verts if v.is_valid}
            for f in bm.faces:
                if f.select or f.hide or f in outside:
                    continue
                if all(v.select for v in f.verts) and not (out_verts and all(v in out_verts for v in f.verts)):
                    f.select = True

        bmesh.ops.remove_doubles(bm, verts=[v for v in bm.verts if use(v) and inner(v)], dist=p.merge_dist)
        bm.verts.ensure_lookup_table()
        # a degenerate edge collapses to its middle and a needle triangle loses its short edge: both ends of
        # every edge passed here must be inner, or a vertex on the border would move or disappear
        bmesh.ops.dissolve_degenerate(bm, dist=p.merge_dist, edges=[
            e for e in bm.edges if use(e) and inner(e) and inner(e.verts[0]) and inner(e.verts[1])])
        reselect()
        loose_v = [v for v in bm.verts if use(v) and not v.link_edges]
        loose_e = [e for e in bm.edges if use(e) and not e.link_faces]
        bmesh.ops.delete(bm, geom=loose_e, context="EDGES")
        bmesh.ops.delete(bm, geom=loose_v, context="VERTS")
        verts = [v for v in bm.verts if use(v) and inner(v)]
        edges = [e for e in bm.edges if use(e) and inner(e)]
        if p.keep_sharp:
            edges = [e for e in edges if e.smooth]
        # remember original planar n-gons (the ones Keep Planar protects)
        protected = set()
        if p.keep_planar:
            bm.normal_update()  # normals are stale after remove_doubles / dissolve_degenerate
            protected = {f for f in bm.faces if use(f) and len(f.verts) > 4 and _is_planar(f, 0.01)}
        # the dissolve only works where the boolean / import left junk (triangles, n-gons): between two quads the
        # grid is already clean, and on a finely curved surface (a sphere near its pole) the faces meet flatter
        # than the dissolve angle, so a clean grid would be torn up
        junk = lambda f: len(f.verts) != 4 and not _pole_fan(f)
        edges = [e for e in edges if any(junk(f) for f in e.link_faces) or len(e.link_faces) != 2]
        verts = [v for v in verts if any(junk(f) for f in v.link_faces)]
        bmesh.ops.dissolve_limit(bm, angle_limit=p.dissolve_angle, use_dissolve_boundaries=False,
                                 verts=verts, edges=edges, delimit={"SHARP", "SEAM"} if p.keep_sharp else set())
        protected = {f for f in protected if f.is_valid}
        reselect()
        # n-gons created by the dissolve itself are always converted; original flat n-gons stay
        faces = [f for f in bm.faces if use(f) and len(f.verts) != 4 and f not in protected and not _pole_fan(f)]
        if faces:
            _to_quads_keep_selection(bm, faces, p.angle_face, p.angle_shape, p.aggressive)
        chosen = [f for f in bm.faces if use(f)]
        if len(chosen) < len(bm.faces):
            # part of the mesh: the patch is open, a recalc would guess its "outside" on its own and can turn it
            # against the faces around it. Keep the winding of the neighbours instead
            _orient_patch(bm, chosen)
        else:
            bmesh.ops.recalc_face_normals(bm, faces=chosen)
        _update(obj, bm)
        after = _stats(bm)
        return (nv0, len(bm.verts)) + before + after + (sel_faces, sel_edges)


class QUADFIXLITE_OT_select_bad(Operator):
    """Select all non-quad faces and boundary (hole) edges"""
    bl_idname = "quadfix_lite.select_bad"
    bl_label = "Select Non-Quads & Holes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "EDIT_MESH" and context.edit_object

    def execute(self, context):
        nbad = nholes = 0
        bms = []
        for obj in _edit_objects(context):
            bm = _bm_from_edit(obj)
            bms.append((obj, bm))
            for g in (bm.verts, bm.edges, bm.faces):
                for x in g:
                    x.select = False
            for f in bm.faces:                    # hidden geometry is not selected and not counted
                if len(f.verts) != 4 and not f.hide:
                    f.select = True
                    nbad += 1
            for e in bm.edges:
                if e.is_boundary and not e.hide:
                    e.select = True
                    nholes += 1
        note = ""
        mode = tuple(context.tool_settings.mesh_select_mode)
        if nholes and not (mode[0] or mode[1]):
            # Face select mode alone can not hold selected edges (they would be dropped): show them
            context.tool_settings.mesh_select_mode = (False, True, True)
            note = " (Edge select mode switched on to show the hole edges)"
        for obj, bm in bms:
            if note:
                bm.select_mode = {"EDGE", "FACE"}
            bm.select_flush_mode()
            _update(obj, bm)
        context.scene.quadfix_lite.report = f"{nbad} non-quad faces, {nholes} boundary edges" + note
        self.report({"INFO"}, context.scene.quadfix_lite.report)
        return {"FINISHED"}


classes = (QuadFixLiteProps, QUADFIXLITE_OT_ngons_to_quads, QUADFIXLITE_OT_clean, QUADFIXLITE_OT_select_bad)
