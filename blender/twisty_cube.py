bl_info = {
    "name": "Twisty Cube 3x3",
    "author": "sunnykrdesign",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar (N) > Twisty Cube",
    "description": "An interactive 3x3 puzzle cube: drag stickers to turn layers, scramble, undo and solve",
    "category": "3D View",
}

"""
Twisty Cube for Blender.

Two ways to use it:
  * Scripting tab -> open this file -> Run Script. It registers the add-on and builds the cube.
  * Edit -> Preferences -> Add-ons -> Install... -> pick this file, then enable "Twisty Cube 3x3".

Then press N in the 3D Viewport, open the "Twisty Cube" tab and click "Play".
While playing:
  * Left-drag a sticker to turn its layer.
  * Left-drag empty space (or middle-drag) to look around; scroll to zoom.
  * Keys U D L R F B M E S turn a layer, Shift for counter-clockwise, Ctrl+Z undoes a turn.
  * Esc stops playing.

Blender is Z-up, so: Up = +Z, Front = -Y, Right = +X.
"""

import math
import random
import time

import bpy
import bmesh
from bpy_extras import view3d_utils
from mathutils import Matrix, Quaternion, Vector

# ---------------------------------------------------------------------------
# Puzzle definition
# ---------------------------------------------------------------------------

# face -> (outward normal, linear RGB colour). Matches the web version: yellow top, blue front, red right.
FACES = {
    "U": ((0, 0, 1), (1.0, 0.66, 0.0)),
    "D": ((0, 0, -1), (0.9, 0.9, 0.9)),
    "F": ((0, -1, 0), (0.012, 0.10, 0.69)),
    "B": ((0, 1, 0), (0.004, 0.33, 0.084)),
    "R": ((1, 0, 0), (0.69, 0.017, 0.023)),
    "L": ((-1, 0, 0), (1.0, 0.195, 0.0)),
}

# letter -> (axis index 0=X 1=Y 2=Z, layer, rotation sign of a clockwise turn seen from that side)
NOTATION = {
    "R": (0, 1, -1), "L": (0, -1, 1), "M": (0, 0, 1),
    "U": (2, 1, -1), "D": (2, -1, 1), "E": (2, 0, 1),
    "F": (1, -1, 1), "B": (1, 1, -1), "S": (1, 0, 1),
}
AXES = "XYZ"

TURN_TIME = 0.20       # seconds per turn when you play
FAST_TIME = 0.08       # seconds per turn while scrambling
COLLECTION = "Twisty Cube"


class State:
    queue = []          # pending moves
    active = None       # move being animated
    history = []        # every completed move, for Undo and Solve
    moves = 0           # turns made by the player since the last scramble
    scrambled = False
    timing = False
    t_start = 0.0
    t_end = 0.0
    auto = None         # "scramble" / "solve" while the add-on is turning by itself
    status = "Solved"
    playing = False
    last = ""


# ---------------------------------------------------------------------------
# Building the cube
# ---------------------------------------------------------------------------

def _material(name, color, roughness):
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*color, 1.0)
        bsdf.inputs["Roughness"].default_value = roughness
    mat.diffuse_color = (*color, 1.0)   # colour in Solid view
    mat.roughness = roughness
    return mat


def _body_mesh():
    me = bpy.data.meshes.get("TwistyBody")
    if me:
        return me
    me = bpy.data.meshes.new("TwistyBody")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=0.96)
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    me.materials.append(_material("Twisty Plastic", (0.008, 0.008, 0.01), 0.45))
    return me


def _sticker_mesh(face):
    name = "TwistySticker_" + face
    me = bpy.data.meshes.get(name)
    if me:
        return me
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    half, r, seg = 0.39, 0.09, 6          # rounded square: half-size, corner radius, segments per corner
    pts = []
    for cx, cy, start in ((half - r, half - r, 0), (-half + r, half - r, 90),
                          (-half + r, -half + r, 180), (half - r, -half + r, 270)):
        for i in range(seg + 1):
            a = math.radians(start + 90 * i / seg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a), 0))
    bm.faces.new([bm.verts.new(p) for p in pts])
    bm.to_mesh(me)
    bm.free()
    me.materials.append(_material("Twisty " + face, FACES[face][1], 0.25))
    return me


