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
        objs = [act] + [o for o in objs if o.data is not act.data]
    return objs


def _orient_to_neighbours(bm, new_faces):
    """Flip new faces so their winding agrees with the original faces around them
    (a shared edge must be traversed in opposite directions). Propagates through the
    patch from every edge that touches original geometry. Unlike recalc_face_normals
    this never guesses an "outside" and never flips original faces."""
    new = [f for f in new_faces if f.is_valid]
    newset = set(new)
    if not new:
        return
    def consistent(f, g, e):
        lf = next(l for l in f.loops if l.edge is e)
        lg = next(l for l in g.loops if l.edge is e)
        return lf.vert is not lg.vert
    oriented = set()
    queue = []
    for f in new:
        for e in f.edges:
            for g in e.link_faces:
                if g not in newset and g.is_valid:
                    queue.append((f, g, e))
    while queue:
        f, g, e = queue.pop()
        if f in oriented or not f.is_valid:
            continue
        if not consistent(f, g, e):
            f.normal_flip()
        oriented.add(f)
        for e2 in f.edges:
            for h in e2.link_faces:
                if h in newset and h not in oriented:
                    queue.append((h, f, e2))
    # islands of new faces with no original neighbour: leave to a plain recalc
    rest = [f for f in new if f not in oriented]
    if rest:
        bmesh.ops.recalc_face_normals(bm, faces=rest)
    bm.normal_update()


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


