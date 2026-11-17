"""Generate the Black Pill carrier schematic from PCB/pinmap.md.

This script is kept with the schematic so the 40-pin mapping is easy to audit.
Run it from any working directory with Python 3 on a KiCad 10 installation.
"""

from __future__ import annotations

import argparse
from itertools import count
import re
import uuid
from pathlib import Path


HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "synthesizer.kicad_sch"
LIBRARY = Path(r"C:\Program Files\KiCad\10.0\share\kicad\symbols\Connector.kicad_sym")
SHEET_UUID = "161bcf67-49cc-4d0d-8cc2-cd831a16933a"
SYMBOL_ID = "Connector:Conn_01x20_Socket"
FOOTPRINT = "Connector_PinSocket_2.54mm:PinSocket_1x20_P2.54mm_Vertical"
_IDS = count(1)

# (physical Black Pill pin, net name).  A None net gets an explicit NC marker.
J1 = [
    ("5V", "+5V"),
    ("GND", "GND"),
    ("3.3V", "+3V3"),
    ("PB10", "BTN_STOP"),
    ("PB2 / BOOT1", None),
    ("PB1", "VS_SDCS"),
    ("PB0", "LCD_DC"),
    ("PA7", "VS_MOSI"),
    ("PA6", "VS_MISO"),
    ("PA5", "VS_SCK"),
    ("PA4", "TRELLIS_INT"),
    ("PA3", "MIDI_RX"),
    ("PA2", "MIDI_TX"),
    ("PA1", "VS_DREQ"),
    ("PA0", "BTN_PLAY"),
    ("NRST", "LCD_RST"),
    ("PC15", None),
    ("PC14", None),
    ("PC13 / LED", None),
    ("VBAT", None),
]

J2 = [
    ("PB12", "LCD_CS"),
    ("PB13", "LCD_SCK"),
    ("PB14", "MCP_INT"),
    ("PB15", "LCD_MOSI"),
    ("PA8", "LCD_LED"),
    ("PA9", "BTN_INST"),
    ("PA10", "ENC5_SW"),
    ("PA11 / USB D-", None),
    ("PA12 / USB D+", None),
    ("PA15", "I2S_LRCK"),
    ("PB3", "I2S_BCK"),
    ("PB4", "VS_XRST"),
    ("PB5", "I2S_DIN"),
    ("PB6", "VS_XCS"),
    ("PB7", "VS_XDCS"),
    ("PB8", "I2C_SCL"),
    ("PB9", "I2C_SDA"),
    ("5V", "+5V"),
    ("GND", "GND"),
    ("3.3V", "+3V3"),
]


def uid() -> str:
    return str(uuid.uuid5(uuid.UUID(SHEET_UUID), f"black-pill-{next(_IDS)}"))