def cubies():
    return [o for o in bpy.data.objects if o.get("twisty_cubie")]


def _collection():
    col = bpy.data.collections.get(COLLECTION)
    if not col:
        col = bpy.data.collections.new(COLLECTION)
    scene = bpy.context.scene
    if col.name not in scene.collection.children:
        scene.collection.children.link(col)
    return col


def remove_cube():
    for o in list(bpy.data.objects):
        if o.get("twisty_cubie") or o.get("twisty_face"):
            bpy.data.objects.remove(o, do_unlink=True)


def build_cube():
    remove_cube()
    col = _collection()
    body = _body_mesh()
    for x in (-1, 0, 1):
        for y in (-1, 0, 1):
            for z in (-1, 0, 1):
                c = bpy.data.objects.new(f"Cubie {x:+d} {y:+d} {z:+d}", body)
                c["twisty_cubie"] = True
                c.location = (x, y, z)
                bev = c.modifiers.new("Bevel", "BEVEL")
                bev.width = 0.07
                bev.segments = 3
                bev.limit_method = "ANGLE"
                if hasattr(bev, "harden_normals"):
                    bev.harden_normals = True
                col.objects.link(c)
                for face, (n, _) in FACES.items():
                    if any(n[i] and n[i] != (x, y, z)[i] for i in range(3)):
                        continue
                    s = bpy.data.objects.new(f"Sticker {face}", _sticker_mesh(face))
                    s["twisty_face"] = face
                    s["twisty_n"] = n
                    nv = Vector(n)
                    s.parent = c
                    s.location = nv * 0.4815
                    s.rotation_mode = "QUATERNION"
                    s.rotation_quaternion = Vector((0, 0, 1)).rotation_difference(nv)
                    s.hide_select = True
                    col.objects.link(s)
    _reset_state()


def setup_scene(context):
    """Camera, lights, world and viewport so the cube looks like the photo."""
    col = _collection()
    scene = context.scene

    cam = bpy.data.objects.get("Twisty Camera")
    if not cam:
        cam = bpy.data.objects.new("Twisty Camera", bpy.data.cameras.new("Twisty Camera"))
        col.objects.link(cam)
    cam.location = (6.2, -7.6, 6.0)
    cam.rotation_mode = "QUATERNION"
    cam.rotation_quaternion = (Vector((0, 0, 0)) - cam.location).to_track_quat("-Z", "Y")
    cam.data.lens = 55
    scene.camera = cam

    for name, loc, energy, size in (("Twisty Key", (3, -4, 8), 650, 5), ("Twisty Fill", (-6, -2, 3), 180, 6)):
        lt = bpy.data.objects.get(name)
        if not lt:
            lt = bpy.data.objects.new(name, bpy.data.lights.new(name, "AREA"))
            col.objects.link(lt)
        lt.data.energy = energy
        lt.data.size = size
        lt.location = loc
        lt.rotation_mode = "QUATERNION"
        lt.rotation_quaternion = (Vector((0, 0, 0)) - Vector(loc)).to_track_quat("-Z", "Y")

    floor = bpy.data.objects.get("Twisty Floor")
    if not floor:
        me = bpy.data.meshes.new("Twisty Floor")
        bm = bmesh.new()
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=20)
        bm.to_mesh(me)
        bm.free()
        me.materials.append(_material("Twisty Floor", (0.62, 0.63, 0.66), 0.8))
        floor = bpy.data.objects.new("Twisty Floor", me)
        floor.hide_select = True
        col.objects.link(floor)
    floor.location = (0, 0, -1.56)

    try:
        scene.view_settings.view_transform = "Standard"   # keep the sticker colours vivid
    except TypeError:
        pass

    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (0.6, 0.61, 0.64, 1)
        bg.inputs["Strength"].default_value = 0.8

    screen = getattr(context, "screen", None)
    if screen:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            space.shading.type = "MATERIAL"
            rv3d = space.region_3d
            rv3d.view_perspective = "PERSP"
            rv3d.view_location = (0, 0, 0)
            rv3d.view_rotation = cam.rotation_quaternion
            rv3d.view_distance = 12


