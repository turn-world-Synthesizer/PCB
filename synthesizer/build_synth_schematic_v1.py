"""Build the STM32F411 Black Pill synthesizer schematic.

The schematic is generated from the repository pin map and from project-local
module symbols.  Connector footprints for off-board modules remain unassigned
until the physical modules are measured.
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


def extract_symbol(lib_id: str) -> str:
    library, name = lib_id.split(":", 1)
    path = HERE / f"{library}.kicad_sym" if library == "Synthesizer_Modules" else STOCK / f"{library}.kicad_sym"
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


def property_defs(source: str) -> dict[str, tuple[str, float, float]]:
    tree = parse_sexpr(source)
    result = {}
    for prop in children(tree, "property"):
        at = one_child(prop, "at")
        result[str(prop[1])] = (str(prop[2]), float(at[1]), float(at[2]))
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
        def prop_at(name):
            _, px, py = defaults.get(name, ("", 0, 0))
            radians = math.radians(rotation)
            return (x + px * math.cos(radians) - py * math.sin(radians),
                    y - px * math.sin(radians) - py * math.cos(radians))
        refx, refy = ref_at or prop_at("Reference")
        valx, valy = value_at or prop_at("Value")
        if footprint is None:
            footprint = defaults.get("Footprint", ("", 0, 0))[0]
        pins = self.pinmaps[lib_id]
        parts = [
            "(symbol", f"(lib_id {q(lib_id)})", f"(at {n(x)} {n(y)} {rotation})",
            "(unit 1)", "(exclude_from_sim no)", "(in_bom yes)",
            f"(on_board {'yes' if on_board else 'no'})", "(dnp no)",
            f"(uuid {q(self.uid())})",
            self.property("Reference", ref, refx, refy),
            self.property("Value", value, valx, valy),
            self.property("Footprint", footprint, x, y, hidden=True),
            self.property("Datasheet", defaults.get("Datasheet", ("", 0, 0))[0], x, y, hidden=True),
        ]
        parts.extend(f'(pin {q(number)} (uuid {q(self.uid())}))' for number in pins)
        parts.append(
            f'(instances (project {q(PROJECT)} (path "/{SHEET_UUID}"'
            f' (reference {q(ref)}) (unit 1))))'
        )
        parts.append(")")
        self.items.append("\n".join(parts))

    def property(self, name, value, x, y, *, hidden=False):
        return (f'(property {q(name)} {q(value)} (at {n(x)} {n(y)} 0) '
                f'(effects (font (size 1.27 1.27)){" (hide yes)" if hidden else ""}))')

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
            f'(title_block (title {q(title)}) (rev "B") '
            '(comment 1 "Module carrier schematic; verify module revisions and footprints before PCB fabrication"))',
            "(lib_symbols\n" + "\n".join(self.cache.values()) + "\n)",
        ]
        items.extend(self.items)
        items.extend(['(sheet_instances (path "/" (page "1")))', '(embedded_fonts no)', ')'])
        return "\n\t".join(items) + "\n"


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

    def resistor(value: str, x: float, y: float, net1: str, net2: str,
                 *, footprint: str | None = None) -> str:
        name = ref("R")
        b.place("Device:R", name, value, x, y, footprint=footprint)
        b.pin_net(name, 1, net1, stub=2.54)
        b.pin_net(name, 2, net2, stub=2.54)
        return name

    def capacitor(value: str, x: float, y: float, net1: str, net2: str) -> str:
        name = ref("C")
        b.place("Device:C", name, value, x, y)
        b.pin_net(name, 1, net1, stub=2.54)
        b.pin_net(name, 2, net2, stub=2.54)
        return name

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

    # The module symbol uses real physical pad numbers.  The main-board MCU
    # circuitry (regulator, USB, clock, SWD) is already on the Black Pill.
    b.place("Synthesizer_Modules:BlackPill_F411CEU6", "U1",
            "WeAct Black Pill F411CEU6", 241.3, 124.46,
            ref_at=(241.3, 90.17), value_at=(241.3, 157.48))
    blackpill = {
        1: "LCD_CS", 2: "LCD_SCK", 3: "MCP_INT", 4: "LCD_MOSI",
        5: "LCD_LED", 6: "BTN_INST", 7: "ENC5_SW",
        10: "I2S_LRCK", 11: "I2S_BCK", 12: "VS_XRST",
        13: "I2S_DIN", 14: "VS_XCS", 15: "VS_XDCS",
        16: "I2C_SCL", 17: "I2C_SDA", 18: "+5V",
        19: "GND", 20: "+3V3", 40: "+5V", 39: "GND",
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
    b.text("BLACK PILL MODULE / pin map from PCB/pinmap.md", 197.485, 73.66, size=1.52)
    b.text("USB, HSE and SWD are on the module. PA11/PA12 are unused on carrier.",
           194.945, 172.72, size=1.0)

    # Adafruit breakout: pin numbers match the physical JP2 (1-16) and JP3
    # (17-32) header pads, rather than the numbering in the reference PDF.
    b.place("Synthesizer_Modules:Adafruit_VS1053_Breakout_V2", "U2",
            "Adafruit VS1053 breakout v2", 411.48, 124.46,
            ref_at=(411.48, 97.79), value_at=(411.48, 154.94))
    vs = {
        1: "AUDIO_R", 2: "AUDIO_L", 3: "GND", 4: "GND",
        5: "GND", 6: "VS_DREQ", 7: "+5V", 9: "GND",
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
    b.text("VS1053: VIN=+5V; its 3V3 pins are regulator outputs (NC here).",
           365.76, 169.545, size=1.0)
    b.text("U2 physical header pin order follows Adafruit v2 JP2/JP3; verify actual board.",
           365.76, 174.625, size=1.0)

    # USB-powered carrier rails.  PWR_FLAG marks the Black Pill as the source
    # for ERC; do not add external 5 V without a separate backfeed design.
    b.text("POWER / USB VBUS from Black Pill", 45.72, 50.8, size=1.52)
    for name, rail, x in (("#FLG01", "+5V", 58.42),
                          ("#FLG02", "+3V3", 83.82),
                          ("#FLG03", "GND", 109.22)):
        b.place("power:PWR_FLAG", name, "PWR_FLAG", x, 66.04)
        b.label(rail, x, 66.04)
    capacitor("10u", 50.8, 91.44, "+5V", "GND")
    capacitor("10u", 76.2, 91.44, "+3V3", "GND")
    capacitor("100n", 101.6, 91.44, "+3V3", "GND")
    resistor("1k5", 127.0, 86.36, "+3V3", "PWR_LED_A")
    b.place("Device:LED", "D1", "RED", 127.0, 104.14,
            ref_at=(133.35, 101.6), value_at=(133.35, 106.68))
    b.pin_net("D1", 2, "PWR_LED_A", stub=2.54)
    b.pin_net("D1", 1, "GND", stub=2.54)
    b.text("+3V3 module regulator output: 300mA budget; keep NeoTrellis dim.",
           45.72, 119.38, size=1.0)
    b.text("External +5V supply requires USB backfeed protection.",
           45.72, 124.46, size=1.0)

    # Audio from the breakout's AC-coupled v2 line outputs.  Pin order is a
    # logical cable interface, with connector footprint left for user choice.
    b.text("AUDIO OUTPUT", 475.615, 68.58, size=1.52)
    connector(3, "STEREO LINE OUT", 487.68, 83.82,
              ["AUDIO_L", "AUDIO_R", "GND"],
              note="Line output; use powered speakers or an external amplifier.")

    # Other planned modules are logical cable interfaces.  Their footprints
    # are deliberately unset because the purchased TFT/PCM/Neo boards differ.
    b.text("DISPLAY / AUDIO / I2C MODULE INTERFACES", 476.25, 124.46, size=1.52)
    connector(8, "TFT SPI interface", 487.68, 147.32,
              ["+3V3", "GND", "LCD_SCK", "LCD_MOSI", "LCD_CS",
               "LCD_DC", "LCD_RST", "LCD_LED"],
              note="TFT physical 14-pin order and backlight current: verify module.")
    connector(6, "PCM5102A interface", 487.68, 201.93,
              ["+3V3", "GND", "I2S_BCK", "I2S_DIN", "I2S_LRCK", "GND"],
              note="Pin 6 is module SCK/MCLK to GND; VIN needs module check.")
    connector(5, "NeoTrellis interface", 487.68, 246.38,
              ["+3V3", "GND", "I2C_SCL", "I2C_SDA", "TRELLIS_INT"],
              note="Use limited LED brightness until 3V3 current budget is measured.")

    # MIDI IN: opto-isolated current loop.  DIN pin 2 is isolated from GND.
    b.text("MIDI IN / 31.25 kbit/s", 45.72, 142.24, size=1.52)
    b.place("Connector:DIN-5_180degree", "J1", "MIDI IN", 71.12, 175.26,
            ref_at=(71.12, 155.575), value_at=(71.12, 195.58))
    for pin in (1, 2, 3):
        b.nc("J1", pin)
    b.pin_net("J1", 4, "MIDI_IN_4", stub=7.62)
    b.pin_net("J1", 5, "MIDI_LED_K", stub=7.62)
    resistor("220", 116.84, 168.91, "MIDI_IN_4", "MIDI_LED_A")
    b.place("Device:D", "D2", "1N4148", 139.7, 156.21)
    b.pin_net("D2", 1, "MIDI_LED_A", stub=2.54)
    b.pin_net("D2", 2, "MIDI_LED_K", stub=2.54)
    b.place("Isolator:6N138", "U3", "6N138", 161.29, 174.625,
            ref_at=(161.29, 164.465), value_at=(161.29, 187.325))
    for pin, net in {2: "MIDI_LED_A", 3: "MIDI_LED_K", 5: "GND",
                     6: "MIDI_RX", 8: "+5V"}.items():
        b.pin_net("U3", pin, net, stub=5.08)
    for pin in (1, 4, 7):
        b.nc("U3", pin)
    b.check_complete("U3")
    resistor("4k7", 186.69, 174.625, "+3V3", "MIDI_RX")
    capacitor("100n", 203.2, 174.625, "+5V", "GND")
    b.text("DIN IN pin 2/shield has no DC ground connection.",
           45.72, 205.74, size=1.0)

    # MIDI OUT: 5 V legacy current loop with a two-transistor non-inverting
    # stage.  PA2 never sinks the MIDI cable current directly.
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
        b.pin_net("Q2", pin, net, stub=5.08)
    b.place("Transistor_BJT:Q_NPN_BEC", "Q1", "MMBT3904", 161.29, 260.35,
            footprint="Package_TO_SOT_SMD:SOT-23",
            ref_at=(153.67, 255.27), value_at=(153.67, 265.43))
    for pin, net in {1: "MOUT_BASE", 2: "GND", 3: "MOUT_GATE"}.items():
        b.pin_net("Q1", pin, net, stub=5.08)
    resistor("10k", 213.36, 245.11, "+3V3", "MOUT_GATE")
    resistor("4k7", 231.14, 245.11, "MIDI_TX", "MOUT_BASE")
    resistor("47k", 248.92, 245.11, "+3V3", "MOUT_BASE")
    b.text("J2 pins 4/5 use 220R each; Q2 sinks cable current, Q1 restores UART polarity.",
           45.72, 292.1, size=1.0)

    # I2C1 pullups, MCP23017 and the encoder harness.
    b.text("I2C1 / MCP23017 / ENCODERS", 290.83, 192.405, size=1.52)
    resistor("4k7", 302.26, 208.28, "+3V3", "I2C_SCL")
    resistor("4k7", 317.5, 208.28, "+3V3", "I2C_SDA")
    b.place("Interface_Expansion:MCP23017x-x-SO", "U4", "MCP23017", 359.41, 249.555,
            ref_at=(359.41, 220.345), value_at=(359.41, 279.4))
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

    # Four direct MCU switch inputs: play/stop/instrument plus ENC5 switch.
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
            b.pin_net(switch_ref, 1, net, stub=2.54)
            b.pin_net(switch_ref, 2, "GND", stub=2.54)
        else:
            b.text("Switch on J7 pin 15", x + 34.29, 348.0, size=1.0)

    b.text("Reference: MainSchematic.pdf identifies these Black Pill and VS1053 modules; wiring follows this project's pinmap.",
           45.72, 389.89, size=1.0)
    b.text("Firmware remap required: DREQ PA0->PA1, XRST PB5->PB4, I2C2->I2C1; MIDI RX and I2S are not active yet.",
           45.72, 394.97, size=1.0)
    return b


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", nargs="*")
    args = parser.parse_args()
    if args.inspect is not None:
        inspect(args.inspect)
        return
    builder = build_circuit()
    OUTPUT.write_text(builder.render(title="Synthesizer v1 - STM32F411 Black Pill + VS1053"),
                      encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
