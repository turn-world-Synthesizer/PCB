# 신디사이저 v1 핀 배치표

기준 보드: WeAct Black Pill STM32F411CEU6 (2026-09-29)

회로도에서 넷 라벨 이름은 아래 표의 **Net** 열을 그대로 씁니다.

## Black Pill 헤더

회로도에는 Black Pill 대신 1x20 암 핀헤더 2개(J1, J2)를 놓고 핀마다 넷 라벨을 붙입니다.
PCB 배치 단계에서 실물 보드를 보고 헤더 방향(1번 핀 위치)을 다시 확인합니다.

### J1 (5V ~ VBAT 줄)

| 핀 | Black Pill | Net | 용도 |
|---|---|---|---|
| 1 | 5V | +5V | 전원 |
| 2 | GND | GND | |
| 3 | 3.3V | +3V3 | Black Pill 레귤레이터 출력 (최대 300mA) |
| 4 | PB10 | BTN_STOP | Stop 버튼 |
| 5 | PB2 | (연결 안 함) | BOOT1 핀이라 사용 안 함 |
| 6 | PB1 | VS_SDCS | VS1053 모듈 SD 카드 CS |
| 7 | PB0 | LCD_DC | TFT 데이터/명령 선택 |
| 8 | PA7 | VS_MOSI | SPI1 |
| 9 | PA6 | VS_MISO | SPI1 |
| 10 | PA5 | VS_SCK | SPI1 |
| 11 | PA4 | TRELLIS_INT | NeoTrellis 인터럽트 |
| 12 | PA3 | MIDI_RX | USART2 RX, MIDI IN |
| 13 | PA2 | MIDI_TX | USART2 TX, MIDI OUT |
| 14 | PA1 | VS_DREQ | VS1053 DREQ |
| 15 | PA0 | BTN_PLAY | Play 버튼 (Black Pill의 KEY 버튼과 같은 핀) |
| 16 | NRST | LCD_RST | TFT 리셋을 MCU 리셋에 연결 |
| 17 | PC15 | (연결 안 함) | 32.768kHz 크리스탈 |
| 18 | PC14 | (연결 안 함) | 32.768kHz 크리스탈 |
| 19 | PC13 | (연결 안 함) | 보드 LED |
| 20 | VBAT | (연결 안 함) | |

### J2 (PB12 ~ 3.3V 줄)

| 핀 | Black Pill | Net | 용도 |
|---|---|---|---|
| 1 | PB12 | LCD_CS | TFT CS |
| 2 | PB13 | LCD_SCK | SPI2 |
| 3 | PB14 | MCP_INT | MCP23017 인터럽트 |
| 4 | PB15 | LCD_MOSI | SPI2 |
| 5 | PA8 | LCD_LED | TFT 백라이트 밝기 (TIM1_CH1 PWM) |
| 6 | PA9 | BTN_INST | Instrument Select 버튼 |
| 7 | PA10 | ENC5_SW | Filter 엔코더 누름 스위치 |
| 8 | PA11 | (연결 안 함) | USB D- |
| 9 | PA12 | (연결 안 함) | USB D+ |
| 10 | PA15 | I2S_LRCK | I2S3 WS, PCM5102A |
| 11 | PB3 | I2S_BCK | I2S3 CK, PCM5102A |
| 12 | PB4 | VS_XRST | VS1053 리셋 |
| 13 | PB5 | I2S_DIN | I2S3 SD, PCM5102A |
| 14 | PB6 | VS_XCS | VS1053 명령 CS |
| 15 | PB7 | VS_XDCS | VS1053 데이터 CS |
| 16 | PB8 | I2C_SCL | I2C1 |
| 17 | PB9 | I2C_SDA | I2C1 |
| 18 | 5V | +5V | |
| 19 | GND | GND | |
| 20 | 3.3V | +3V3 | |

남는 핀은 PB2와 PC13뿐입니다. 둘 다 쓰기에 제약이 있는 핀입니다.

## MCP23017 (I2C 주소 0x20, A0~A2 = GND)

2022년 데이터시트 개정 이후 GPA7과 GPB7은 **출력 전용**이라 입력으로 쓰면 안 됩니다.
그래서 입력은 14개까지만 쓰고, 다섯 번째 엔코더의 누름 스위치는 MCU의 PA10에 연결합니다.

| MCP 핀 | Net | 용도 |
|---|---|---|
| GPA0 | ENC1_A | BPM |
| GPA1 | ENC1_B | BPM |
| GPA2 | ENC2_A | Volume |
| GPA3 | ENC2_B | Volume |
| GPA4 | ENC3_A | Pitch |
| GPA5 | ENC3_B | Pitch |
| GPA6 | ENC1_SW | BPM 누름 |
| GPA7 | (연결 안 함) | 출력 전용 |
| GPB0 | ENC4_A | Decay |
| GPB1 | ENC4_B | Decay |
| GPB2 | ENC5_A | Filter |
| GPB3 | ENC5_B | Filter |
| GPB4 | ENC2_SW | Volume 누름 |
| GPB5 | ENC3_SW | Pitch 누름 |
| GPB6 | ENC4_SW | Decay 누름 |
| GPB7 | (연결 안 함) | 출력 전용 |

## 모듈 연결 요약

| 모듈 | 연결 |
|---|---|
| VS1053 (Adafruit) | VS_SCK, VS_MISO, VS_MOSI, VS_XCS, VS_XDCS, VS_XRST, VS_DREQ, VS_SDCS |
| PCM5102A 모듈 | BCK ← I2S_BCK, DIN ← I2S_DIN, LCK ← I2S_LRCK, SCK → GND (내부 PLL 사용) |
| TFT 14핀 (ILI9341 / ILI9488) | LCD_SCK, LCD_MOSI, LCD_CS, LCD_DC, LCD_RST, LCD_LED. SDO(MISO)와 터치 핀은 연결 안 함 |
| NeoTrellis 4x4 | I2C_SCL, I2C_SDA, TRELLIS_INT |
| MCP23017 | I2C_SCL, I2C_SDA, MCP_INT |
| MIDI | MIDI_TX (OUT), MIDI_RX (IN, 6N138 경유) |

## 펌웨어에서 바뀌는 핀 (stm32f411_vs1053 README 대비)

| 기능 | 현재 | v1 보드 |
|---|---|---|
| VS1053 DREQ | PA0 | PA1 |
| VS1053 XRST | PB5 | PB4 |
| I2C | I2C2 (PB10 SCL, PB3 SDA) | I2C1 (PB8 SCL, PB9 SDA) |

SPI1, VS1053 XCS/XDCS, USART2 MIDI는 그대로입니다.

## 설계 메모

- Black Pill의 5V 핀은 USB VBUS에 다이오드 없이 바로 연결되어 있다 (V2.0 기준). 외부 5V 전원을 넣을 때 역류 방지가 필요하다.
- ILI9488 SPI 모듈은 CS가 꺼져도 SDO가 버스를 놓지 않으므로 MISO를 연결하지 않는다.
- MCU에 바로 연결되는 버튼 입력(PA0, PA9, PA10, PB10)은 외부 10k 풀업과 100nF 디바운스 커패시터를 단다.