# ---------------------------------------------------------------------------
# Move engine
# ---------------------------------------------------------------------------

def _reset_state():
    State.queue = []
    State.active = None
    State.history = []
    State.moves = 0
    State.scrambled = False
    State.timing = False
    State.t_start = State.t_end = 0.0
    State.auto = None
    State.status = "Solved"
    State.last = ""


def move_name(m):
    for letter, (axis, layer, cw) in NOTATION.items():
        if axis == m["axis"] and layer == m["layer"]:
            return letter + ("" if m["dir"] == cw else "'")
    return "?"


def enqueue(axis, layer, direction, **opts):
    m = {"axis": axis, "layer": layer, "dir": direction, "record": True, "time": TURN_TIME}
    m.update(opts)
    State.queue.append(m)


def busy():
    return bool(State.auto or State.active or State.queue)


def user_turn(letter, prime=False):
    if State.auto or len(State.queue) > 2 or not cubies():
        return
    axis, layer, cw = NOTATION[letter]
    enqueue(axis, layer, -cw if prime else cw, user=True)


def _start_next():
    m = State.queue.pop(0)
    group = [c for c in cubies() if round(c.matrix_basis.translation[m["axis"]]) == m["layer"]]
    State.active = {"m": m, "group": group, "starts": [c.matrix_basis.copy() for c in group],
                    "t0": time.perf_counter()}


def _set_angle(angle):
    a = State.active
    rot = Matrix.Rotation(angle, 4, AXES[a["m"]["axis"]])
    for c, start in zip(a["group"], a["starts"]):
        c.matrix_basis = rot @ start


def _finish_active():
    a = State.active
    m = a["m"]
    rot = Matrix.Rotation(m["dir"] * math.pi / 2, 4, AXES[m["axis"]])
    for c, start in zip(a["group"], a["starts"]):
        mw = rot @ start
        c.matrix_basis = Matrix([[round(v) for v in row] for row in mw])   # snap: no drift after many turns
    State.active = None
    if m.get("record"):
        State.history.append({"axis": m["axis"], "layer": m["layer"], "dir": m["dir"]})
    if m.get("user"):
        State.moves += 1
        if State.scrambled and not State.timing and not State.t_end:
            State.timing = True
            State.t_start = time.perf_counter()
    if m.get("undo"):
        State.moves = max(0, State.moves - 1)
    if m.get("user") or m.get("undo"):
        State.last = " ".join(move_name(h) for h in State.history[-12:])
        check_solved()


def is_solved():
    seen = {}
    for c in cubies():
        r = c.matrix_basis.to_3x3()
        for s in c.children:
            face = s.get("twisty_face")
            if not face:
                continue
            n = r @ Vector(s["twisty_n"])
            key = (round(n.x), round(n.y), round(n.z))
            if seen.setdefault(key, face) != face:
                return False
    return True


def check_solved():
    if is_solved():
        if State.timing:
            State.timing = False
            State.t_end = time.perf_counter()
        State.status = "Solved!" if State.t_end else "Solved"
        State.scrambled = False
    else:
        State.status = "Scrambled"


def _end_auto():
    kind, State.auto = State.auto, None
    if kind == "scramble":
        State.scrambled = True
        check_solved()
    elif kind == "solve":
        State.moves = 0
        State.last = ""
        check_solved()


def step():
    """Advance the animation. Returns True while something is moving."""
    if State.active:
        a = State.active
        t = min(1.0, (time.perf_counter() - a["t0"]) / max(a["m"]["time"], 1e-6))
        e = 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2
        _set_angle(a["m"]["dir"] * math.pi / 2 * e)
        if t >= 1.0:
            _finish_active()
        return True
    if State.queue:
        _start_next()
        return True
    if State.auto:
        _end_auto()
        return True
    return False