def _tris_to_quads(bm, faces, angle_face, angle_shape, aggressive=False, keep_selected=False):
    if aggressive:
        angle_face = angle_shape = radians(180)
    res = bmesh.ops.join_triangles(
        bm, faces=[f for f in faces if len(f.verts) == 3],
        angle_face_threshold=angle_face, angle_shape_threshold=angle_shape,
        cmp_seam=False, cmp_sharp=False, cmp_uvs=False, cmp_vcols=False, cmp_materials=False)
    made = res.get("faces", [])
    # in Face and Edge select mode the triangles and quads made here come back unselected although they replace
    # selected faces: the selection would shrink with every run, and Only Selected would skip them next time
    # (keep_selected: N-gons to Quads and Clean; the hole fill tracks its new faces by selection itself)
    if keep_selected:
        for f in list(faces) + list(made):
            if f.is_valid and not f.select and all(v.select for v in f.verts):
                f.select = True
    return made


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
                                description="Restrict to selected geometry (Edit Mode); off = whole mesh")
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
        faces = [f for f in bm.faces if (f.select or not p.only_selected) and len(f.verts) != 4
                 and not (p.keep_planar and len(f.verts) > 4 and _is_planar(f, 0.01))]
        if not faces:
            self.report({"INFO"}, "Nothing to convert (all quads or flat n-gons)")
            return {"CANCELLED"}
        before = _stats(bm)
        if faces:
            tri = bmesh.ops.triangulate(bm, faces=faces, quad_method="BEAUTY", ngon_method="BEAUTY")
            _tris_to_quads(bm, tri["faces"], p.angle_face, p.angle_shape, p.aggressive, keep_selected=True)
        _update(obj, bm)
        after = _stats(bm)
        p.report = f"n-gons {before[2]}→{after[2]}, tris {before[0]}→{after[0]}, quads {before[1]}→{after[1]}"
        others = len(_edit_objects(context)) - 1
        if others:
            p.report += f" ({others} more objects in Edit Mode not converted: this tool works on the active object)"
        self.report({"WARNING" if others else "INFO"}, p.report)
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
        tot = [0] * 8                             # verts before/after, tris, quads, n-gons before/after
        border = 0
        for obj in objs:
            r = self._clean(obj, p)
            border += r[8]
            tot = [a + b for a, b in zip(tot, r)]
        nv0, nv1, t0, q0, n0, t1, q1, n1 = tot
        p.report = f"verts {nv0}→{nv1}, quads {q0}→{q1}, tris {t0}→{t1}, n-gons {n0}→{n1}"
        if len(objs) > 1:
            p.report = f"{len(objs)} objects: " + p.report
        if border and (nv0, t0, q0, n0) == (nv1, t1, q1, n1):
            # Only Selected never changes a face that is not selected, so a selection of single edges or
            # vertices (their faces are not selected as a whole) gives nothing to do: say so
            p.report = (f"Nothing cleaned: the {border} selected edges touch faces that are not selected. "
                        "Select the faces around them or turn off Only Selected")
        self.report({"INFO"}, p.report)
        return {"FINISHED"}

    @staticmethod
    def _clean(obj, p):
        bm = _bm_from_edit(obj)
        only = p.only_selected
        sel = lambda g: (g.select or not only)
        # Only Selected: a face that is not selected is never changed. A vertex or edge on the border of the
        # selection is selected too, but merging or dissolving it would change the unselected faces around it
        # (a merged vertex is lost from them, a dissolved edge joins them into an n-gon)
        inner = lambda g: not only or all(f.select for f in g.link_faces)
        border = sum(1 for e in bm.edges if e.select and not all(f.select for f in e.link_faces)) if only else 0
        before = _stats(bm)
        nv0 = len(bm.verts)
        # the unselected faces: no step below changes or removes one of them, so the references stay valid
        outside = {f for f in bm.faces if not f.select} if only else set()

        def reselect():
            # the merge, the dissolve and the join return their faces unselected in Face and Edge select mode:
            # select them again when all their vertices are selected, so "Only Selected" keeps working and the
            # selection does not shrink (a face that was unselected before stays out, even when its neighbours
            # make all its vertices selected)
            for f in bm.faces:
                if not f.select and f not in outside and all(v.select for v in f.verts):
                    f.select = True

        bmesh.ops.remove_doubles(bm, verts=[v for v in bm.verts if sel(v) and inner(v)], dist=p.merge_dist)
        bm.verts.ensure_lookup_table()
        bmesh.ops.dissolve_degenerate(bm, dist=p.merge_dist, edges=[e for e in bm.edges if sel(e) and inner(e)])
        reselect()
        loose_v = [v for v in bm.verts if sel(v) and not v.link_edges]
        loose_e = [e for e in bm.edges if sel(e) and not e.link_faces]
        bmesh.ops.delete(bm, geom=loose_e, context="EDGES")
        bmesh.ops.delete(bm, geom=loose_v, context="VERTS")
        verts = [v for v in bm.verts if sel(v) and inner(v)]
        edges = [e for e in bm.edges if sel(e) and inner(e)]
        if p.keep_sharp:
            edges = [e for e in edges if e.smooth]
        # remember original planar n-gons (the ones Keep Planar protects)
        protected = set()
        if p.keep_planar:
            bm.normal_update()  # normals are stale after remove_doubles / dissolve_degenerate
            protected = {f for f in bm.faces if sel(f) and len(f.verts) > 4 and _is_planar(f, 0.01)}
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
        faces = [f for f in bm.faces if sel(f) and len(f.verts) != 4 and f not in protected and not _pole_fan(f)]
        if faces:
            tri = bmesh.ops.triangulate(bm, faces=faces, quad_method="BEAUTY", ngon_method="BEAUTY")
            _tris_to_quads(bm, tri["faces"], p.angle_face, p.angle_shape, p.aggressive, keep_selected=True)
        reselect()
        chosen = [f for f in bm.faces if sel(f)]
        if len(chosen) < len(bm.faces):
            # part of the mesh: the selected patch is open, a recalc would guess its "outside" on its own and
            # can turn it against the faces around it. Follow the winding of the unselected neighbours instead
            _orient_to_neighbours(bm, chosen)
        else:
            bmesh.ops.recalc_face_normals(bm, faces=chosen)
        _update(obj, bm)
        after = _stats(bm)
        return (nv0, len(bm.verts)) + before + after + (border,)


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
        for obj in _edit_objects(context):
            bm = _bm_from_edit(obj)
            for g in (bm.verts, bm.edges, bm.faces):
                for x in g:
                    x.select = False
            for f in bm.faces:
                if len(f.verts) != 4:
                    f.select = True
                    nbad += 1
            for e in bm.edges:
                if e.is_boundary:
                    e.select = True
                    nholes += 1
            bm.select_flush_mode()
            _update(obj, bm)
        context.scene.quadfix_lite.report = f"{nbad} non-quad faces, {nholes} boundary edges"
        self.report({"INFO"}, context.scene.quadfix_lite.report)
        return {"FINISHED"}


classes = (QuadFixLiteProps, QUADFIXLITE_OT_ngons_to_quads, QUADFIXLITE_OT_clean, QUADFIXLITE_OT_select_bad)
