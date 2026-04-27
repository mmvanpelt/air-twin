"""
airtwin_addon.py — Blender addon for Air Twin scene setup and export.

Asset agnostic — reads available assets from asset_registry.json.
No device-specific knowledge baked in.

Installation:
    Edit → Preferences → Add-ons → Install → select this file → Enable
    Then set project root in addon preferences.

Usage:
    Press N in viewport → Air Twin tab
"""

bl_info = {
    "name": "Air Twin",
    "author": "Air Twin Project",
    "version": (1, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Air Twin",
    "description": "Air Twin scene setup, asset tagging, and export pipeline",
    "category": "Import-Export",
}

import bpy
import json
from pathlib import Path
from bpy.props import StringProperty, FloatProperty, EnumProperty, BoolProperty
from bpy.types import Panel, Operator, PropertyGroup, AddonPreferences


# ---------------------------------------------------------------------------
# Addon preferences
# ---------------------------------------------------------------------------

class AirTwinPreferences(AddonPreferences):
    bl_idname = __name__

    project_root: StringProperty(
        name="Project Root",
        description="Path to air-twin project root (contains assets/, data/, frontend/)",
        default=r"C:\Users\mvnpl\air-twin",
        subtype='DIR_PATH',
    )

    def draw(self, context):
        self.layout.prop(self, "project_root")


def get_project_root() -> Path:
    prefs = bpy.context.preferences.addons[__name__].preferences
    return Path(prefs.project_root)


def load_registry() -> dict:
    """Load asset_registry.json from project root. Returns empty dict on failure."""
    try:
        path = get_project_root() / "data" / "asset_registry.json"
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"assets": {}}


def get_asset_items(scene, context):
    """
    Dynamic enum items for asset ID dropdown.
    Reads asset IDs from asset_registry.json at draw time.
    """
    registry = load_registry()
    items = []
    for asset_id in registry.get("assets", {}).keys():
        if not asset_id.startswith("_"):
            asset = registry["assets"][asset_id]
            asset_type = asset.get("asset_type", "unknown")
            label = f"{asset_id} ({asset_type})"
            items.append((asset_id, label, asset_id))

    if not items:
        items.append(("none", "No assets found — check registry path", ""))

    return items


ASSET_TYPE_ITEMS = [
    ("purifier", "Purifier", "Air purifier device"),
    ("sensor",   "Sensor",   "PM2.5 or other sensor"),
    ("room",     "Room",     "Room geometry mesh"),
    ("other",    "Other",    "Other asset type"),
]


# ---------------------------------------------------------------------------
# Scene properties
# ---------------------------------------------------------------------------

class AirTwinSceneProps(PropertyGroup):
    floor_slab_thickness: FloatProperty(
        name="Floor Slab Thickness (m)",
        default=0.0,
        precision=3,
    )
    room_volume: FloatProperty(
        name="Room Volume (m3)",
        default=0.0,
        precision=1,
    )
    analysis_done: BoolProperty(default=False)

    tag_asset_id: EnumProperty(
        name="Asset ID",
        description="Asset ID from registry to assign to selected object",
        items=get_asset_items,
    )
    tag_asset_type: EnumProperty(
        name="Type",
        description="Asset type",
        items=ASSET_TYPE_ITEMS,
        default="purifier",
    )
    tag_custom_id: StringProperty(
        name="Custom ID",
        description="Type a custom asset ID if not in registry",
        default="",
    )
    use_custom_id: BoolProperty(
        name="Use Custom ID",
        description="Enter a custom asset ID instead of selecting from registry",
        default=False,
    )


# ---------------------------------------------------------------------------
# Operator: Analyse Room
# ---------------------------------------------------------------------------

