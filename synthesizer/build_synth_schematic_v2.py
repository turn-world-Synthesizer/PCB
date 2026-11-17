"""Build the STM32F411 Black Pill synthesizer schematic (main board v2).

v2 changes from v1:
  * Dedicated USB-C 5 V power input (CC resistors, polyfuse, TVS, P-MOSFET
    load switch) instead of powering everything from the Black Pill USB.
  * Rails: +5V (LED/TFT/MIDI), +5V_BP (Black Pill via Schottky, no VBUS
    backfeed), +5VA (VS1053/PCM5102A/mixer via ferrite), +3V3 (Black Pill
    LDO, logic only).
  * G474 synth link header: USART1 (PA15/PB3) to the G474 and I2S from the
    G474 to the PCM5102A, with series resistors.  The F411 no longer drives I2S.
  * PB5 senses the main 5 V rail.
  * Two NeoTrellis connectors and the TFT on +5V, passive v0 audio mixer.

The schematic is generated from the repository pin map and from project-local
module symbols.  Connector footprints for off-board modules remain unassigned
until the physical modules are measured.

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
import uuid
from collections import defaultdict
from pathlib import Path


HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "synthesizer.kicad_sch"
STOCK = Path(r"C:\Program Files\KiCad\10.0\share\kicad\symbols")
SHEET_UUID = "161bcf67-49cc-4d0d-8cc2-cd831a16933a"
PROJECT = "synthesizer"
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


def snap(value: float) -> float:
    return round(value / GRID) * GRID


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


def symbol_path(library: str, name: str) -> Path:
    if library == "Synthesizer_Modules":
        return HERE / f"{library}.kicad_sym"
    merged = STOCK / f"{library}.kicad_sym"
    if merged.exists():
        return merged
    # KiCad library repository layout: one file per symbol.
    return STOCK / f"{library}.kicad_symdir" / f"{name}.kicad_sym"


def extract_symbol(lib_id: str) -> str:
    library, name = lib_id.split(":", 1)
    path = symbol_path(library, name)
    data = path.read_text(encoding="utf-8")
    match = re.search(r'\(symbol\s+' + re.escape(q(name)) + r'(?=\s|\))', data)
    if not match:
        raise ValueError(f"{lib_id} not found in {path}")
    source = balanced_at(data, match.start())
    tree = parse_sexpr(source)
    if one_child(tree, "extends") is not None:
        raise ValueError(f"Inherited symbol needs expansion: {lib_id}")
    return source.replace(f'(symbol {q(name)}', f'(symbol {q(lib_id)}', 1)


def pin_defs(source: str) -> dict[str, tuple[float, float, int, str]]:
    tree = parse_sexpr(source)
    result = {}
    for part in children(tree, "symbol"):
        for pin in children(part, "pin"):
            at = one_child(pin, "at")
            number = one_child(pin, "number")
            name = one_child(pin, "name")
            result[str(number[1])] = (float(at[1]), float(at[2]), int(float(at[3])), str(name[1]))
    return result


def property_defs(source: str) -> dict[str, tuple]:
    """name -> (value, x, y, angle, justify tokens, hidden) from the library."""
    tree = parse_sexpr(source)
    result = {}
    for prop in children(tree, "property"):
        at = one_child(prop, "at")
        effects = one_child(prop, "effects") or []
        justify = one_child(effects, "justify")
        hidden = any(
            (isinstance(node, list) and node and node[0] == "hide"
             and (len(node) == 1 or str(node[1]) == "yes"))
            or (isinstance(node, Atom) and node == "hide")
            for node in list(prop) + list(effects)
        )
        angle = int(float(at[3])) if len(at) > 3 else 0
        result[str(prop[1])] = (str(prop[2]), float(at[1]), float(at[2]), angle,
                                [str(t) for t in justify[1:]] if justify else None, hidden)
    return result


class Builder:
    def __init__(self):
        self.items: list[str] = []
        self.cache: dict[str, str] = {}
        self.pinmaps: dict[str, dict[str, tuple[float, float, int, str]]] = {}
        self.libprops: dict[str, dict[str, tuple[str, float, float]]] = {}
        self.placed: dict[str, tuple[str, float, float, int]] = {}
        self.net_members: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.connected_pins: set[tuple[str, str]] = set()
        self.nc_pins: set[tuple[str, str]] = set()
        self.counter = 0

    def uid(self) -> str:
        self.counter += 1
        return str(uuid.uuid5(uuid.UUID(SHEET_UUID), f"synth-v2-{self.counter}"))

    def lib(self, lib_id: str):
        if lib_id not in self.cache:
            source = extract_symbol(lib_id)
            self.cache[lib_id] = source
            self.pinmaps[lib_id] = pin_defs(source)
            self.libprops[lib_id] = property_defs(source)

    def pin(self, ref: str, number: int | str) -> tuple[float, float, int]:
        lib_id, sx, sy, rotation = self.placed[ref]
        px, py, direction, _ = self.pinmaps[lib_id][str(number)]
        radians = math.radians(rotation)
        rx = px * math.cos(radians) - py * math.sin(radians)
        ry = px * math.sin(radians) + py * math.cos(radians)
        return round(sx + rx, 4), round(sy - ry, 4), (direction + rotation) % 360

    def place(
        self, lib_id: str, ref: str, value: str, x: float, y: float,
        *, rotation: int = 0, footprint: str | None = None, on_board: bool = True,
        ref_at: tuple[float, float] | None = None,
        value_at: tuple[float, float] | None = None,
    ):
        self.lib(lib_id)
        x, y = snap(x), snap(y)
        self.placed[ref] = (lib_id, x, y, rotation)
        defaults = self.libprops[lib_id]
        empty = ("", 0.0, 0.0, 0, None, False)
        def prop_at(name):
            _, px, py = defaults.get(name, empty)[:3]
            radians = math.radians(rotation)
            return (x + px * math.cos(radians) - py * math.sin(radians),
                    y - px * math.sin(radians) - py * math.cos(radians))
        def prop_style(name, override):
            # Library text angle/justification unless the caller moved the text.
            if override is not None:
                return {"angle": 0, "justify": None, "hidden": False}
            _, _, _, angle, justify, hidden = defaults.get(name, empty)
            return {"angle": (angle + rotation) % 180, "justify": justify, "hidden": hidden}
        refx, refy = ref_at or prop_at("Reference")
        valx, valy = value_at or prop_at("Value")
        if footprint is None:
            footprint = defaults.get("Footprint", empty)[0]
        pins = self.pinmaps[lib_id]
        parts = [
            "(symbol", f"(lib_id {q(lib_id)})", f"(at {n(x)} {n(y)} {rotation})",
            "(unit 1)", "(exclude_from_sim no)", "(in_bom yes)",
            f"(on_board {'yes' if on_board else 'no'})", "(dnp no)",
            f"(uuid {q(self.uid())})",
            self.property("Reference", ref, refx, refy, **prop_style("Reference", ref_at)),
            self.property("Value", value, valx, valy, **prop_style("Value", value_at)),
            self.property("Footprint", footprint, x, y, hidden=True),
            self.property("Datasheet", defaults.get("Datasheet", empty)[0], x, y, hidden=True),
        ]
        parts.extend(f'(pin {q(number)} (uuid {q(self.uid())}))' for number in pins)
        parts.append(
            f'(instances (project {q(PROJECT)} (path "/{SHEET_UUID}"'
            f' (reference {q(ref)}) (unit 1))))'
        )
        parts.append(")")
        self.items.append("\n".join(parts))

    def property(self, name, value, x, y, *, hidden=False, angle=0, justify=None):
        just = f" (justify {' '.join(justify)})" if justify else ""
        return (f'(property {q(name)} {q(value)} (at {n(x)} {n(y)} {angle}) '
                f'(effects (font (size 1.27 1.27)){just}{" (hide yes)" if hidden else ""}))')

    def wire(self, x1, y1, x2, y2):
        if abs(x1 - x2) < 0.001 and abs(y1 - y2) < 0.001:
            return
        self.items.append(
            f'(wire (pts (xy {n(x1)} {n(y1)}) (xy {n(x2)} {n(y2)})) '
            f'(stroke (width 0) (type solid)) (uuid {q(self.uid())}))'
        )

    def label(self, name, x, y, *, angle=0):
        self.items.append(
            f'(label {q(name)} (at {n(x)} {n(y)} {angle}) '
            f'(effects (font (size 1.27 1.27)) (justify left bottom)) (uuid {q(self.uid())}))'
        )

    def pin_net(self, ref: str, number: int | str, net: str, *, stub=7.62):
        x, y, direction = self.pin(ref, number)
        radians = math.radians(direction)
        ex = round(x - stub * math.cos(radians), 4)
        ey = round(y + stub * math.sin(radians), 4)
        self.wire(x, y, ex, ey)
        self.label(net, ex, ey)
        self.net_members[net].append((ref, str(number)))
        self.connected_pins.add((ref, str(number)))

    def tie(self, ref: str, number: int | str, net: str, *, to: int | str):
        """Register a pin stacked on another pin (same position, same net)."""
        if self.pin(ref, number)[:2] != self.pin(ref, to)[:2]:
            raise ValueError(f"{ref}.{number} is not stacked on {ref}.{to}")
        self.net_members[net].append((ref, str(number)))
        self.connected_pins.add((ref, str(number)))

    def nc(self, ref: str, number: int | str):
        x, y, _ = self.pin(ref, number)
        self.items.append(f'(no_connect (at {n(x)} {n(y)}) (uuid {q(self.uid())}))')
        self.nc_pins.add((ref, str(number)))

    def check_complete(self, ref: str):
        lib_id, _, _, _ = self.placed[ref]
        for number in self.pinmaps[lib_id]:
            key = (ref, number)
            if key not in self.connected_pins and key not in self.nc_pins:
                raise ValueError(f"Unassigned pin {ref}.{number}")
            if key in self.connected_pins and key in self.nc_pins:
                raise ValueError(f"Pin both connected and NC: {ref}.{number}")

    def check_all(self):
        for ref in self.placed:
            if not ref.startswith("#"):
                self.check_complete(ref)
        lonely = {net: members for net, members in self.net_members.items()
                  if len(members) < 2 and net not in POWER_FLAGGED}
        if lonely:
            raise ValueError(f"Nets with a single connection: {lonely}")

    def text(self, message, x, y, *, size=1.27):
        self.items.append(
            f'(text {q(message)} (at {n(x)} {n(y)} 0) '
            f'(effects (font (size {n(size)} {n(size)})) (justify left)) '
            f'(uuid {q(self.uid())}))'
        )

    def render(self, *, title: str) -> str:
        items = [
            "(kicad_sch", "(version 20250114)", '(generator "eeschema")',
            '(generator_version "9.0")', f'(uuid {q(SHEET_UUID)})', '(paper "A2")',
            f'(title_block (title {q(title)}) (rev "C") '
            '(comment 1 "Main board v2: dedicated 5V input, G474 synth link, passive mixer v0")'
            ' (comment 2 "Module carrier; verify module revisions and footprints before PCB fabrication"))',
            "(lib_symbols\n" + "\n".join(self.cache.values()) + "\n)",
        ]
        items.extend(self.items)
        items.extend(['(sheet_instances (path "/" (page "1")))', '(embedded_fonts no)', ')'])
        return "\n\t".join(items) + "\n"


# Nets that are driven by a PWR_FLAG label only (no symbol pin besides it).
POWER_FLAGGED: set[str] = set()


def inspect(ids: list[str]):
    for lib_id in ids:
        source = extract_symbol(lib_id)
        print(lib_id)
        for number, data in pin_defs(source).items():
            print(" ", number, data)


def build_circuit() -> Builder:
    b = Builder()
    refs = defaultdict(int)
    refs["J"] = 2  # J1/J2 are the MIDI DIN sockets.

    def ref(prefix: str) -> str:
        refs[prefix] += 1
        return f"{prefix}{refs[prefix]}"

    def two_pin(lib_id: str, prefix: str, value: str, x: float, y: float,
                net1: str, net2: str, *, footprint: str | None = None,
                stub: float = 2.54, name: str | None = None) -> str:
        """Vertical parts (R, C, ferrite, fuse): pin 1 on top, pin 2 below.
        Horizontal parts (diodes, switches, jumpers) use a longer stub so the
        label text clears the symbol body."""
        name = name or ref(prefix)
        b.place(lib_id, name, value, x, y, footprint=footprint)
        b.pin_net(name, 1, net1, stub=stub)
        b.pin_net(name, 2, net2, stub=stub)
        return name

    def resistor(value: str, x: float, y: float, net1: str, net2: str,
                 *, footprint: str | None = None) -> str:
        return two_pin("Device:R", "R", value, x, y, net1, net2, footprint=footprint)

    def capacitor(value: str, x: float, y: float, net1: str, net2: str,
                  *, polarized: bool = False) -> str:
        lib_id = "Device:C_Polarized" if polarized else "Device:C"
        return two_pin(lib_id, "C", value, x, y, net1, net2)

    def power_flag(rail: str, x: float, y: float):
        name = f"#FLG{refs['#FLG'] + 1:02d}"
        refs["#FLG"] += 1
        b.place("power:PWR_FLAG", name, "PWR_FLAG", x, y)
        b.pin_net(name, 1, rail, stub=2.54)

    def connector(size: int, title: str, x: float, y: float,
                  signals: list[str], *, note: str | None = None) -> str:
        if len(signals) != size:
            raise ValueError(f"{title}: {len(signals)} nets for {size} pins")
        name = ref("J")
        b.place(f"Connector:Conn_01x{size:02d}_Pin", name, title, x, y,
                ref_at=(x, y - size * 1.27 - 5.08),
                value_at=(x + 18, y - size * 1.27 - 5.08))
        for number, net in enumerate(signals, 1):
            if net:
                b.pin_net(name, number, net, stub=10.16)
            else:
                b.nc(name, number)
        if note:
            b.text(note, x - 2.54, y + size * 1.27 + 10.16, size=1.0)
        return name

    # ------------------------------------------------------------------
    # POWER INPUT: USB-C (power only) -> F1 -> D3 -> Q3 load switch -> +5V
    # ------------------------------------------------------------------
    b.text("POWER INPUT / USB-C 5V 3A (power only)", 25.4, 30.48, size=1.52)
    usb = ref("J")
    b.place("Connector:USB_C_Receptacle_PowerOnly_6P", usb, "USB-C PWR", 35.56, 55.88)
    b.pin_net(usb, "A9", "VBUS_IN", stub=7.62)
    b.tie(usb, "B9", "VBUS_IN", to="A9")
    b.pin_net(usb, "A5", "USB_CC1", stub=7.62)
    b.pin_net(usb, "B5", "USB_CC2", stub=7.62)
    b.pin_net(usb, "A12", "GND", stub=5.08)
    b.tie(usb, "B12", "GND", to="A12")
    b.pin_net(usb, "SH", "GND", stub=5.08)
    b.check_complete(usb)
    resistor("5k1", 76.2, 68.58, "USB_CC1", "GND")
    resistor("5k1", 88.9, 68.58, "USB_CC2", "GND")
    two_pin("Device:Polyfuse", "F", "2A hold", 104.14, 55.88, "VBUS_IN", "VBUS_F",
            footprint="Fuse:Fuse_1812_4532Metric", name="F1")
    two_pin("Device:D_TVS", "D", "SMBJ5.0CA", 121.92, 68.58, "VBUS_F", "GND",
            footprint="Diode_SMD:D_SMB", stub=7.62, name="D3")
    b.place("Transistor_FET:Q_PMOS_GSD", "Q3", "AO3401A", 152.4, 55.88,
            footprint="Package_TO_SOT_SMD:SOT-23")
    b.pin_net("Q3", 1, "SW_GATE", stub=10.16)
    b.pin_net("Q3", 2, "VBUS_F", stub=2.54)
    b.pin_net("Q3", 3, "+5V", stub=2.54)
    b.check_complete("Q3")
    resistor("100k", 167.64, 55.88, "VBUS_F", "SW_GATE")
    two_pin("Switch:SW_SPST", "SW", "POWER", 152.4, 76.2, "SW_GATE", "GND",
            stub=10.16, name="SW4")
    capacitor("470u", 180.34, 55.88, "+5V", "GND", polarized=True)
    power_flag("+5V", 101.6, 86.36)
    power_flag("+5VA", 127.0, 86.36)
    power_flag("GND", 152.4, 86.36)

    # ------------------------------------------------------------------
    # POWER DISTRIBUTION: +5V_BP (Schottky), +5VA (ferrite), +3V3, 5V sense
    # ------------------------------------------------------------------
    b.text("POWER DISTRIBUTION", 25.4, 93.98, size=1.52)
    two_pin("Device:D_Schottky", "D", "B5819W", 43.18, 104.14, "+5V_BP", "+5V",
            footprint="Diode_SMD:D_SOD-123", stub=7.62, name="D4")
    capacitor("10u", 30.48, 121.92, "+5V_BP", "GND")
    two_pin("Device:FerriteBead", "FB", "600R", 63.5, 111.76, "+5V", "+5VA",
            footprint="Inductor_SMD:L_0805_2012Metric", name="FB1")
    capacitor("100u", 86.36, 111.76, "+5VA", "GND", polarized=True)
    capacitor("100n", 99.06, 111.76, "+5VA", "GND")
    capacitor("10u", 116.84, 111.76, "+3V3", "GND")
    capacitor("100n", 129.54, 111.76, "+3V3", "GND")
    resistor("1k5", 142.24, 106.68, "+3V3", "PWR_LED_A")
    b.place("Device:LED", "D1", "RED", 142.24, 124.46)
    b.pin_net("D1", 2, "PWR_LED_A", stub=7.62)
    b.pin_net("D1", 1, "GND", stub=7.62)
    resistor("10k", 170.18, 106.68, "+5V", "5V_SENSE")
    resistor("15k", 170.18, 124.46, "5V_SENSE", "GND")
    b.text("D4 keeps the Black Pill USB VBUS and the adapter apart. FB1 isolates audio from LED/TFT PWM noise.",
           25.4, 133.35, size=1.0)
    b.text("+3V3 is the Black Pill LDO (300 mA): logic only. TFT, NeoTrellis and PCM5102A run from +5V/+5VA.",
           25.4, 137.16, size=1.0)

    # ------------------------------------------------------------------
    # MCU: Black Pill module (physical pad numbers)
    # ------------------------------------------------------------------
    b.place("Synthesizer_Modules:BlackPill_F411CEU6", "U1",
            "WeAct Black Pill F411CEU6", 241.3, 124.46,
            ref_at=(241.3, 90.17), value_at=(241.3, 157.48))
    blackpill = {
        1: "LCD_CS", 2: "LCD_SCK", 3: "MCP_INT", 4: "LCD_MOSI",
        5: "LCD_LED", 6: "BTN_INST", 7: "ENC5_SW",
        10: "SYN_TX", 11: "SYN_RX", 12: "VS_XRST",
        13: "5V_SENSE", 14: "VS_XCS", 15: "VS_XDCS",
        16: "I2C_SCL", 17: "I2C_SDA", 18: "+5V_BP",
        19: "GND", 20: "+3V3", 40: "+5V_BP", 39: "GND",
        38: "+3V3", 37: "BTN_STOP", 35: "VS_SDCS",
        34: "LCD_DC", 33: "VS_MOSI", 32: "VS_MISO",
        31: "VS_SCK", 30: "TRELLIS_INT", 29: "MIDI_RX",
        28: "MIDI_TX", 27: "VS_DREQ", 26: "BTN_PLAY",
        25: "LCD_RST",
    }
    for number in b.pinmaps["Synthesizer_Modules:BlackPill_F411CEU6"]:
        net = blackpill.get(int(number))
        if net:
            b.pin_net("U1", number, net, stub=11.43)
        else:
            b.nc("U1", number)
    b.check_complete("U1")
    b.text("BLACK PILL MODULE / v2 pins (PA15, PB3, PB5 changed from pinmap.md v1)", 197.485, 73.66, size=1.52)
    b.text("USB, HSE and SWD are on the module. PA11/PA12 are unused on carrier.",
           213.36, 161.29, size=1.0)
    b.text("PA15/PB3 = USART1 to G474, PB5 = 5V_SENSE (I2S moved to the G474).",
           213.36, 165.1, size=1.0)

    # ------------------------------------------------------------------
    # G474 SYNTH LINK: UART to the G474, I2S from the G474 to the PCM5102A
    # ------------------------------------------------------------------
    b.text("G474 SYNTH LINK (Nucleo-G474RE now, G474 board later)", 302.26, 30.48, size=1.52)
    resistor("100R", 314.96, 60.96, "SYN_TX", "SYN_TX_H")
    resistor("100R", 330.2, 60.96, "SYN_RX", "SYN_RX_H")
    resistor("33R", 345.44, 60.96, "I2S_BCK", "I2S_BCK_H")
    resistor("33R", 360.68, 60.96, "I2S_LRCK", "I2S_LRCK_H")
    resistor("33R", 375.92, 60.96, "I2S_DIN", "I2S_DIN_H")
    two_pin("Jumper:SolderJumper_2_Open", "JP", "OPEN (dev)", 403.86, 60.96,
            "+5V", "SYN_5V", stub=7.62, name="JP1")
    connector(10, "G474 SYNTH LINK", 431.8, 60.96,
              ["SYN_5V", "GND", "SYN_TX_H", "SYN_RX_H", "GND",
               "I2S_BCK_H", "GND", "I2S_LRCK_H", "I2S_DIN_H", "GND"])
    b.text("SYN_TX: F411 PA15 (USART1_TX) -> G474 RX.  SYN_RX: G474 TX -> F411 PB3 (USART1_RX).",
           302.26, 80.01, size=1.0)
    b.text("Nucleo dev: powered by its own ST-LINK USB. Keep JP1 open and connect GND + signals only.",
           302.26, 83.82, size=1.0)
    b.text("Series R: 100R on UART, 33R on I2S (damping, limits back-powering when one side is off).",
           302.26, 87.63, size=1.0)

    # ------------------------------------------------------------------
    # VS1053 module (GM backing band)
    # ------------------------------------------------------------------
    # Adafruit breakout: pin numbers match the physical JP2 (1-16) and JP3
    # (17-32) header pads, rather than the numbering in the reference PDF.
    b.place("Synthesizer_Modules:Adafruit_VS1053_Breakout_V2", "U2",
            "Adafruit VS1053 breakout v2", 411.48, 124.46,
            ref_at=(411.48, 97.79), value_at=(411.48, 154.94))
    vs = {
        1: "VS_R", 2: "VS_L", 3: "GND", 4: "GND",
        5: "GND", 6: "VS_DREQ", 7: "+5VA", 9: "GND",
        10: "VS_MISO", 11: "VS_MOSI", 12: "VS_SCK",
        13: "VS_XRST", 14: "VS_XCS", 15: "VS_SDCS",
        16: "VS_XDCS", 20: "GND",
    }
    for number in b.pinmaps["Synthesizer_Modules:Adafruit_VS1053_Breakout_V2"]:
        net = vs.get(int(number))
        if net:
            b.pin_net("U2", number, net, stub=10.16)
        else:
            b.nc("U2", number)
    b.check_complete("U2")
    b.text("VS1053: VIN=+5VA; its 3V3 pins are regulator outputs (NC here).",
           365.76, 169.545, size=1.0)
    b.text("U2 physical header pin order follows Adafruit v2 JP2/JP3; verify actual board.",
           365.76, 174.625, size=1.0)

    # ------------------------------------------------------------------
    # AUDIO: passive v0 mixer (VS1053 + PCM5102A) -> line out
    # ------------------------------------------------------------------
    b.text("AUDIO MIXER v0 (passive) / LINE OUT", 469.9, 30.48, size=1.52)
    resistor("10k", 480.06, 50.8, "VS_L", "LINE_L")
    resistor("10k", 495.3, 50.8, "PCM_L", "LINE_L")
    resistor("10k", 510.54, 50.8, "VS_R", "LINE_R")
    resistor("10k", 525.78, 50.8, "PCM_R", "LINE_R")
    connector(3, "STEREO LINE OUT", 546.1, 50.8, ["LINE_L", "LINE_R", "GND"])
    b.text("Passive sum: -6 dB per input, about 5k output impedance.", 469.9, 68.58, size=1.0)
    b.text("Line level only (powered speakers / amp). Op-amp stage comes with the audio design.",
           469.9, 72.39, size=1.0)

    # ------------------------------------------------------------------
    # MODULE INTERFACES: TFT, PCM5102A, NeoTrellis x2
    # ------------------------------------------------------------------
    b.text("DISPLAY / AUDIO / I2C MODULE INTERFACES", 469.9, 88.9, size=1.52)
    connector(8, "TFT ILI9488 SPI", 487.68, 111.76,
              ["+5V", "GND", "LCD_SCK", "LCD_MOSI", "LCD_CS",
               "LCD_DC", "LCD_RST", "LCD_LED"],
              note="VCC=+5V (open the module 3V3 jumper J1 if fitted); 3.3V signals; MISO not connected.")
    connector(6, "PCM5102A I2S", 487.68, 154.94,
              ["+5VA", "GND", "I2S_BCK", "I2S_DIN", "I2S_LRCK", "GND"],
              note="VIN=+5VA (check module LDO). Pin 6 = module SCK to GND. I2S comes from the G474 link.")
    connector(3, "PCM5102A AUDIO OUT", 487.68, 190.5,
              ["PCM_L", "GND", "PCM_R"],
              note="Wire from the module output jack pads (L / G / R).")
    connector(5, "NeoTrellis 1", 487.68, 228.6,
              ["+5V", "GND", "I2C_SCL", "I2C_SDA", "TRELLIS_INT"],
              note="Board 1: I2C 0x2E (default).")
    connector(5, "NeoTrellis 2", 541.02, 228.6,
              ["+5V", "GND", "I2C_SCL", "I2C_SDA", "TRELLIS_INT"],
              note="Board 2: 0x2F (A0 bridged). INT shared.")
    capacitor("470u", 492.76, 264.16, "+5V", "GND", polarized=True)
    capacitor("100n", 505.46, 264.16, "+5V", "GND")
    b.text("Bulk caps next to the NeoTrellis connectors. Firmware caps LED current at about 0.5 A total.",
           469.9, 278.13, size=1.0)

    # ------------------------------------------------------------------
    # MIDI IN: opto-isolated current loop.  DIN pin 2 is isolated from GND.
    # ------------------------------------------------------------------
    b.text("MIDI IN / 31.25 kbit/s", 45.72, 142.24, size=1.52)
    b.place("Connector:DIN-5_180degree", "J1", "MIDI IN", 71.12, 175.26,
            ref_at=(71.12, 155.575), value_at=(71.12, 195.58))
    for pin in (1, 2, 3):
        b.nc("J1", pin)
    b.pin_net("J1", 4, "MIDI_IN_4", stub=7.62)
    b.pin_net("J1", 5, "MIDI_LED_K", stub=7.62)
    resistor("220", 116.84, 168.91, "MIDI_IN_4", "MIDI_LED_A")
    b.place("Device:D", "D2", "1N4148", 139.7, 156.21)
    b.pin_net("D2", 1, "MIDI_LED_A", stub=12.7)
    b.pin_net("D2", 2, "MIDI_LED_K", stub=12.7)
    b.place("Isolator:6N138", "U3", "6N138", 161.29, 174.625,
            ref_at=(161.29, 164.465), value_at=(161.29, 187.325))
    for pin, net in {2: "MIDI_LED_A", 3: "MIDI_LED_K", 5: "GND",
                     6: "MIDI_RX", 8: "+5V"}.items():
        b.pin_net("U3", pin, net, stub=12.7 if pin in (2, 3) else 5.08)
    for pin in (1, 4, 7):
        b.nc("U3", pin)
    b.check_complete("U3")
    resistor("4k7", 186.69, 174.625, "+3V3", "MIDI_RX")
    capacitor("100n", 203.2, 174.625, "+5V", "GND")
    b.text("DIN IN pin 2/shield has no DC ground connection.",
           45.72, 205.74, size=1.0)

    # ------------------------------------------------------------------
    # MIDI OUT: 5 V current loop with a two-transistor non-inverting stage.
    # ------------------------------------------------------------------
    b.text("MIDI OUT / protected 5V current loop", 45.72, 223.52, size=1.52)
    b.place("Connector:DIN-5_180degree", "J2", "MIDI OUT", 71.12, 260.35,
            ref_at=(71.12, 240.665), value_at=(71.12, 280.67))
    for pin in (1, 3):
        b.nc("J2", pin)
    b.pin_net("J2", 2, "GND", stub=5.08)
    b.pin_net("J2", 4, "MIDI_OUT_4", stub=7.62)
    b.pin_net("J2", 5, "MIDI_OUT_5", stub=7.62)
    resistor("220", 113.03, 248.92, "+5V", "MIDI_OUT_4")
    resistor("220", 133.35, 248.92, "MIDI_OUT_5", "MIDI_OUT_SINK")
    b.place("Transistor_FET:Q_NMOS_GSD", "Q2", "BSS138", 185.42, 260.35,
            footprint="Package_TO_SOT_SMD:SOT-23",
            ref_at=(195.58, 255.27), value_at=(195.58, 260.35))
    for pin, net in {1: "MOUT_GATE", 2: "GND", 3: "MIDI_OUT_SINK"}.items():
        b.pin_net("Q2", pin, net, stub=12.7 if pin == 1 else 5.08)
    b.place("Transistor_BJT:Q_NPN_BEC", "Q1", "MMBT3904", 161.29, 260.35,
            footprint="Package_TO_SOT_SMD:SOT-23",
            ref_at=(153.67, 255.27), value_at=(153.67, 265.43))
    for pin, net in {1: "MOUT_BASE", 2: "GND", 3: "MOUT_GATE"}.items():
        b.pin_net("Q1", pin, net, stub=12.7 if pin == 1 else 5.08)
    resistor("10k", 213.36, 245.11, "+3V3", "MOUT_GATE")
    resistor("4k7", 231.14, 245.11, "MIDI_TX", "MOUT_BASE")
    resistor("47k", 248.92, 245.11, "+3V3", "MOUT_BASE")
    b.text("J2 pins 4/5 use 220R each; Q2 sinks cable current, Q1 restores UART polarity.",
           45.72, 292.1, size=1.0)

    # ------------------------------------------------------------------
    # I2C1 pullups, MCP23017 and the encoder harness.
    # ------------------------------------------------------------------
    b.text("I2C1 / MCP23017 / ENCODERS", 290.83, 192.405, size=1.52)
    resistor("4k7", 302.26, 208.28, "+3V3", "I2C_SCL")
    resistor("4k7", 317.5, 208.28, "+3V3", "I2C_SDA")
    b.place("Interface_Expansion:MCP23017x-x-SO", "U4", "MCP23017", 359.41, 249.555,
            ref_at=(359.41, 220.345), value_at=(345.44, 281.94))
    mcp = {
        1: "ENC4_A", 2: "ENC4_B", 3: "ENC5_A", 4: "ENC5_B",
        5: "ENC2_SW", 6: "ENC3_SW", 7: "ENC4_SW",
        9: "+3V3", 10: "GND", 12: "I2C_SCL", 13: "I2C_SDA",
        15: "GND", 16: "GND", 17: "GND", 18: "MCP_RESET",
        20: "MCP_INT", 21: "ENC1_A", 22: "ENC1_B",
        23: "ENC2_A", 24: "ENC2_B", 25: "ENC3_A",
        26: "ENC3_B", 27: "ENC1_SW",
    }
    for number in b.pinmaps["Interface_Expansion:MCP23017x-x-SO"]:
        net = mcp.get(int(number))
        if net:
            b.pin_net("U4", number, net, stub=6.35)
        else:
            b.nc("U4", number)
    b.check_complete("U4")
    resistor("10k", 392.43, 205.74, "+3V3", "MCP_RESET")
    resistor("10k", 407.67, 205.74, "+3V3", "MCP_INT")
    capacitor("100n", 422.91, 205.74, "+3V3", "GND")
    b.text("A0-A2=GND (0x20); GPA7/GPB7 NC: output-only on MCP23017.",
           292.1, 289.56, size=1.0)
    connector(16, "ENCODERS / panel harness", 431.8, 265.43,
              ["ENC1_A", "ENC1_B", "ENC1_SW", "ENC2_A", "ENC2_B",
               "ENC2_SW", "ENC3_A", "ENC3_B", "ENC3_SW",
               "ENC4_A", "ENC4_B", "ENC4_SW", "ENC5_A", "ENC5_B",
               "ENC5_SW", "GND"],
              note="Five encoder A/B/switch contacts; use MCP weak pullups and debounce.")

    # ------------------------------------------------------------------
    # Direct MCU switch inputs: play/stop/instrument plus ENC5 switch.
    # ------------------------------------------------------------------
    b.text("DIRECT BUTTON INPUTS / 10k + 100n RC", 45.72, 316.23, size=1.52)
    for i, (net, label) in enumerate((
        ("BTN_PLAY", "PLAY"), ("BTN_STOP", "STOP"),
        ("BTN_INST", "INSTRUMENT"), ("ENC5_SW", "ENC5 PUSH"),
    )):
        x = 63.5 + i * 83.82
        b.text(label, x, 334.01, size=1.0)
        resistor("10k", x, 347.98, "+3V3", net)
        capacitor("100n", x + 20.32, 347.98, net, "GND")
        if i < 3:
            switch_ref = ref("SW")
            b.place("Switch:SW_Push", switch_ref, "SW_Push", x + 43.18, 347.98,
                    ref_at=(x + 43.18, 341.63),
                    value_at=(x + 43.18, 355.6))
            b.pin_net(switch_ref, 1, net, stub=10.16)
            b.pin_net(switch_ref, 2, "GND", stub=2.54)
        else:
            b.text("Switch on encoder harness pin 15", x + 34.29, 348.0, size=1.0)

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------
    notes = [
        "v2 rails: +5V (LED, TFT, MIDI), +5V_BP (Black Pill via D4), +5VA (VS1053, PCM5102A, mixer via FB1), +3V3 (Black Pill LDO, logic).",
        "Budget: system about 0.45 A typical, about 1.1 A max with NeoTrellis capped at 0.5 A. Use a 5V 3A USB-C adapter.",
        "Firmware remap: USART1 PA15/PB3 (G474 link), PB5 = 5V_SENSE input, DREQ PA1, XRST PB4, I2C1 PB8/PB9; I2S is no longer on the F411.",
        "Reference: BassMate MainSchematic.pdf identifies the Black Pill and VS1053 modules; wiring follows this project's pinmap.",
    ]
    for i, line in enumerate(notes):
        b.text(line, 45.72, 377.19 + i * 3.81, size=1.0)

    b.check_all()
    return b


def main():
    global STOCK
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", nargs="*")
    parser.add_argument("--stock", type=Path, help="KiCad symbol folder")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if args.stock is not None:
        STOCK = args.stock
    if args.inspect is not None:
        inspect(args.inspect)
        return
    builder = build_circuit()
    args.output.write_text(
        builder.render(title="Synthesizer v2 - F411 controller + VS1053 + G474 synth link"),
        encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
