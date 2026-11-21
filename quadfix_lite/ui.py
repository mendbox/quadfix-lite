# SPDX-License-Identifier: GPL-3.0-or-later
import os
import textwrap
import tomllib
from bpy.types import Panel

FULL_URL = "https://mendbox.gumroad.com/l/quadfix"
FULL_TEXT = "Full version fills holes with a clean quad grid (no triangle fans) and bridges two edge loops with quads."


def _version():
    try:
        with open(os.path.join(os.path.dirname(__file__), "blender_manifest.toml"), "rb") as f:
            return tomllib.load(f).get("version", "?")
    except Exception:
        return "?"


VERSION = _version()


def wrapped(layout, context, text, icon):
    """A label cuts long text with "..."; this breaks it into lines that fit the sidebar."""
    chars = max(16, int((context.region.width - 70) / (7.5 * context.preferences.system.ui_scale)))
    for n, line in enumerate(textwrap.wrap(text, chars)):
        layout.label(text=line, icon=icon if n == 0 else "BLANK1")


class QUADFIXLITE_PT_panel(Panel):
    bl_label = "QuadFix Lite"
    bl_idname = "QUADFIXLITE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "QuadFix Lite"
    bl_context = "mesh_edit"

    def draw(self, context):
        p = context.scene.quadfix_lite
        col = self.layout.column(align=True)
        col.prop(p, "only_selected")
        col.separator()
        col.operator("quadfix_lite.select_bad", icon="RESTRICT_SELECT_OFF")
        col.separator()
        box = col.box()
        box.label(text="Quads", icon="MOD_TRIANGULATE")
        box.operator("quadfix_lite.ngons_to_quads", icon="MESH_PLANE")
        box.prop(p, "angle_face")
        box.prop(p, "angle_shape")
        box.prop(p, "keep_planar")
        box.prop(p, "aggressive")
        box = col.box()
        box.label(text="Boolean / Import Cleanup", icon="MOD_BOOLEAN")
        box.operator("quadfix_lite.clean", icon="BRUSH_DATA")
        box.prop(p, "merge_dist")
        box.prop(p, "dissolve_angle")
        box.prop(p, "keep_sharp")
        if p.report:
            col.separator()
            col.label(text=p.report, icon="INFO")
        box = self.layout.box()
        wrapped(box, context, FULL_TEXT, "INFO")
        box.operator("wm.url_open", text="Get the full version", icon="URL").url = FULL_URL
        self.layout.label(text=f"QuadFix Lite v{VERSION}")


classes = (QUADFIXLITE_PT_panel,)