def run_instantly():
    """Apply every queued move at once (used by tests and when no viewport is open)."""
    while State.queue or State.active:
        if not State.active:
            _start_next()
        _finish_active()
    if State.auto:
        _end_auto()


def scramble(n=22):
    if busy() or not cubies():
        return
    prev = None
    for _ in range(n):
        axis = random.choice([a for a in range(3) if a != prev])
        prev = axis
        enqueue(axis, random.choice((-1, 1)), random.choice((-1, 1)), time=FAST_TIME)
    State.auto = "scramble"
    State.moves = 0
    State.timing = False
    State.t_start = State.t_end = 0.0
    State.last = ""
    State.status = "Scrambling..."


def solve():
    if busy() or not State.history:
        return
    steps = list(reversed(State.history))
    State.history = []
    t = max(0.05, min(0.14, 2.6 / len(steps)))
    for m in steps:
        enqueue(m["axis"], m["layer"], -m["dir"], record=False, time=t)
    State.auto = "solve"
    State.timing = False
    State.scrambled = False
    State.status = "Solving..."


def undo():
    if busy() or not State.history:
        return
    m = State.history.pop()
    enqueue(m["axis"], m["layer"], -m["dir"], record=False, undo=True, time=0.16)


def _redraw():
    wm = bpy.data.window_managers[0] if bpy.data.window_managers else None
    if not wm:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _timer():
    moving = step()
    if moving or State.timing:
        _redraw()
    return 1 / 60 if (moving or State.timing) else 0.1


def fmt_time():
    if State.timing:
        secs = time.perf_counter() - State.t_start
    elif State.t_end:
        secs = State.t_end - State.t_start
    else:
        secs = 0.0
    return f"{int(secs // 60)}:{secs % 60:04.1f}"


# ---------------------------------------------------------------------------
# Mouse: drag a sticker to turn its layer
# ---------------------------------------------------------------------------

def pick_turn(region, rv3d, hit, normal, cubie, dx, dy, project=None):
    """Work out which layer turn a drag means.

    For each axis lying in the touched face, a +90 degree turn moves the touched point along
    axis x normal. Project that direction to the screen and pick the axis whose motion lines up
    best with the drag; the sign of the match gives the direction.
    """
    project = project or (lambda p: view3d_utils.location_3d_to_region_2d(region, rv3d, p))
    n_ax = max(range(3), key=lambda i: abs(normal[i]))
    n = Vector((0, 0, 0))
    n[n_ax] = 1 if normal[n_ax] > 0 else -1
    drag = Vector((dx, dy))
    if drag.length == 0:
        return None
    drag.normalize()
    p0 = project(hit)
    if p0 is None:
        return None
    best = None
    for a in range(3):
        if a == n_ax:
            continue
        axis_v = Vector((0, 0, 0))
        axis_v[a] = 1
        p1 = project(hit + axis_v.cross(n) * 0.5)
        if p1 is None:
            continue
        sv = Vector(p1) - Vector(p0)
        if sv.length < 1e-6:
            continue
        score = sv.normalized().dot(drag)
        if best is None or abs(score) > abs(best[1]):
            best = (a, score)
    if not best:
        return None
    layer = round(cubie.matrix_basis.translation[best[0]])
    return {"axis": best[0], "layer": layer, "dir": 1 if best[1] > 0 else -1}


