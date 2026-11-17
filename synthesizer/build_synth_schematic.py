"""Build the STM32F411 Black Pill synthesizer schematic (main board v3, wired).

v3 keeps the v2 circuit exactly (same reference designators, values,
footprints and nets) and only changes how it is drawn:
  * The Black Pill sits in the middle.  The VS1053, TFT, G474 link, MCP23017,
    encoder harness, NeoTrellis boards, buttons, MIDI IN/OUT and the mixer are
    wired straight to the pins they use, so every connection can be followed.
  * Supplies use power symbols.  Six signals that would have to cross the
    whole sheet use a pair of net labels instead: VS_XRST, VS_XCS, VS_XDCS,
    LCD_RST, MCP_INT and ENC5_SW.
  * Each block has a dashed frame and a title; notes are at the bottom left.

Before writing, the script checks the drawing: every wire end lands on a pin,
a wire or a label, no wire runs over a pin it does not connect to, unused
pins carry a no-connect flag, and no two named nets are shorted.

Usage:
  python build_synth_schematic.py                # writes synthesizer.kicad_sch
  python build_synth_schematic.py --stock DIR    # other KiCad symbol folder
  python build_synth_schematic.py --output FILE  # write somewhere else
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import uuid
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "synthesizer.kicad_sch"
STOCK = Path(r"C:\Program Files\KiCad\10.0\share\kicad\symbols")
SHEET_UUID = "161bcf67-49cc-4d0d-8cc2-cd831a16933a"
PROJECT = "synthesizer"


# ======================================================================
# schematic writer: symbols, wires, power symbols, labels, checks
# ======================================================================
GRID = 1.27


class Atom(str):
    pass


def parse_sexpr(source: str):
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+', source)
    pos = 0

    def node():
        nonlocal pos
        token = tokens[pos]
        pos += 1
        if token == "(":
            result = []
            while tokens[pos] != ")":
                result.append(node())
            pos += 1
            return result
        if token == ")":
            raise ValueError("Unexpected closing parenthesis")
        if token.startswith('"'):
            return json.loads(token)
        return Atom(token)

    result = node()
    if pos != len(tokens):
        raise ValueError("Trailing S-expression tokens")
    return result


def one_child(tree, name: str):
    return next((x for x in tree if isinstance(x, list) and x and x[0] == name), None)


def children(tree, name: str):
    return [x for x in tree if isinstance(x, list) and x and x[0] == name]


def q(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def n(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def r2(value: float) -> float:
    return round(value + 0.0, 2)


def pt(x: float, y: float) -> tuple[float, float]:
    return (r2(x), r2(y))


def balanced_at(data: str, start: int) -> str:
    depth = 0
    quoted = False
    escaped = False
    for i in range(start, len(data)):
        char = data[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return data[start : i + 1]
    raise ValueError("Unbalanced source symbol")


def transform(px: float, py: float, rot: int, mirror: str | None) -> tuple[float, float]:
    """Library coordinates (y up) to a sheet offset (y down).

    Matches KiCad: rotate counter-clockwise first, then apply the mirror in
    sheet space (mirror x flips the sheet y, mirror y flips the sheet x).
    """
    a = math.radians(rot)
    rx = px * math.cos(a) - py * math.sin(a)
    ry = px * math.sin(a) + py * math.cos(a)
    sx, sy = rx, -ry
    if mirror == "x":
        sy = -sy
    elif mirror == "y":
        sx = -sx
    return sx, sy


class Library:
    def __init__(self, stock: Path, local: dict[str, Path]):
        self.stock = stock
        self.local = local
        self.cache: dict[str, str] = {}
        self.pins: dict[str, dict[str, tuple[float, float, int, str, str]]] = {}
        self.props: dict[str, dict[str, str]] = {}

    def path(self, library: str, name: str) -> Path:
        if library in self.local:
            return self.local[library]
        merged = self.stock / f"{library}.kicad_sym"
        if merged.exists():
            return merged
        return self.stock / f"{library}.kicad_symdir" / f"{name}.kicad_sym"

    def load(self, lib_id: str):
        if lib_id in self.cache:
            return
        library, name = lib_id.split(":", 1)
        path = self.path(library, name)
        data = path.read_text(encoding="utf-8")
        match = re.search(r'\(symbol\s+' + re.escape(q(name)) + r'(?=\s|\))', data)
        if not match:
            raise ValueError(f"{lib_id} not found in {path}")
        source = balanced_at(data, match.start())
        tree = parse_sexpr(source)
        if one_child(tree, "extends") is not None:
            raise ValueError(f"Inherited symbol needs expansion: {lib_id}")
        self.cache[lib_id] = source.replace(f'(symbol {q(name)}', f'(symbol {q(lib_id)}', 1)
        pins = {}
        for part in children(tree, "symbol"):
            for pin in children(part, "pin"):
                at = one_child(pin, "at")
                pins[str(one_child(pin, "number")[1])] = (
                    float(at[1]), float(at[2]), int(float(at[3])),
                    str(one_child(pin, "name")[1]), str(pin[1]))
        self.pins[lib_id] = pins
        self.props[lib_id] = {str(p[1]): str(p[2]) for p in children(tree, "property")}


class Sheet:
    def __init__(self, lib: Library, *, sheet_uuid: str, project: str):
        self.lib = lib
        self.sheet_uuid = sheet_uuid
        self.project = project
        self.parts: dict[str, dict] = {}
        self.segs: list[tuple[tuple[float, float], tuple[float, float]]] = []
        self.labels: list[tuple[str, tuple[float, float], int]] = []
        self.ncs: list[tuple[float, float]] = []
        self.texts: list[tuple[str, float, float, float, bool]] = []
        self.frames: list[tuple[float, float, float, float]] = []
        self.counter = 0
        self.pwr_count = 0
        self.flag_count = 0
        self.junctions: list[tuple[float, float]] = []

    # -- identifiers -------------------------------------------------------
    def uid(self) -> str:
        self.counter += 1
        return str(uuid.uuid5(uuid.UUID(self.sheet_uuid), f"synth-v3-{self.counter}"))

    # -- symbols -----------------------------------------------------------
    def place(self, ref: str, lib_id: str, value: str, x: float, y: float, *,
              rot: int = 0, mirror: str | None = None, footprint: str | None = None,
              fields: dict | None = None, hide_value: bool = False, power: bool = False):
        """fields: name -> (x, y, display_angle, justify) in sheet coordinates."""
        if ref in self.parts:
            raise ValueError(f"Duplicate reference {ref}")
        self.lib.load(lib_id)
        self.parts[ref] = dict(lib=lib_id, value=value, x=r2(x), y=r2(y), rot=rot,
                               mirror=mirror, footprint=footprint, fields=fields or {},
                               hide_value=hide_value, power=power)
        return ref

    def P(self, ref: str, number) -> tuple[float, float]:
        part = self.parts[ref]
        px, py, _, _, _ = self.lib.pins[part["lib"]][str(number)]
        dx, dy = transform(px, py, part["rot"], part["mirror"])
        return pt(part["x"] + dx, part["y"] + dy)

    def pin_dir(self, ref: str, number) -> tuple[float, float]:
        """Unit vector pointing from the pin end away from the symbol body."""
        part = self.parts[ref]
        _, _, ang, _, _ = self.lib.pins[part["lib"]][str(number)]
        vx, vy = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        dx, dy = transform(vx, vy, part["rot"], part["mirror"])
        return (-round(dx), -round(dy))

    # -- wires -------------------------------------------------------------
    def W(self, *points):
        pts = [self._p(p) for p in points]
        for a, b in zip(pts, pts[1:]):
            if a == b:
                continue
            if a[0] != b[0] and a[1] != b[1]:
                raise ValueError(f"Diagonal wire {a} -> {b}")
            self.segs.append((a, b))
        return pts[-1]

    def _p(self, p):
        if isinstance(p, str):
            ref, number = p.split(".", 1)
            return self.P(ref, number)
        return pt(*p)

    def stub(self, ref: str, number, length: float = 2.54) -> tuple[float, float]:
        x, y = self.P(ref, number)
        dx, dy = self.pin_dir(ref, number)
        end = pt(x + dx * length, y + dy * length)
        self.W((x, y), end)
        return end

    # -- power symbols, flags, labels -------------------------------------
    POWER = {"+5V": "power:+5V", "+3V3": "power:+3V3", "GND": "power:GND",
             "+5VA": "power:+5VA", "+5V_BP": "power:+5V"}

    def pwr(self, net: str, p, direction: str | None = None, *, text: str = "auto",
            hide: bool = False, text_at=None):
        """Power symbol whose pin sits at p, pointing up/down/left/right."""
        x, y = self._p(p)
        lib_id = self.POWER[net]
        ground = net == "GND"
        if direction is None:
            direction = "down" if ground else "up"
        if ground:
            rot = {"down": 0, "right": 90, "up": 180, "left": 270}[direction]
        else:
            rot = {"up": 0, "left": 90, "down": 180, "right": 270}[direction]
        self.pwr_count += 1
        ref = f"#PWR{self.pwr_count:03d}"
        width = 0.95 * len(net)
        if direction == "up":
            vf = (x, y - 3.81, 0, None)
        elif direction == "down":
            vf = (x, y + 3.81, 0, None)
        elif direction == "left":
            vf = (x - 3.6 - width / 2, y, 0, None)
        else:
            vf = (x + 3.6 + width / 2, y, 0, None)
        if text_at is not None:
            vf = (text_at[0], text_at[1], 0, None)
        fields = {"Reference": (x, y + 5.08, 0, None, True), "Value": vf}
        self.place(ref, lib_id, net, x, y, rot=rot, fields=fields, hide_value=hide, power=True)
        return ref

    def flag(self, p, direction: str = "up"):
        """PWR_FLAG whose pin sits at p; the flag points up/down/left/right."""
        x, y = self._p(p)
        self.flag_count += 1
        ref = f"#FLG{self.flag_count:02d}"
        rot = {"up": 0, "down": 180, "left": 90, "right": 270}[direction]
        vf = {"up": (x, y - 4.45), "down": (x, y + 4.45),
              "left": (x - 8.89, y), "right": (x + 8.89, y)}[direction]
        fields = {"Reference": (x, y - 6.35, 0, None, True), "Value": (*vf, 0, None)}
        self.place(ref, "power:PWR_FLAG", "PWR_FLAG", x, y, rot=rot, fields=fields, power=True)
        return ref

    # -- moving finished blocks -------------------------------------------
    def mark(self):
        return (len(self.parts), len(self.segs), len(self.labels), len(self.ncs),
                len(self.texts), len(self.frames))

    def translate_since(self, mark, dx: float, dy: float):
        """Move everything created after `mark` by (dx, dy)."""
        np_, ns, nl, nn, nt, nf = mark
        for ref in list(self.parts)[np_:]:
            part = self.parts[ref]
            part["x"], part["y"] = r2(part["x"] + dx), r2(part["y"] + dy)
            part["fields"] = {k: (r2(v[0] + dx), r2(v[1] + dy), *v[2:])
                              for k, v in part["fields"].items()}
        self.segs[ns:] = [(pt(a[0] + dx, a[1] + dy), pt(b[0] + dx, b[1] + dy))
                          for a, b in self.segs[ns:]]
        self.labels[nl:] = [(nm, pt(p[0] + dx, p[1] + dy), ang) for nm, p, ang in self.labels[nl:]]
        self.ncs[nn:] = [pt(p[0] + dx, p[1] + dy) for p in self.ncs[nn:]]
        self.texts[nt:] = [(m, r2(x + dx), r2(y + dy), sz, b) for m, x, y, sz, b in self.texts[nt:]]
        self.frames[nf:] = [(r2(a + dx), r2(b + dy), r2(c + dx), r2(d + dy), t)
                            for a, b, c, d, t in self.frames[nf:]]

    def label(self, name: str, p, side: str = "right"):
        """Net label anchored at p; side = where the text goes (right/left/up/down)."""
        angle = {"right": 0, "left": 180, "up": 90, "down": 270}[side]
        self.labels.append((name, self._p(p), angle))

    def nc(self, ref: str, number):
        self.ncs.append(self.P(ref, number))

    def text(self, message: str, x: float, y: float, size: float = 1.27, bold: bool = False):
        self.texts.append((message, r2(x), r2(y), size, bold))

    def frame(self, x1: float, y1: float, x2: float, y2: float, title: str | None = None):
        self.frames.append((r2(x1), r2(y1), r2(x2), r2(y2), title))

    # -- block frames --------------------------------------------------------
    def _body_box(self, lib_id):
        """Graphic + pin extents of a library symbol, library coordinates."""
        if not hasattr(self, "_boxes"):
            self._boxes = {}
        if lib_id not in self._boxes:
            tree = parse_sexpr(self.lib.cache[lib_id])
            xs, ys = [], []
            for unit in children(tree, "symbol"):
                for g in unit:
                    if not isinstance(g, list) or not g:
                        continue
                    if g[0] == "rectangle":
                        a, b = one_child(g, "start"), one_child(g, "end")
                        xs += [float(a[1]), float(b[1])]
                        ys += [float(a[2]), float(b[2])]
                    elif g[0] in ("polyline", "bezier"):
                        for xy in children(one_child(g, "pts"), "xy"):
                            xs.append(float(xy[1]))
                            ys.append(float(xy[2]))
                    elif g[0] == "circle":
                        c = one_child(g, "center")
                        r = float(one_child(g, "radius")[1])
                        xs += [float(c[1]) - r, float(c[1]) + r]
                        ys += [float(c[2]) - r, float(c[2]) + r]
                    elif g[0] == "arc":
                        for k in ("start", "mid", "end"):
                            q_ = one_child(g, k)
                            xs.append(float(q_[1]))
                            ys.append(float(q_[2]))
                    elif g[0] == "pin" and one_child(g, "hide") is None:
                        at = one_child(g, "at")
                        ln = float(one_child(g, "length")[1])
                        a = math.radians(float(at[3]))
                        xs += [float(at[1]), float(at[1]) + ln * math.cos(a)]
                        ys += [float(at[2]), float(at[2]) + ln * math.sin(a)]
            self._boxes[lib_id] = (min(xs), max(xs), min(ys), max(ys)) if xs else (0, 0, 0, 0)
        return self._boxes[lib_id]

    @staticmethod
    def _text_box(msg, x, y, size=1.27, justify=None, angle=0):
        w = 1.05 * len(msg) * size / 1.27
        h = size
        if angle in (90, 270):
            return (x - h / 2, y - w / 2, x + h / 2, y + w / 2)
        if justify == "left":
            return (x, y - h / 2, x + w, y + h / 2)
        if justify == "right":
            return (x - w, y - h / 2, x, y + h / 2)
        return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)

    def extents_since(self, mark, skip=()):
        np_, ns, nl, nn, nt, nf = mark[:6]
        boxes = []
        for ref in list(self.parts)[np_:]:
            if ref in skip:
                continue
            part = self.parts[ref]
            x0, x1, y0, y1 = self._body_box(part["lib"])
            pts = [transform(px, py, part["rot"], part["mirror"]) for px in (x0, x1) for py in (y0, y1)]
            xs = [part["x"] + p[0] for p in pts]
            ys = [part["y"] + p[1] for p in pts]
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
            for name, value in (("Reference", ref), ("Value", part["value"])):
                f = part["fields"].get(name)
                if f is None:
                    continue
                hidden = (len(f) > 4 and f[4]) or (name == "Value" and part["hide_value"])
                if hidden:
                    continue
                boxes.append(self._text_box(value, f[0], f[1], 1.27, f[3], f[2]))
        for a, b in self.segs[ns:]:
            boxes.append((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])))
        for name, p, ang in self.labels[nl:]:
            w = 1.05 * len(name) + 1.0
            if ang == 0:
                boxes.append((p[0], p[1] - 1.5, p[0] + w, p[1]))
            elif ang == 180:
                boxes.append((p[0] - w, p[1] - 1.5, p[0], p[1]))
            elif ang == 90:
                boxes.append((p[0] - 1.5, p[1] - w, p[0], p[1]))
            else:
                boxes.append((p[0] - 1.5, p[1], p[0], p[1] + w))
        for msg, x, y, size, bold in self.texts[nt:]:
            boxes.append(self._text_box(msg, x, y, size, "left"))
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def auto_frame(self, mark, title, *, pad=2.54, pad_top=6.35, skip=(), grow=(0, 0, 0, 0)):
        """Dashed frame around everything created since `mark`, title at the top left."""
        x1, y1, x2, y2 = self.extents_since(mark, skip)
        g = GRID
        x1 = math.floor((x1 - pad - grow[0]) / g) * g
        y1 = math.floor((y1 - pad_top - grow[1]) / g) * g
        x2 = math.ceil((x2 + pad + grow[2]) / g) * g
        y2 = math.ceil((y2 + pad + grow[3]) / g) * g
        self.frame(x1, y1, x2, y2, title)
        return (x1, y1, x2, y2)

    # -- geometry ----------------------------------------------------------
    def pin_points(self):
        """(point, ref, number) for every symbol pin."""
        out = []
        for ref, part in self.parts.items():
            for number in self.lib.pins[part["lib"]]:
                out.append((self.P(ref, number), ref, number))
        return out

    def finalize(self):
        """Split wires at T joints and compute junction dots."""
        # merge collinear overlaps are not allowed: detect them
        segs = [(min(a, b), max(a, b)) for a, b in self.segs]
        segs = sorted(set(segs))
        ends = defaultdict(int)
        for a, b in segs:
            ends[a] += 1
            ends[b] += 1
        power_pts = set()
        for p, ref, number in self.pin_points():
            if self.parts[ref]["power"]:
                power_pts.add(p)
        # split a segment where another wire end or a power pin lands inside it
        split_pts = set(ends) | power_pts
        changed = True
        while changed:
            changed = False
            new = []
            for a, b in segs:
                inner = [p for p in split_pts if _inside(p, a, b)]
                if inner:
                    pts = sorted(set([a, b] + inner))
                    new.extend(zip(pts, pts[1:]))
                    changed = True
                else:
                    new.append((a, b))
            segs = sorted(set(new))
        self.segs = segs
        # junction dots: 3+ connection items at one point
        count = defaultdict(int)
        for a, b in segs:
            count[a] += 1
            count[b] += 1
        for p, ref, number in self.pin_points():
            if p in count:
                count[p] += 1
        self.junctions = sorted(p for p, c in count.items() if c >= 3)
        self._place_frame_titles()

    def obstacles(self):
        """Boxes of everything drawn: symbol bodies, visible fields, wires, labels, texts."""
        obs = []
        for ref, part in self.parts.items():
            x0, x1, y0, y1 = self._body_box(part["lib"])
            pts = [transform(px, py, part["rot"], part["mirror"]) for px in (x0, x1) for py in (y0, y1)]
            xs = [part["x"] + p[0] for p in pts]
            ys = [part["y"] + p[1] for p in pts]
            obs.append((min(xs), min(ys), max(xs), max(ys)))
            for name, value in (("Reference", ref), ("Value", part["value"])):
                f = part["fields"].get(name)
                if f is None:
                    continue
                if (len(f) > 4 and f[4]) or (name == "Value" and part["hide_value"]):
                    continue
                obs.append(self._text_box(value, f[0], f[1], 1.27, f[3], f[2]))
        for a, b in self.segs:
            obs.append((min(a[0], b[0]) - 0.3, min(a[1], b[1]) - 0.3,
                        max(a[0], b[0]) + 0.3, max(a[1], b[1]) + 0.3))
        for name, p, ang in self.labels:
            w = 1.05 * len(name) + 1.0
            obs.append({0: (p[0], p[1] - 1.5, p[0] + w, p[1]),
                        180: (p[0] - w, p[1] - 1.5, p[0], p[1]),
                        90: (p[0] - 1.5, p[1] - w, p[0], p[1]),
                        270: (p[0] - 1.5, p[1], p[0], p[1] + w)}[ang])
        for msg, x, y, size, bold in self.texts:
            obs.append(self._text_box(msg, x, y, size, "left"))
        for p in self.ncs:
            obs.append((p[0] - 1.0, p[1] - 1.0, p[0] + 1.0, p[1] + 1.0))
        return obs

    def _place_frame_titles(self, size: float = 1.6):
        obs = self.obstacles()
        self.frame_texts = []

        def hits(box):
            return any(box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3] for o in obs)

        for i, (x1, y1, x2, y2, title) in enumerate(self.frames):
            if not title:
                continue
            w = 1.12 * len(title) * size / 1.27
            cands = [(x1 + 1.91, y1 + 2.54), (x2 - 1.91 - w, y1 + 2.54),
                     (x1 + 1.91, y2 - 2.54), (x2 - 1.91 - w, y2 - 2.54)]
            pick = None
            for cx, cy in cands:
                box = (cx - 0.5, cy - size / 2 - 0.5, cx + w + 0.5, cy + size / 2 + 0.5)
                if not hits(box):
                    pick = (cx, cy)
                    break
            if pick is None:
                # make room: grow the frame downwards and use the new bottom strip
                y2 = r2(y2 + 3.81)
                self.frames[i] = (x1, y1, x2, y2, title)
                pick = (x1 + 1.91, y2 - 2.54)
                box = (pick[0] - 0.5, pick[1] - size / 2 - 0.5, pick[0] + w + 0.5, pick[1] + size / 2 + 0.5)
                if hits(box):
                    print("WARNING: frame title overlaps something:", title)
            self.frame_texts.append((title, r2(pick[0]), r2(pick[1]), size, True))
            obs.append((pick[0], pick[1] - size / 2, pick[0] + w, pick[1] + size / 2))

    def check(self) -> dict:
        """Return connectivity and raise on drawing errors."""
        errors = []
        segs = self.segs
        # collinear overlaps
        for i in range(len(segs)):
            a1, b1 = segs[i]
            for j in range(i + 1, len(segs)):
                a2, b2 = segs[j]
                if _overlap(a1, b1, a2, b2):
                    errors.append(f"overlapping wires {segs[i]} {segs[j]}")
        pins = self.pin_points()
        pin_at = defaultdict(list)
        for p, ref, number in pins:
            pin_at[p].append((ref, number))
        # wire passing over a pin end, label or NC
        special = set(pin_at) | {p for _, p, _ in self.labels} | set(self.ncs)
        for a, b in segs:
            for p in special:
                if _inside(p, a, b):
                    errors.append(f"wire {a}-{b} runs over {p} {pin_at.get(p)}")
        # dangling wire ends
        count = defaultdict(int)
        for a, b in segs:
            count[a] += 1
            count[b] += 1
        label_pts = {p for _, p, _ in self.labels}
        for p, c in count.items():
            if c == 1 and p not in pin_at and p not in label_pts:
                errors.append(f"dangling wire end at {p}")
        for _, p, _ in self.labels:
            if count.get(p, 0) == 0 and p not in pin_at:
                errors.append(f"label not on a wire at {p}")
        for p in self.ncs:
            if count.get(p, 0):
                errors.append(f"no-connect on a wire at {p}")
        # union-find
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for a, b in segs:
            union(("pt", a), ("pt", b))
        for p, ref, number in pins:
            union(("pin", ref, number), ("pt", p))
        names = defaultdict(set)
        for ref, part in self.parts.items():
            if part["power"] and part["lib"] != "power:PWR_FLAG":
                names[find(("pin", ref, "1"))].add(part["value"])
        for name, p, _ in self.labels:
            names[find(("pt", p))].add(name)
        # merge same-named groups (power symbols and labels are global on one sheet)
        by_name = defaultdict(list)
        for root, ns in names.items():
            for nm in ns:
                by_name[nm].append(root)
        for nm, roots in by_name.items():
            for r in roots[1:]:
                union(roots[0], r)
        groups = defaultdict(set)
        gnames = defaultdict(set)
        for p, ref, number in pins:
            groups[find(("pin", ref, number))].add((ref, number))
        for root, ns in names.items():
            gnames[find(root)] |= ns
        nets = []
        for root, members in groups.items():
            nm = gnames.get(root, set())
            if len(nm) > 1:
                errors.append(f"short between named nets {sorted(nm)}")
            nets.append((sorted(nm), sorted(members)))
        # unconnected, non-NC pins
        nc_set = set(self.ncs)
        for root, members in groups.items():
            real = [m for m in members if not self.parts[m[0]]["power"]]
            if len(members) == 1 and real:
                ref, number = real[0]
                etype = self.lib.pins[self.parts[ref]["lib"]][number][4]
                if self.P(ref, number) not in nc_set and etype != "no_connect":
                    errors.append(f"unconnected pin {ref}.{number}")
        return {"errors": errors, "nets": nets}

    # -- output ------------------------------------------------------------
    def render(self, *, paper: str, title_block: str) -> str:
        items = ["(kicad_sch", "(version 20250114)", '(generator "eeschema")',
                 '(generator_version "9.0")', f'(uuid {q(self.sheet_uuid)})',
                 f'(paper {q(paper)})', title_block]
        items.append("(lib_symbols\n" + "\n".join(self.lib.cache[k] for k in sorted(
            {p["lib"] for p in self.parts.values()})) + "\n)")
        for x1, y1, x2, y2, _ in self.frames:
            items.append(f'(rectangle (start {n(x1)} {n(y1)}) (end {n(x2)} {n(y2)}) '
                         f'(stroke (width 0.254) (type dash)) (fill (type none)) '
                         f'(uuid {q(self.uid())}))')
        for p in self.junctions:
            items.append(f'(junction (at {n(p[0])} {n(p[1])}) (diameter 0) (color 0 0 0 0) '
                         f'(uuid {q(self.uid())}))')
        for p in self.ncs:
            items.append(f'(no_connect (at {n(p[0])} {n(p[1])}) (uuid {q(self.uid())}))')
        for a, b in self.segs:
            items.append(f'(wire (pts (xy {n(a[0])} {n(a[1])}) (xy {n(b[0])} {n(b[1])})) '
                         f'(stroke (width 0) (type default)) (uuid {q(self.uid())}))')
        for name, p, ang in self.labels:
            just = "left bottom" if ang in (0, 90) else "right bottom"
            items.append(f'(label {q(name)} (at {n(p[0])} {n(p[1])} {ang}) '
                         f'(effects (font (size 1.27 1.27)) (justify {just})) '
                         f'(uuid {q(self.uid())}))')
        for message, x, y, size, bold in self.texts + getattr(self, "frame_texts", []):
            b = " (bold yes)" if bold else ""
            items.append(f'(text {q(message)} (exclude_from_sim no) (at {n(x)} {n(y)} 0) '
                         f'(effects (font (size {n(size)} {n(size)}){b}) (justify left)) '
                         f'(uuid {q(self.uid())}))')
        for ref, part in self.parts.items():
            items.append(self._symbol(ref, part))
        items.extend(['(sheet_instances (path "/" (page "1")))', '(embedded_fonts no)', ')'])
        return "\n\t".join(items) + "\n"

    def _field(self, name, value, x, y, angle, justify, hidden):
        just = f" (justify {justify})" if justify else ""
        hide = " (hide yes)" if hidden else ""
        return (f'(property {q(name)} {q(value)} (at {n(x)} {n(y)} {angle}) '
                f'(effects (font (size 1.27 1.27)){just}{hide}))')

    def _symbol(self, ref, part):
        lib_id = part["lib"]
        rot = part["rot"]
        x, y = part["x"], part["y"]
        toggled = rot in (90, 270)

        def stored(display):
            return (90 if display == 0 else 0) if toggled else display

        fields = part["fields"]
        out = ["(symbol", f"(lib_id {q(lib_id)})", f"(at {n(x)} {n(y)} {rot})"]
        if part["mirror"]:
            out.append(f"(mirror {part['mirror']})")
        out += ["(unit 1)", "(exclude_from_sim no)", "(in_bom yes)", "(on_board yes)",
                "(dnp no)", f"(uuid {q(self.uid())})"]
        for name, value in (("Reference", ref), ("Value", part["value"])):
            f = fields.get(name)
            if f is None:
                f = (x, y, 0, None, True)
            fx, fy, disp, just = f[:4]
            hidden = (len(f) > 4 and f[4]) or (name == "Value" and part["hide_value"])
            out.append(self._field(name, value, fx, fy, stored(disp), just, hidden))
        fp = part["footprint"] if part["footprint"] is not None else \
            self.lib.props[lib_id].get("Footprint", "")
        out.append(self._field("Footprint", fp, x, y, 0, None, True))
        out.append(self._field("Datasheet", self.lib.props[lib_id].get("Datasheet", ""),
                               x, y, 0, None, True))
        for number in self.lib.pins[lib_id]:
            out.append(f'(pin {q(number)} (uuid {q(self.uid())}))')
        out.append(f'(instances (project {q(self.project)} (path "/{self.sheet_uuid}" '
                   f'(reference {q(ref)}) (unit 1))))')
        out.append(")")
        return "\n".join(out)


def _inside(p, a, b) -> bool:
    """p strictly inside segment a-b (axis aligned)."""
    if a[0] == b[0] == p[0]:
        return min(a[1], b[1]) < p[1] < max(a[1], b[1])
    if a[1] == b[1] == p[1]:
        return min(a[0], b[0]) < p[0] < max(a[0], b[0])
    return False


def _overlap(a1, b1, a2, b2) -> bool:
    if a1[0] == b1[0] == a2[0] == b2[0]:
        lo = max(min(a1[1], b1[1]), min(a2[1], b2[1]))
        hi = min(max(a1[1], b1[1]), max(a2[1], b2[1]))
        return hi - lo > 1e-6
    if a1[1] == b1[1] == a2[1] == b2[1]:
        lo = max(min(a1[0], b1[0]), min(a2[0], b2[0]))
        hi = min(max(a1[0], b1[0]), max(a2[0], b2[0]))
        return hi - lo > 1e-6
    return False


def crossings(segs) -> list[tuple[float, float]]:
    out = []
    hs = [s for s in segs if s[0][1] == s[1][1]]
    vs = [s for s in segs if s[0][0] == s[1][0]]
    for h in hs:
        y = h[0][1]
        x1, x2 = sorted((h[0][0], h[1][0]))
        for v in vs:
            x = v[0][0]
            y1, y2 = sorted((v[0][1], v[1][1]))
            if x1 < x < x2 and y1 < y < y2:
                out.append((x, y))
    return out


# ----------------------------------------------------------------------
# small placement helpers
# ----------------------------------------------------------------------
def r_v(s, ref, value, x, top, *, lib="Device:R", side="right", fp=None):
    """Vertical two-pin part: pin 1 at (x, top), pin 2 at (x, top + 7.62)."""
    y = top + 3.81
    if side == "right":
        f = {"Reference": (x + 2.54, y - 1.27, 0, "left"), "Value": (x + 2.54, y + 1.27, 0, "left")}
    else:
        f = {"Reference": (x - 2.54, y - 1.27, 0, "right"), "Value": (x - 2.54, y + 1.27, 0, "right")}
    s.place(ref, lib, value, x, y, footprint=fp, fields=f)
    return (x, top), (x, top + 7.62)


def c_v(s, ref, value, x, top, *, polarized=False, side="right"):
    return r_v(s, ref, value, x, top, lib="Device:C_Polarized" if polarized else "Device:C", side=side)


def r_h(s, ref, value, left, y, *, pin1="left", lib="Device:R", text="split", fp=None):
    """Horizontal two-pin part between (left, y) and (left + 7.62, y)."""
    x = left + 3.81
    rot = 90 if pin1 == "left" else 270
    if text == "split":
        f = {"Reference": (x, y - 2.54, 0, None), "Value": (x, y + 2.54, 0, None)}
    elif text == "above":
        f = {"Reference": (x - 2.54, y - 2.54, 0, None), "Value": (x + 2.54, y - 2.54, 0, None)}
    else:
        f = {"Reference": (x - 2.54, y + 2.54, 0, None), "Value": (x + 2.54, y + 2.54, 0, None)}
    s.place(ref, lib, value, x, y, rot=rot, footprint=fp, fields=f)
    a, b = (left, y), (left + 7.62, y)
    return (a, b) if pin1 == "left" else (b, a)


def button(s, net, entry, *, go, r_ref, c_ref, sw_ref=None, label=None, title=None):
    """Debounced input: pull-up R to +3V3, C to GND, push switch to GND.

    The signal arrives at `entry` and the row continues towards `go`."""
    x0, y = entry
    d = 1 if go == "right" else -1
    xr, xc = x0 + d * 5.08, x0 + d * 12.7
    r_v(s, r_ref, "10k", xr, y - 7.62, side="left" if d > 0 else "right")
    s.pwr("+3V3", (xr, y - 7.62), "up")
    c_v(s, c_ref, "100n", xc, y, side="right" if d > 0 else "left")
    s.pwr("GND", (xc, y + 7.62), "down")
    end = (xc, y)
    if sw_ref:
        xs = x0 + d * 22.86
        s.place(sw_ref, "Switch:SW_Push", "SW_Push", xs, y, rot=0 if d > 0 else 180,
                fields={"Reference": (xs, y - 5.08, 0, None), "Value": (xs, y + 2.54, 0, None)},
                hide_value=True)
        if title:
            s.text(title, xs - 0.6 * len(title), y - 7.62, size=1.27, bold=True)
        end = s.P(sw_ref, 1)
        s.pwr("GND", s.P(sw_ref, 2), "down")
    s.W(entry, (xr, y), (xc, y), end)
    if label:
        s.label(label, end if not sw_ref else entry, "right" if d > 0 else "left")


def conn(s, ref, n, title, x, y, *, rot=0, mirror=None, ref_at=None, val_at=None):
    lib = f"Connector:Conn_01x{n:02d}_Pin"
    f = {}
    for name, at in (("Reference", ref_at), ("Value", val_at)):
        if at:
            f[name] = (at[0], at[1], 0, at[2] if len(at) > 2 else None)
    s.place(ref, lib, title, x, y, rot=rot, mirror=mirror, fields=f)



# ----------------------------------------------------------------------
# layout knobs (mm).  Blocks are drawn in "core" coordinates around the
# Black Pill, then moved as groups so the sheet is filled evenly.
# ----------------------------------------------------------------------
CORE_TOP = 101.6                 # the wired core is centred horizontally, top at this y
LEFT_G474 = -33.02               # G474 link + PCM5102A header group
LEFT_INST = -38.1                # INST button group
LEFT_MCP = -33.02                # MCP23017 + encoder harness group
DOWN_MCP = 10.16
RIGHT_VS = 38.1                  # VS1053 + audio mixer group
RIGHT_MIDI = 53.34               # MIDI IN / MIDI OUT groups
DOWN_MIDI_OUT = 15.24


# ----------------------------------------------------------------------
# the circuit
# ----------------------------------------------------------------------
def build(stock: Path) -> Sheet:
    lib = Library(stock, {"Synthesizer_Modules": HERE / "Synthesizer_Modules.kicad_sym"})
    s = Sheet(lib, sheet_uuid=SHEET_UUID, project=PROJECT)
    W, P, pwr = s.W, s.P, s.pwr
    start = s.mark()

    # ================= Black Pill (U1) ====================================
    m = s.mark()
    s.place("U1", "Synthesizer_Modules:BlackPill_F411CEU6", "WeAct Black Pill F411CEU6",
            254.0, 203.2, fields={"Reference": (254.0, 173.99, 0, None),
                                  "Value": (254.0, 232.41, 0, None)})
    for pin, net in (("18", "+5V_BP"), ("19", "GND"), ("20", "+3V3")):
        pwr(net, s.stub("U1", pin, 2.54), "left")
    for pin, net in (("40", "+5V_BP"), ("39", "GND"), ("38", "+3V3")):
        pwr(net, s.stub("U1", pin, 2.54), "right")
    for pin in ("8", "9", "36", "21", "22", "23", "24"):
        s.nc("U1", pin)
    # label pairs: signals that would otherwise cross the whole sheet
    for pin, net in (("3", "MCP_INT"), ("7", "ENC5_SW"), ("12", "VS_XRST"),
                     ("14", "VS_XCS"), ("15", "VS_XDCS")):
        s.label(net, s.stub("U1", pin, 2.54), "left")
    s.label("LCD_RST", s.stub("U1", "25", 2.54), "right")

    # ================= VS1053 + passive mixer (moved right as a group) ====
    m = s.mark()
    s.place("U2", "Synthesizer_Modules:Adafruit_VS1053_Breakout_V2", "Adafruit VS1053 breakout v2",
            386.08, 195.58, fields={"Reference": (386.08, 170.18, 0, None),
                                    "Value": (386.08, 220.98, 0, None)})
    W("U2.3", (347.98, 181.61))
    pwr("GND", (347.98, 181.61), "left")
    W("U2.4", (350.52, 184.15))
    W("U2.5", (350.52, 186.69))
    W((350.52, 181.61), (350.52, 186.69))
    pwr("+5VA", s.stub("U2", "7", 2.54), "left")
    pwr("GND", s.stub("U2", "9", 2.54), "left")
    for pin, net in (("13", "VS_XRST"), ("14", "VS_XCS"), ("16", "VS_XDCS")):
        s.label(net, s.stub("U2", pin, 2.54), "left")
    pwr("GND", s.stub("U2", "20", 2.54), "right")
    for pin in ["8"] + [str(p) for p in range(17, 33) if p != 20]:
        s.nc("U2", pin)
    fv = s.auto_frame(m, "GM BACKING BAND  VS1053", pad=3.81, grow=(10.16, 0, 0, 0))
    # passive mixer v0: VS1053 + PCM5102A -> line out
    mm = s.mark()
    conn(s, "J8", 3, "PCM5102A AUDIO OUT", 325.12, 132.08,
         ref_at=(326.39, 124.46), val_at=(326.39, 121.92))
    l13 = r_h(s, "R13", "10k", 345.44, 129.54, pin1="left", text="above")
    l12 = r_h(s, "R12", "10k", 345.44, 134.62, pin1="left", text="below")
    l15 = r_h(s, "R15", "10k", 350.52, 144.78, pin1="left", text="above")
    l14 = r_h(s, "R14", "10k", 350.52, 149.86, pin1="left", text="below")
    W("J8.1", l13[0])
    pwr("GND", s.stub("J8", 2, 2.54), "right")
    W("J8.3", (340.36, 134.62), (340.36, 144.78), l15[0])
    conn(s, "J5", 3, "STEREO LINE OUT", 378.46, 132.08, mirror="y",
         ref_at=(377.19, 124.46), val_at=(377.19, 121.92))
    W(l13[1], l12[1])
    W(l13[1], "J5.1")
    W(l15[1], l14[1])
    W(l15[1], (365.76, 144.78), (365.76, 132.08), "J5.2")
    pwr("GND", "J5.3", "down")
    s.auto_frame(mm, "AUDIO MIX v0 (passive)  /  LINE OUT", pad=3.81)
    W("U2.2", (345.44, 179.07), l12[0])
    W("U2.1", (347.98, 176.53), (347.98, 149.86), l14[0])
    s.translate_since(m, RIGHT_VS, 0)

    # U1 <-> VS1053 SPI and the signals that leave through the gap
    W("U1.32", "U2.10")                                        # MISO straight
    W("U1.27", (322.58, 212.09), (322.58, 189.23), "U2.6")     # DREQ
    W("U1.31", (327.66, 201.93), (327.66, 204.47), "U2.12")    # SCK
    W("U1.35", (330.2, 191.77), (330.2, 212.09), "U2.15")      # SDCS
    W("U1.33", (332.74, 196.85), (332.74, 201.93), "U2.11")    # MOSI

    # ================= TFT (top, wired straight up) ======================
    m = s.mark()
    J6x, J6y = 200.66, 154.94
    conn(s, "J6", 8, "TFT ILI9488 SPI", J6x, J6y, rot=270,
         ref_at=(J6x - 6.35, J6y - 5.08), val_at=(J6x + 5.08, J6y - 5.08))
    s.label("LCD_RST", P("J6", 7), "down")
    pwr("GND", "J6.2", "down", text_at=(209.55, 162.56))
    W("J6.1", (210.82, 160.02))
    pwr("+5V", (210.82, 160.02), "up")
    W("U1.1", (198.12, 179.07), "J6.5")                       # LCD_CS
    W("U1.2", (203.2, 181.61), "J6.3")                        # LCD_SCK
    W("U1.4", (200.66, 186.69), "J6.4")                       # LCD_MOSI
    W("U1.5", (190.5, 189.23), "J6.8")                        # LCD_LED
    W("U1.34", (312.42, 194.31), (312.42, 168.91), (195.58, 168.91), "J6.6")  # LCD_DC

    # ================= STOP button (above the gap) =======================
    W("U1.37", (309.88, 186.69), (309.88, 160.02))
    button(s, "BTN_STOP", (309.88, 160.02), go="right", r_ref="R28", c_ref="C12", sw_ref="SW2",
           title="STOP")

    # ================= G474 link + PCM5102A I2S input (moved left) =======
    m = s.mark()
    J4x, J4y = 142.24, 207.01
    jx = J4x + 5.08                                              # J4 / J7 pin column
    conn(s, "J4", 10, "G474 SYNTH LINK", J4x, J4y, ref_at=(J4x - 1.27, J4y - 1.27, "right"),
         val_at=(J4x - 1.27, J4y + 1.27, "right"))
    s.place("JP1", "Jumper:SolderJumper_2_Open", "OPEN (dev)", jx + 12.7, 196.85, rot=180,
            fields={"Reference": (jx + 12.7, 199.39, 0, None),
                    "Value": (jx + 12.7, 199.39, 0, None, True)})
    W("J4.1", P("JP1", 2))
    pwr("+5V", P("JP1", 1), "right")
    for pin in ("2", "5", "7"):
        pwr("GND", s.stub("J4", pin, 2.54), "right", hide=True)
    W("J4.10", (jx + 2.54, 219.71))
    pwr("GND", (jx + 2.54, 219.71), "down")
    # UART to the G474 through 100R
    r7 = r_h(s, "R7", "100R", jx + 17.78, 201.93, pin1="right", text="above")
    W("J4.3", r7[1])
    r8 = r_h(s, "R8", "100R", jx + 30.48, 204.47, pin1="right", text="below")
    W("J4.4", r8[1])
    # I2S from the G474 through 33R, then down to the PCM5102A header (J7)
    bck = r_h(s, "R9", "33R", jx + 7.62, 209.55, pin1="right", text="above")
    lrck = r_h(s, "R10", "33R", jx + 7.62, 214.63, pin1="right", text="above")
    din = r_h(s, "R11", "33R", jx + 17.78, 217.17, pin1="right", text="below")
    W("J4.6", bck[1]); W("J4.8", lrck[1]); W("J4.9", din[1])
    J7y = 236.22
    conn(s, "J7", 6, "PCM5102A I2S", J4x, J7y, mirror="x",
         ref_at=(J4x - 1.27, J7y - 1.27, "right"), val_at=(J4x - 1.27, J7y + 1.27, "right"))
    W(bck[0], (jx + 29.21, 209.55), (jx + 29.21, J7y), "J7.3")
    W(lrck[0], (jx + 15.24, 214.63), (jx + 15.24, J7y - 5.08), "J7.5")
    W(din[0], (jx + 26.67, 217.17), (jx + 26.67, J7y - 2.54), "J7.4")
    for pin in ("6", "2"):
        pwr("GND", s.stub("J7", pin, 2.54), "right", hide=True)
    pwr("+5VA", s.stub("J7", "1", 2.54), "right")
    s.auto_frame(m, "G474 SYNTH LINK  +  PCM5102A DAC", pad=3.81, pad_top=1.27, grow=(0, 0, 3.81, 0))
    s.translate_since(m, LEFT_G474, 0)
    W(P("R7", 1), "U1.10")
    W(P("R8", 1), "U1.11")

    # ================= INST button (moved left) ==========================
    m = s.mark()
    button(s, "BTN_INST", (120.65, 191.77), go="left", r_ref="R29", c_ref="C13", sw_ref="SW3",
           title="INST")
    s.translate_since(m, LEFT_INST, 0)
    W("U1.6", (120.65 + LEFT_INST, 191.77))

    # ================= 5 V sense divider ==================================
    W("U1.13", (196.85, 209.55), (196.85, 226.06), (190.5, 226.06))
    r_v(s, "R5", "10k", 190.5, 218.44, side="left")
    pwr("+5V", (190.5, 218.44), "up")
    r_v(s, "R6", "15k", 190.5, 226.06, side="left")
    pwr("GND", (190.5, 233.68), "down")

    # ================= I2C bus (U1 PB8/PB9) ===============================
    SCLX, SDAX = 199.39, 201.93
    W("U1.16", (SCLX, 217.17), (SCLX, 259.08), (SCLX, 254.0 + DOWN_MCP))
    W("U1.17", (SDAX, 219.71), (SDAX, 256.54), (SDAX, 256.54 + DOWN_MCP))
    r23 = r_v(s, "R23", "4k7", 185.42, 246.38 + DOWN_MCP, side="right")   # SCL pull-up
    pwr("+3V3", r23[0], "up")

    # ================= MCP23017 + encoder harness (moved left) ===========
    m = s.mark()
    MX, MY = 134.62, 274.32
    s.place("U4", "Interface_Expansion:MCP23017x-x-SO", "MCP23017", MX, MY, mirror="y",
            fields={"Reference": (MX + 5.08, 248.92, 0, "left"),
                    "Value": (MX - 5.08, 300.99, 0, "right")})
    pwr("+3V3", "U4.9", "up")
    pwr("GND", "U4.10", "down")
    W("U4.20", (152.4, 269.24))
    s.label("MCP_INT", (152.4, 269.24), "up")
    p1, p2 = r_h(s, "R26", "10k", 157.48, 269.24, pin1="right", text="above")
    W((152.4, 269.24), p2)
    pwr("+3V3", p1, "right")
    p1, p2 = r_h(s, "R25", "10k", 152.4, 276.86, pin1="right", text="below")
    W("U4.18", p2)
    pwr("+3V3", p1, "right")
    for pin in ("15", "16", "17"):
        W(f"U4.{pin}", (149.86, P("U4", pin)[1]))
    W((149.86, 289.56), (149.86, 292.1), (149.86, 294.64), (149.86, 297.18))
    pwr("GND", (149.86, 297.18), "down")
    for pin in ("19", "28", "8"):
        s.nc("U4", pin)
    c_v(s, "C10", "100n", 172.72, 281.94)
    pwr("+3V3", (172.72, 281.94), "up")
    pwr("GND", (172.72, 289.56), "down")
    # harness J11: pin order is the panel order (ENC1 A/B/SW ... ENC5 A/B/SW, GND)
    J11x, J11y = 83.82, 269.24
    conn(s, "J11", 16, "ENCODERS / panel harness", J11x, J11y,
         ref_at=(J11x + 2.54, 246.38), val_at=(J11x + 2.54, 248.92))
    jorder = ["ENC1_A", "ENC1_B", "ENC1_SW", "ENC2_A", "ENC2_B", "ENC2_SW", "ENC3_A", "ENC3_B",
              "ENC3_SW", "ENC4_A", "ENC4_B", "ENC4_SW", "ENC5_A", "ENC5_B", "ENC5_SW", "GND"]
    mcp_pin = {"ENC1_A": 21, "ENC1_B": 22, "ENC2_A": 23, "ENC2_B": 24, "ENC3_A": 25,
               "ENC3_B": 26, "ENC1_SW": 27, "ENC4_A": 1, "ENC4_B": 2, "ENC5_A": 3,
               "ENC5_B": 4, "ENC2_SW": 5, "ENC3_SW": 6, "ENC4_SW": 7}
    cols = {"ENC4_SW": 96.52, "ENC3_SW": 99.06, "ENC2_SW": 101.6, "ENC3_A": 104.14,
            "ENC3_B": 106.68, "ENC1_SW": 109.22, "ENC1_B": 111.76, "ENC1_A": 114.3,
            "ENC4_B": 116.84, "ENC4_A": 119.38}
    for nm, mp in mcp_pin.items():
        a = P("U4", mp)
        b = P("J11", jorder.index(nm) + 1)
        if a[1] == b[1]:
            W(a, b)
        else:
            W(a, (cols[nm], a[1]), (cols[nm], b[1]), b)
    W("J11.16", (88.9, 292.1))
    pwr("GND", (88.9, 292.1), "down")
    W("J11.15", (93.98, 287.02), (93.98, 309.88))
    button(s, "ENC5_SW", (93.98, 309.88), go="right", r_ref="R30", c_ref="C14", label="ENC5_SW")
    s.auto_frame(m, "CONTROLS  MCP23017 (I2C 0x20)  +  5 ENCODERS", pad=3.81)
    s.translate_since(m, LEFT_MCP, DOWN_MCP)
    W((SCLX, 254.0 + DOWN_MCP), r23[1], P("U4", 12))
    W((SDAX, 256.54 + DOWN_MCP), P("U4", 13))

    # ================= NeoTrellis x2 (under U1) ===========================
    m = s.mark()
    J9x, J10x, JNy = 214.63, 240.03, 271.78
    for ref, jx_, title in (("J9", J9x, "NeoTrellis 1"), ("J10", J10x, "NeoTrellis 2")):
        conn(s, ref, 5, title, jx_, JNy, ref_at=(jx_ + 1.27, JNy - 10.16),
             val_at=(jx_ + 1.27, JNy + 10.16))
        pwr("+5V", s.stub(ref, 1, 2.54), "up")
        pwr("GND", s.stub(ref, 2, 2.54), "right", hide=True)
        for pin in (3, 4, 5):
            W(f"{ref}.{pin}", (jx_ + 7.62 + 2.54 * pin, P(ref, pin)[1]))
    c_v(s, "C7", "470u", 265.43, 264.16, polarized=True)
    pwr("+5V", (265.43, 264.16), "up")
    pwr("GND", (265.43, 271.78), "down")
    c_v(s, "C8", "100n", 275.59, 264.16)
    pwr("+5V", (275.59, 264.16), "up")
    pwr("GND", (275.59, 271.78), "down")
    s.auto_frame(m, "PADS  NeoTrellis x2 (I2C 0x2E / 0x2F)", pad=2.54, pad_top=1.27)
    for jx_ in (J9x, J10x):
        px = jx_ + 5.08
        W((px + 10.16, JNy), (px + 10.16, 259.08))
        W((px + 12.7, JNy + 2.54), (px + 12.7, 256.54))
        W((px + 15.24, JNy + 5.08), (px + 15.24, 254.0))
    W((SCLX, 259.08), (J9x + 15.24, 259.08), (J10x + 15.24, 259.08))
    r24 = r_v(s, "R24", "4k7", 210.82, 248.92, side="left")        # SDA pull-up
    pwr("+3V3", r24[0], "up")
    W((SDAX, 256.54), r24[1], (J9x + 17.78, 256.54), (J10x + 17.78, 256.54))
    W("U1.30", (314.96, 204.47), (314.96, 254.0), (J10x + 20.32, 254.0), (J9x + 20.32, 254.0))

    # ================= PLAY button =========================================
    W("U1.26", (317.5, 214.63), (317.5, 287.02))
    button(s, "BTN_PLAY", (317.5, 287.02), go="left", r_ref="R27", c_ref="C11", sw_ref="SW1",
           title="PLAY")

    # ================= MIDI IN (6N138, mirrored) ===========================
    m = s.mark()
    yr = 256.54
    s.place("U3", "Isolator:6N138", "6N138", 365.76, yr - 2.54, mirror="y",
            fields={"Reference": (365.76, yr - 12.7, 0, None),
                    "Value": (365.76, yr + 6.35, 0, None)})
    r17 = r_v(s, "R17", "4k7", 335.28, yr - 7.62, side="left")
    pwr("+3V3", r17[0], "up")
    W(r17[1], "U3.6")
    pwr("+5V", s.stub("U3", "8", 2.54), "up")
    pwr("GND", s.stub("U3", "5", 2.54), "down")
    s.nc("U3", "7")
    c_v(s, "C9", "100n", 347.98, yr - 15.24, side="left")
    pwr("+5V", (347.98, yr - 15.24), "up")
    pwr("GND", (347.98, yr - 7.62), "down")
    s.place("D2", "Device:D", "1N4148", 378.46, yr - 1.27, rot=270,
            fields={"Reference": (382.27, yr - 1.27, 0, None),
                    "Value": (384.81, yr + 1.27, 0, None)})
    p1, p2 = r_h(s, "R16", "220", 383.54, yr - 5.08, pin1="right", text="above")
    J1x, J1y = 401.32, yr - 2.54
    s.place("J1", "Connector:DIN-5_180degree", "MIDI IN", J1x, J1y,
            fields={"Reference": (J1x, J1y - 10.16, 0, None), "Value": (J1x, J1y + 8.89, 0, None)})
    W("U3.2", "D2.1", p2)
    W(p1, "J1.4")
    W("U3.3", (375.92, yr), (375.92, yr + 2.54), "D2.2")
    W("J1.5", (J1x + 10.16, yr - 5.08), (J1x + 10.16, yr + 7.62), (378.46, yr + 7.62), "D2.2")
    for pin in ("1", "2", "3"):
        s.nc("J1", pin)
    s.auto_frame(m, "MIDI IN  (opto-isolated)", pad=3.81)
    s.translate_since(m, RIGHT_MIDI, 0)
    W("U1.29", (325.12, 207.01), (325.12, yr), P("R17", 2))

    # ================= MIDI OUT ============================================
    m = s.mark()
    ym = 297.18
    r21 = r_h(s, "R21", "4k7", 332.74, ym, pin1="left", text="below")
    c, d = r_v(s, "R22", "47k", 342.9, ym - 7.62, side="left")
    pwr("+3V3", c, "up")
    s.place("Q1", "Transistor_BJT:Q_NPN_BEC", "MMBT3904", 350.52, ym,
            footprint="Package_TO_SOT_SMD:SOT-23",
            fields={"Reference": (356.87, ym + 1.27, 0, "left"),
                    "Value": (356.87, ym + 3.81, 0, "left")})
    W(r21[1], d, "Q1.1")
    pwr("GND", "Q1.2", "down")
    s.place("Q2", "Transistor_FET:Q_NMOS_GSD", "BSS138", 368.3, ym - 10.16,
            footprint="Package_TO_SOT_SMD:SOT-23",
            fields={"Reference": (374.65, ym - 8.89, 0, "left"),
                    "Value": (374.65, ym - 6.35, 0, "left")})
    e, f = r_v(s, "R20", "10k", 358.14, ym - 17.78, side="left")
    pwr("+3V3", e, "up")
    W("Q1.3", (353.06, ym - 10.16), f, "Q2.1")
    pwr("GND", "Q2.2", "down")
    g, h = r_h(s, "R19", "220", 375.92, ym - 20.32, pin1="right", text="above")
    W("Q2.3", (370.84, ym - 20.32), h)
    s.place("J2", "Connector:DIN-5_180degree", "MIDI OUT", 393.7, ym - 22.86, rot=180,
            fields={"Reference": (393.7, ym - 33.02, 0, None), "Value": (393.7, ym - 30.48, 0, None)})
    W(g, "J2.5")
    k, l = r_v(s, "R18", "220", 406.4, ym - 27.94, side="right")
    pwr("+5V", k, "up")
    W("J2.4", l)
    pwr("GND", "J2.2", "down")
    s.nc("J2", "1")
    s.nc("J2", "3")
    s.auto_frame(m, "MIDI OUT  (5 V current loop)", pad=3.81)
    s.translate_since(m, RIGHT_MIDI, DOWN_MIDI_OUT)
    W("U1.28", (320.04, 209.55), (320.04, ym + DOWN_MIDI_OUT), P("R21", 1))

    # ================= place the wired core on the sheet =================
    x1, y1, x2, y2 = s.extents_since(start)
    dx = round((297.0 - (x1 + x2) / 2) / 1.27) * 1.27
    dy = round((CORE_TOP - y1) / 1.27) * 1.27
    s.translate_since(start, dx, dy)
    core_bottom = y2 + dy

    # ================= power input: USB-C -> F1 -> D3 -> Q3 -> +5V =========
    m = s.mark()
    VB = 53.34
    s.place("J3", "Connector:USB_C_Receptacle_PowerOnly_6P", "USB-C PWR", 35.56, 60.96,
            fields={"Reference": (35.56, 40.64, 0, None), "Value": (35.56, 43.18, 0, None)})
    f1 = r_h(s, "F1", "2A hold", 57.15, VB, pin1="left", lib="Device:Polyfuse", text="above",
             fp="Fuse:Fuse_1812_4532Metric")
    s.place("D3", "Device:D_TVS", "SMBJ5.0CA", 71.12, VB + 3.81, rot=270,
            footprint="Diode_SMD:D_SMB",
            fields={"Reference": (75.57, VB + 2.54, 0, None), "Value": (79.0, VB + 5.08, 0, None)})
    a3, b3 = r_v(s, "R3", "100k", 88.9, VB, side="right")
    s.place("Q3", "Transistor_FET:Q_PMOS_GSD", "AO3401A", 104.14, VB + 2.54, rot=90, mirror="y",
            footprint="Package_TO_SOT_SMD:SOT-23",
            fields={"Reference": (104.14, VB - 7.62, 0, None), "Value": (104.14, VB - 5.08, 0, None)})
    s.place("SW4", "Switch:SW_SPST", "POWER", 96.52, VB + 12.7, rot=270,
            fields={"Reference": (100.33, VB + 11.43, 0, None), "Value": (101.6, VB + 13.97, 0, None)})
    c_v(s, "C1", "470u", 119.38, VB, polarized=True, side="left")
    W("J3.A9", f1[0])
    W(f1[1], "D3.1", a3, "Q3.2")
    W("Q3.3", (119.38, VB), (127.0, VB))
    pwr("+5V", (127.0, VB), "up")
    s.flag((127.0, VB), "down")
    pwr("GND", "D3.2", "down")
    W(b3, "SW4.1", "Q3.1")
    pwr("GND", "SW4.2", "down")
    pwr("GND", (119.38, VB + 7.62), "down")
    W("J3.A5", (66.04, P("J3", "A5")[1]))
    r_v(s, "R1", "5k1", 66.04, P("J3", "A5")[1], side="right")
    pwr("GND", (66.04, P("J3", "A5")[1] + 7.62), "down")
    W("J3.B5", (57.15, P("J3", "B5")[1]))
    r_v(s, "R2", "5k1", 57.15, P("J3", "B5")[1], side="right")
    pwr("GND", (57.15, P("J3", "B5")[1] + 7.62), "down")
    W("J3.SH", "J3.A12")
    pwr("GND", "J3.A12", "down")
    s.auto_frame(m, "POWER INPUT  USB-C 5 V 3 A (power only)", pad=3.81, grow=(0, 0, 3.81, 0))

    # ================= power rails ==========================================
    m = s.mark()
    # +5V -> D4 -> +5V_BP (Black Pill 5V pins)
    xa = 147.32
    pwr("+5V", (xa, VB), "up")
    s.place("D4", "Device:D_Schottky", "B5819W", xa + 8.89, VB, rot=180,
            footprint="Diode_SMD:D_SOD-123",
            fields={"Reference": (xa + 8.89, VB - 3.81, 0, None), "Value": (xa + 8.89, VB + 3.81, 0, None)})
    c_v(s, "C2", "10u", xa + 20.32, VB)
    W((xa, VB), "D4.2")
    W("D4.1", (xa + 20.32, VB), (xa + 27.94, VB))
    pwr("+5V_BP", (xa + 27.94, VB), "up")
    pwr("GND", (xa + 20.32, VB + 7.62), "down")
    s.flag((xa + 20.32, VB + 7.62), "right")
    # +5V -> FB1 -> +5VA (VS1053, PCM5102A)
    xb = 193.04
    pwr("+5V", (xb, VB), "up")
    fb = r_h(s, "FB1", "600R", xb + 5.08, VB, pin1="left", lib="Device:FerriteBead", text="above",
             fp="Inductor_SMD:L_0805_2012Metric")
    c_v(s, "C3", "100u", xb + 20.32, VB, polarized=True)
    c_v(s, "C4", "100n", xb + 30.48, VB)
    W((xb, VB), fb[0])
    W(fb[1], (xb + 20.32, VB), (xb + 30.48, VB), (xb + 43.18, VB))
    pwr("+5VA", (xb + 43.18, VB), "up")
    s.flag((xb + 43.18, VB), "down")
    pwr("GND", (xb + 20.32, VB + 7.62), "down")
    pwr("GND", (xb + 30.48, VB + 7.62), "down")
    # +3V3 from the Black Pill LDO: decoupling and power LED
    xc = 254.0
    pwr("+3V3", (xc, VB), "up")
    c_v(s, "C5", "10u", xc + 10.16, VB)
    c_v(s, "C6", "100n", xc + 20.32, VB)
    r_v(s, "R4", "1k5", xc + 30.48, VB)
    s.place("D1", "Device:LED", "RED", xc + 30.48, VB + 15.24, rot=90,
            fields={"Reference": (xc + 34.29, VB + 13.97, 0, None),
                    "Value": (xc + 34.29, VB + 16.51, 0, None)})
    W((xc, VB), (xc + 10.16, VB), (xc + 20.32, VB), (xc + 30.48, VB))
    W("R4.2", "D1.2")
    pwr("GND", (xc + 10.16, VB + 7.62), "down")
    pwr("GND", (xc + 20.32, VB + 7.62), "down")
    pwr("GND", "D1.1", "down")
    for x, lines in ((xa, ("D4: Black Pill USB and the adapter", "never back-feed each other.")),
                     (xb, ("FB1 keeps LED / TFT noise", "out of the audio supply.")),
                     (xc, ("+3V3 = Black Pill LDO (300 mA),", "logic only."))):
        for i, line in enumerate(lines):
            s.text(line, x, VB + 22.86 + 2.54 * i, size=1.0)
    s.auto_frame(m, "POWER RAILS  +5V_BP / +5VA / +3V3", pad=3.81)
    # one band: both power frames share top and bottom
    (a1, b1, c1, d1, t1), (a2, b2, c2, d2, t2) = s.frames[-2], s.frames[-1]
    top, bot = min(b1, b2), max(d1, d2)
    s.frames[-2], s.frames[-1] = (a1, top, c1, bot, t1), (a2, top, c2, bot, t2)

    # ================= notes ================================================
    notes = [
        "NOTES",
        "1. Same circuit as v2 (76 nets, same parts and footprints), drawn with wires.",
        "2. Six signals connect by net label instead of a long wire: VS_XRST, VS_XCS, VS_XDCS, LCD_RST, MCP_INT, ENC5_SW.",
        "3. Rails: +5V (TFT, NeoTrellis, MIDI), +5V_BP (Black Pill via D4), +5VA (VS1053, PCM5102A via FB1), +3V3 (logic).",
        "4. Budget: about 0.45 A typical, 1.1 A max with NeoTrellis LEDs capped at 0.5 A. Use a 5 V 3 A USB-C adapter.",
        "5. G474 link: SYN_TX = F411 PA15 -> G474 RX, SYN_RX = G474 TX -> F411 PB3. Nucleo dev: keep JP1 open.",
        "6. Series R: 100R on UART, 33R on I2S. PCM5102A pin 6 (SCK) is tied to GND.",
        "7. MIDI IN: DIN pin 2 has no ground connection. MIDI OUT: 220R on pins 4 and 5.",
        "8. I2C1 PB8/PB9: MCP23017 0x20 (A0-A2 = GND), NeoTrellis 0x2E and 0x2F (A0 bridged), INT shared.",
        "9. MCP23017 GPA7/GPB7 unused (output-only on current silicon). Encoders use the MCP weak pull-ups.",
        "10. Mixer v0 is passive (-6 dB per input, about 5k output): line level only.",
    ]
    nx, ny = 20.32, round((core_bottom + 10.16) / 1.27) * 1.27
    m = s.mark()
    for i, line in enumerate(notes):
        s.text(line, nx, ny + 5.08 * i, size=1.8 if i == 0 else 1.5, bold=(i == 0))
    return s


def find_stock(path: Path) -> Path:
    """The KiCad symbol folder: the given one, or any installed KiCad version."""
    if path.exists():
        return path
    for root in (Path(r"C:\Program Files\KiCad"), Path("/usr/share/kicad"),
                 Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport")):
        for cand in sorted(root.glob("*/share/kicad/symbols"), reverse=True) + [root / "symbols"]:
            if cand.exists():
                return cand
    raise SystemExit(f"KiCad symbol folder not found: {path} (use --stock DIR)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stock", type=Path, default=STOCK, help="KiCad symbol folder")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    s = build(find_stock(args.stock))
    s.finalize()
    result = s.check()
    for e in result["errors"]:
        print("ERROR", e)
    if result["errors"]:
        raise SystemExit("drawing check failed; nothing written")
    tb = ('(title_block (title "Synthesizer v3 - F411 controller + VS1053 + G474 synth link") '
          '(rev "D") (comment 1 "Main board v3: same circuit as v2, drawn as a wired schematic") '
          '(comment 2 "Module carrier; verify module revisions and footprints before PCB fabrication"))')
    args.output.write_text(s.render(paper="A2", title_block=tb), encoding="utf-8")
    parts = len([r for r in s.parts if not r.startswith("#")])
    nets = len([n for n in result["nets"] if len(n[1]) > 1 or n[0]])
    print(f"{args.output}: {parts} parts, {nets} nets, drawing check OK")


if __name__ == "__main__":
    main()