class AIRTWIN_OT_AnalyseRoom(Operator):
    bl_idname = "airtwin.analyse_room"
    bl_label = "Analyse Room"
    bl_description = "Detect floor slab thickness and calculate room volume from scan"

    def execute(self, context):
        room = bpy.data.objects.get('room')
        if room is None:
            self.report({'ERROR'}, "No object named 'room' found. Tag room mesh first.")
            return {'CANCELLED'}

        coords = [(room.matrix_world @ v.co) for v in room.data.vertices]

        floor_zs = sorted(set(round(c.z, 2) for c in coords if c.z < 0.2))

        slab_thickness = 0.0
        if len(floor_zs) >= 2:
            slab_bottom = floor_zs[0]
            slab_top = slab_bottom
            for z in floor_zs:
                if z - slab_bottom < 0.15:
                    slab_top = z
                else:
                    break
            slab_thickness = round(slab_top - slab_bottom + 0.001, 3)

        min_x = min(c.x for c in coords)
        max_x = max(c.x for c in coords)
        min_y = min(c.y for c in coords)
        max_y = max(c.y for c in coords)
        height = round(max(c.z for c in coords) - slab_thickness, 3)
        bbox_x = max_x - min_x
        bbox_y = max_y - min_y

        floor_coords = [c for c in coords if c.z < slab_thickness + 0.05]
        from collections import defaultdict
        x_slices = defaultdict(list)
        for c in floor_coords:
            x_slices[round(c.x, 1)].append(c.y)

        extents = []
        for x, ys in sorted(x_slices.items()):
            if len(ys) > 5:
                extent = max(ys) - min(ys)
                if extent > 0.5:
                    extents.append((x, extent))

        if extents:
            full = [e for e in extents if e[1] > bbox_y * 0.8]
            partial = [e for e in extents if e[1] < bbox_y * 0.7]
            if full and partial:
                wide_vol = len(full) * 0.1 * max(e[1] for e in full) * height
                narrow_vol = len(partial) * 0.1 * max(e[1] for e in partial) * height
                volume = round(wide_vol + narrow_vol, 1)
            else:
                volume = round(bbox_x * bbox_y * height * 0.75, 1)
        else:
            volume = round(bbox_x * bbox_y * height, 1)

        props = context.scene.airtwin
        props.floor_slab_thickness = slab_thickness
        props.room_volume = volume
        props.analysis_done = True

        self.report({'INFO'},
            f"Slab: {slab_thickness*1000:.0f}mm | "
            f"{bbox_x:.1f}x{bbox_y:.1f}x{height:.1f}m | "
            f"Vol: {volume} m3"
        )
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Snap to Floor
# ---------------------------------------------------------------------------

class AIRTWIN_OT_SnapToFloor(Operator):
    bl_idname = "airtwin.snap_to_floor"
    bl_label = "Snap Selected to Floor"
    bl_description = "Snap selected object base to top of floor slab"

    def execute(self, context):
        props = context.scene.airtwin
        if not props.analysis_done:
            self.report({'ERROR'}, "Run Analyse Room first to detect slab thickness")
            return {'CANCELLED'}

        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Select a mesh object first")
            return {'CANCELLED'}

        slab_top = props.floor_slab_thickness
        min_z = min((obj.matrix_world @ v.co).z for v in obj.data.vertices)
        obj.location.z += (slab_top - min_z)

        self.report({'INFO'}, f"{obj.name} base at z={slab_top:.3f}m")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Tag Object
# ---------------------------------------------------------------------------

class AIRTWIN_OT_TagObject(Operator):
    bl_idname = "airtwin.tag_object"
    bl_label = "Tag Object"
    bl_description = "Assign airtwin_asset_id and airtwin_type to selected object"

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({'ERROR'}, "No object selected")
            return {'CANCELLED'}

        props = context.scene.airtwin

        if props.use_custom_id and props.tag_custom_id.strip():
            asset_id = props.tag_custom_id.strip()
        else:
            asset_id = props.tag_asset_id

        if not asset_id or asset_id == "none":
            self.report({'ERROR'}, "Select an asset ID or enter a custom ID")
            return {'CANCELLED'}

        asset_type = props.tag_asset_type

        obj['airtwin_asset_id'] = asset_id
        obj['airtwin_type'] = asset_type

        if asset_type == 'room':
            obj.name = 'room'
        else:
            obj.name = asset_id.split('_')[0] if '_' in asset_id else asset_id

        self.report({'INFO'},
            f"Tagged '{obj.name}' — id={asset_id}, type={asset_type}"
        )
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Export All
# ---------------------------------------------------------------------------