class TWISTY_OT_play(bpy.types.Operator):
    """Play with the cube in this viewport: drag stickers to turn, Esc to stop"""
    bl_idname = "twisty.play"
    bl_label = "Play"

    def invoke(self, context, event):
        if context.area is None or context.area.type != "VIEW_3D":
            self.report({"WARNING"}, "Start Play from the 3D Viewport sidebar")
            return {"CANCELLED"}
        if not cubies():
            build_cube()
            setup_scene(context)
        self.area = context.area
        self.region = next(r for r in context.area.regions if r.type == "WINDOW")
        self.rv3d = context.area.spaces.active.region_3d
        self.drag = None
        State.playing = True
        self.area.header_text_set("Twisty Cube: drag a sticker to turn it, drag empty space to look around. "
                                  "U D L R F B M E S (+Shift) turn, Ctrl+Z undo, Esc stop")
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _inside_view(self, event):
        x, y = event.mouse_x, event.mouse_y
        for r in self.area.regions:
            if r.type in {"UI", "HEADER", "TOOL_HEADER", "TOOLS", "NAVIGATION_BAR", "ASSET_SHELF"} \
                    and r.x <= x < r.x + r.width and r.y <= y < r.y + r.height and r.width > 1:
                return False
        r = self.region
        return r.x <= x < r.x + r.width and r.y <= y < r.y + r.height

    def _stop(self):
        State.playing = False
        try:
            self.area.header_text_set(None)
        except ReferenceError:
            pass
        _redraw()

    def modal(self, context, event):
        if not State.playing:
            self._stop()
            return {"FINISHED"}
        if event.type == "ESC" and event.value == "PRESS":
            self._stop()
            return {"FINISHED"}

        if self.drag is not None:
            if event.type == "MOUSEMOVE":
                d = self.drag
                dx, dy = event.mouse_x - d["x"], event.mouse_y - d["y"]
                if d["mode"] == "orbit":
                    rv = self.rv3d
                    spin = Quaternion((0, 0, 1), -dx * 0.008)
                    tilt = Quaternion(rv.view_rotation @ Vector((1, 0, 0)), dy * 0.008)
                    rv.view_rotation = spin @ tilt @ rv.view_rotation
                    d["x"], d["y"] = event.mouse_x, event.mouse_y
                    self.area.tag_redraw()
                elif not d["done"] and math.hypot(dx, dy) > 12:
                    d["done"] = True
                    m = pick_turn(self.region, self.rv3d, d["hit"], d["normal"], d["cubie"], dx, dy)
                    if m and not busy():
                        enqueue(m["axis"], m["layer"], m["dir"], user=True)
                return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "RELEASE":
                self.drag = None
                return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "PRESS" and self._inside_view(event):
            coord = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)
            origin = view3d_utils.region_2d_to_origin_3d(self.region, self.rv3d, coord)
            direction = view3d_utils.region_2d_to_vector_3d(self.region, self.rv3d, coord)
            hit, loc, normal, _i, obj, _m = context.scene.ray_cast(context.evaluated_depsgraph_get(), origin, direction)
            cubie = None
            if hit and obj is not None:
                obj = getattr(obj, "original", obj)
                if obj.get("twisty_cubie"):
                    cubie = obj
                elif obj.parent is not None and obj.parent.get("twisty_cubie"):
                    cubie = obj.parent
            if cubie is not None and not State.auto:
                self.drag = {"mode": "turn", "x": event.mouse_x, "y": event.mouse_y, "done": False,
                             "hit": loc.copy(), "normal": normal.copy(), "cubie": cubie}
            else:
                self.drag = {"mode": "orbit", "x": event.mouse_x, "y": event.mouse_y}
            return {"RUNNING_MODAL"}

        if event.value == "PRESS" and self._inside_view(event) and not event.alt:
            if event.type == "Z" and (event.ctrl or event.oskey):
                undo()
                return {"RUNNING_MODAL"}
            if event.type in NOTATION and not event.ctrl and not event.oskey:
                user_turn(event.type, event.shift)
                return {"RUNNING_MODAL"}

        return {"PASS_THROUGH"}


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

class TWISTY_OT_build(bpy.types.Operator):
    """Build the cube with camera, lights and floor (replaces an existing cube)"""
    bl_idname = "twisty.build"
    bl_label = "Build Cube"

    def execute(self, context):
        build_cube()
        setup_scene(context)
        return {"FINISHED"}