def q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def n(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def library_symbol() -> str:
    data = LIBRARY.read_text(encoding="utf-8")
    match = re.search(r'\n\t\(symbol "Conn_01x20_Socket"\s', data)
    if not match:
        raise RuntimeError(f"Cannot find {SYMBOL_ID} in {LIBRARY}")
    start = match.start() + 2
    depth = 0
    inside_string = False
    escaped = False
    end = None
    for i in range(start, len(data)):
        ch = data[i]
        if inside_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                inside_string = False
        elif ch == '"':
            inside_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise RuntimeError("Unbalanced KiCad symbol library")
    symbol = data[start:end]
    return symbol.replace('(symbol "Conn_01x20_Socket"', f'(symbol "{SYMBOL_ID}"', 1)


def property_text(name: str, value: str, x: float, y: float, *, hidden=False) -> str:
    effects = f'(effects (font (size 1.27 1.27)){" (hide yes)" if hidden else ""})'
    return f'(property {q(name)} {q(value)} (at {n(x)} {n(y)} 0) {effects})'


def connector(ref: str, cx: float, cy: float) -> str:
    items = [
        "(symbol",
        f"(lib_id {q(SYMBOL_ID)})",
        f"(at {n(cx)} {n(cy)} 0)",
        "(unit 1)",
        "(exclude_from_sim no)",
        "(in_bom yes)",
        "(on_board yes)",
        "(dnp no)",
        f'(uuid {q(uid())})',
        property_text("Reference", ref, cx, cy - 25.4),
        property_text("Value", "Black Pill socket", cx, cy + 27.94),
        property_text("Footprint", FOOTPRINT, cx, cy, hidden=True),
        property_text("Datasheet", "", cx, cy, hidden=True),
    ]
    items += [f'(pin {q(str(pin))} (uuid {q(uid())}))' for pin in range(1, 21)]
    items.append(
        f'(instances (project "synthesizer" (path "/{SHEET_UUID}"'
        f' (reference {q(ref)}) (unit 1))))'
    )
    items.append(")")
    return "\n\t".join(items)


def pin_y(cy: float, pin: int) -> float:
    # The first pin of the stock symbol is at library y=+22.86 mm.
    return cy - 22.86 + (pin - 1) * 2.54


def wire(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f'(wire (pts (xy {n(x1)} {n(y1)}) (xy {n(x2)} {n(y2)}))'
        f' (stroke (width 0) (type solid)) (uuid {q(uid())}))'
    )


def label(name: str, x: float, y: float) -> str:
    return (
        f'(label {q(name)} (at {n(x)} {n(y)} 0)'
        f' (effects (font (size 1.27 1.27)) (justify left bottom))'
        f' (uuid {q(uid())}))'
    )


def no_connect(x: float, y: float) -> str:
    return f'(no_connect (at {n(x)} {n(y)}) (uuid {q(uid())}))'


def text_item(message: str, x: float, y: float, *, size=1.0) -> str:
    return (
        f'(text {q(message)} (at {n(x)} {n(y)} 0)'
        f' (effects (font (size {n(size)} {n(size)})) (justify left))'
        f' (uuid {q(uid())}))'
    )


def build() -> str:
    global _IDS
    _IDS = count(1)
    if len(J1) != 20 or len(J2) != 20:
        raise AssertionError("Black Pill sockets must have 20 pins each")

    j1x, j2x, cy = 109.22, 190.5, 100.33
    pieces = [
        '(kicad_sch',
        '(version 20250114)',
        '(generator "eeschema")',
        '(generator_version "9.0")',
        f'(uuid "{SHEET_UUID}")',
        '(paper "A4")',
        '(title_block (title "Synthesizer v1 - STM32F411 Black Pill carrier")'
        ' (rev "A") (comment 1 "Pin nets follow PCB/pinmap.md"))',
        '(lib_symbols\n' + library_symbol() + '\n)',
        connector("J1", j1x, cy),
        connector("J2", j2x, cy),
        text_item("J1: 5V to VBAT row", 68.58, 61.0, size=1.27),
        text_item("J2: PB12 to 3.3V row", 151.13, 61.0, size=1.27),
    ]

    for cx, mapping in ((j1x, J1), (j2x, J2)):
        endpoint = cx - 5.08
        label_x = cx - 35.56
        physical_x = cx + 7.62
        for number, (physical, net) in enumerate(mapping, start=1):
            y = pin_y(cy, number)
            if net is None:
                pieces.append(no_connect(endpoint, y))
            else:
                pieces.append(wire(label_x, y, endpoint, y))
                pieces.append(label(net, label_x, y))
            pieces.append(text_item(physical, physical_x, y))

    pieces.extend(
        [
            text_item("WeAct Black Pill STM32F411CEU6 module", 74.93, 145.0, size=1.27),
            text_item("Onboard: 3.3V regulator, HSE, USB and SWD.", 74.93, 151.0),
            text_item("+3V3 is the module regulator output (300mA maximum per pinmap.md).", 74.93, 157.0),
            text_item("Check physical pin 1 orientation and row spacing before PCB placement.", 74.93, 163.0),
            '(sheet_instances (path "/" (page "1")))',
            '(embedded_fonts no)',
            ')',
        ]
    )
    return "\n\t".join(pieces) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    args.output.write_text(build(), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