class AIRTWIN_OT_ExportAll(Operator):
    bl_idname = "airtwin.export_all"
    bl_label = "Export All"
    bl_description = "Update registry + config + export glb files for all tagged objects"

    def execute(self, context):
        root = get_project_root()
        props = context.scene.airtwin

        tagged = {
            obj['airtwin_asset_id']: obj
            for obj in bpy.data.objects
            if 'airtwin_asset_id' in obj
        }
        room_obj = next(
            (obj for obj in bpy.data.objects if obj.get('airtwin_type') == 'room'),
            None
        )

        if not tagged:
            self.report({'ERROR'}, "No tagged objects found.")
            return {'CANCELLED'}

        if room_obj is None:
            self.report({'ERROR'}, "No room object tagged.")
            return {'CANCELLED'}

        # Update asset_registry.json
        registry_path = root / "data" / "asset_registry.json"
        try:
            with open(registry_path) as f:
                registry = json.load(f)

            for asset_id, obj in tagged.items():
                loc = obj.location
                position = [round(loc.x, 3), round(loc.y, 3), round(loc.z, 3)]
                asset_type = obj.get('airtwin_type', 'unknown')

                if asset_id in registry.get('assets', {}):
                    registry['assets'][asset_id]['placement']['position_m'] = position
                    registry['assets'][asset_id]['placement']['position_source'] = 'blender_export'
                    registry['assets'][asset_id]['placement']['blender_object_name'] = obj.name
                else:
                    registry.setdefault('assets', {})[asset_id] = {
                        "asset_type": asset_type,
                        "device_profile": asset_id.rsplit('_', 1)[0] if '_' in asset_id else asset_id,
                        "placement": {
                            "position_m": position,
                            "blender_object_name": obj.name,
                            "position_source": "blender_export",
                        },
                        "commissioned_at": None,
                        "notes": f"Tagged in Blender as {asset_type}",
                    }

            with open(registry_path, 'w') as f:
                json.dump(registry, f, indent=2)
            self.report({'INFO'}, f"Registry updated — {len(tagged)} asset(s)")

        except Exception as e:
            self.report({'ERROR'}, f"Registry update failed: {e}")
            return {'CANCELLED'}

        # Update config.json volume
        if props.analysis_done and props.room_volume > 0:
            config_path = root / "assets" / "config.json"
            try:
                with open(config_path) as f:
                    config = json.load(f)
                config['room']['volume_m3'] = props.room_volume
                config['room']['floor_slab_thickness_m'] = props.floor_slab_thickness
                config['room']['scan_derived'] = True
                with open(config_path, 'w') as f:
                    json.dump(config, f, indent=2)
                self.report({'INFO'}, f"Volume updated: {props.room_volume} m3")
            except Exception as e:
                self.report({'WARNING'}, f"Config update failed: {e}")

        # Export glb files
        export_path = root / "frontend" / "assets"
        export_path.mkdir(parents=True, exist_ok=True)

        # Room
        try:
            bpy.ops.object.select_all(action='DESELECT')
            room_obj.select_set(True)
            bpy.context.view_layer.objects.active = room_obj
            bpy.ops.export_scene.gltf(
                filepath=str(export_path / "room.glb"),
                use_selection=True,
                export_format='GLB',
                export_extras=True,
            )
            self.report({'INFO'}, "room.glb exported")
        except Exception as e:
            self.report({'ERROR'}, f"room.glb failed: {e}")
            return {'CANCELLED'}

        # Each tagged asset
        for asset_id, obj in tagged.items():
            filename = f"{obj.name}.glb"
            try:
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                bpy.ops.export_scene.gltf(
                    filepath=str(export_path / filename),
                    use_selection=True,
                    export_format='GLB',
                    export_extras=True,
                )
                self.report({'INFO'}, f"{filename} exported")
            except Exception as e:
                self.report({'ERROR'}, f"{filename} failed: {e}")
                return {'CANCELLED'}

        self.report({'INFO'}, "Export complete")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Sidebar Panel
# ---------------------------------------------------------------------------

class AIRTWIN_PT_Panel(Panel):
    bl_label = "Air Twin"
    bl_idname = "AIRTWIN_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Air Twin'

    def draw(self, context):
        layout = self.layout
        props = context.scene.airtwin

        box = layout.box()
        box.label(text="Room Analysis", icon='MESH_DATA')
        box.operator("airtwin.analyse_room", icon='VIEWZOOM')
        if props.analysis_done:
            col = box.column(align=True)
            col.label(text=f"Slab: {props.floor_slab_thickness*1000:.0f}mm")
            col.label(text=f"Volume: {props.room_volume} m3")

        layout.separator()

        box = layout.box()
        box.label(text="Positioning", icon='OBJECT_ORIGIN')
        box.operator("airtwin.snap_to_floor", icon='TRIA_DOWN')

        layout.separator()

        box = layout.box()
        box.label(text="Tag Selected Object", icon='BOOKMARKS')
        col = box.column(align=True)
        col.prop(props, "tag_asset_type")
        col.prop(props, "use_custom_id")
        if props.use_custom_id:
            col.prop(props, "tag_custom_id")
        else:
            col.prop(props, "tag_asset_id")
        box.operator("airtwin.tag_object", icon='BOOKMARKS')

        layout.separator()

        box = layout.box()
        box.label(text="Export Pipeline", icon='EXPORT')
        box.operator("airtwin.export_all", icon='PLAY')

        layout.separator()

        box = layout.box()
        box.label(text="Scene Status", icon='INFO')
        room_obj = next(
            (obj for obj in bpy.data.objects if obj.get('airtwin_type') == 'room'),
            None
        )
        if room_obj:
            box.label(text=f"room: tagged", icon='CHECKMARK')
        else:
            box.label(text="room: not tagged", icon='ERROR')

        tagged = [obj for obj in bpy.data.objects if 'airtwin_asset_id' in obj]
        for obj in tagged:
            box.label(
                text=f"{obj['airtwin_asset_id']} ({obj.get('airtwin_type','?')})",
                icon='CHECKMARK'
            )
        if not tagged:
            box.label(text="No assets tagged", icon='INFO')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = [
    AirTwinPreferences,
    AirTwinSceneProps,
    AIRTWIN_OT_AnalyseRoom,
    AIRTWIN_OT_SnapToFloor,
    AIRTWIN_OT_TagObject,
    AIRTWIN_OT_ExportAll,
    AIRTWIN_PT_Panel,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.airtwin = bpy.props.PointerProperty(type=AirTwinSceneProps)
    print("Air Twin addon v1.1 registered")


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.airtwin
    print("Air Twin addon unregistered")


if __name__ == "__main__":
    register()