class TWISTY_OT_turn(bpy.types.Operator):
    """Turn one layer"""
    bl_idname = "twisty.turn"
    bl_label = "Turn"
    letter: bpy.props.StringProperty(default="U")
    prime: bpy.props.BoolProperty(default=False)

    @classmethod
    def description(cls, context, props):
        return f"Turn the {props.letter} layer {'counter-clockwise' if props.prime else 'clockwise'}"

    def execute(self, context):
        user_turn(self.letter, self.prime)
        return {"FINISHED"}


class TWISTY_OT_scramble(bpy.types.Operator):
    """Scramble the cube with 22 random turns"""
    bl_idname = "twisty.scramble"
    bl_label = "Scramble"

    def execute(self, context):
        scramble()
        return {"FINISHED"}


class TWISTY_OT_solve(bpy.types.Operator):
    """Solve the cube by playing every turn made so far backwards"""
    bl_idname = "twisty.solve"
    bl_label = "Solve"

    def execute(self, context):
        solve()
        return {"FINISHED"}


class TWISTY_OT_undo(bpy.types.Operator):
    """Undo the last turn"""
    bl_idname = "twisty.undo"
    bl_label = "Undo Turn"

    def execute(self, context):
        undo()
        return {"FINISHED"}


class TWISTY_OT_reset(bpy.types.Operator):
    """Put the cube back to solved straight away"""
    bl_idname = "twisty.reset"
    bl_label = "Reset"

    def execute(self, context):
        if not busy():
            build_cube()
        return {"FINISHED"}


class TWISTY_OT_stop(bpy.types.Operator):
    """Stop playing"""
    bl_idname = "twisty.stop"
    bl_label = "Stop"

    def execute(self, context):
        State.playing = False
        return {"FINISHED"}


class TWISTY_PT_panel(bpy.types.Panel):
    bl_label = "Twisty Cube"
    bl_idname = "TWISTY_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Twisty Cube"

    def draw(self, context):
        layout = self.layout
        if not cubies():
            layout.operator("twisty.build", icon="MESH_CUBE")
            return

        box = layout.box()
        row = box.row()
        row.label(text=f"Moves: {State.moves}")
        row.label(text=f"Time: {fmt_time()}")
        box.label(text=State.status, icon="CHECKMARK" if State.status.startswith("Solved") else "INFO")
        if State.last:
            box.label(text=State.last)

        if State.playing:
            layout.operator("twisty.stop", text="Stop playing (Esc)", icon="PAUSE")
        else:
            layout.operator("twisty.play", text="Play (drag stickers)", icon="PLAY")

        locked = bool(State.auto)
        col = layout.column(align=True)
        col.enabled = not locked
        col.label(text="Face turns")
        for prime in (False, True):
            row = col.row(align=True)
            for letter in "UDFBLR":
                op = row.operator("twisty.turn", text=letter + ("'" if prime else ""))
                op.letter = letter
                op.prime = prime

        col = layout.column(align=True)
        row = col.row(align=True)
        row.enabled = not locked
        row.operator("twisty.scramble", icon="FILE_REFRESH")
        sub = row.row(align=True)
        sub.enabled = bool(State.history)
        sub.operator("twisty.solve", icon="CHECKMARK")
        row = col.row(align=True)
        row.enabled = not locked
        sub = row.row(align=True)
        sub.enabled = bool(State.history)
        sub.operator("twisty.undo", icon="LOOP_BACK")
        row.operator("twisty.reset", icon="X")
        layout.operator("twisty.build", text="Rebuild cube and scene", icon="MESH_CUBE")


CLASSES = (TWISTY_OT_play, TWISTY_OT_build, TWISTY_OT_turn, TWISTY_OT_scramble, TWISTY_OT_solve,
           TWISTY_OT_undo, TWISTY_OT_reset, TWISTY_OT_stop, TWISTY_PT_panel)


def register():
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:          # already registered
            pass
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, persistent=True)


def unregister():
    State.playing = False
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()
    build_cube()
    setup_scene(bpy.context)
