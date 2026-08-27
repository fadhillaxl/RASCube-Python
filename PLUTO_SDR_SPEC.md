# RASCube PlutoSDR Ground Station Specification

Spesifikasi teknis implementasi Ground Station penerima telemetri satelit RASCube berbasis **Software Defined Radio (SDR)** menggunakan **ADALM-PLUTO / Pluto+ SDR (AD9363)**.

---

## 1. Arsitektur Sistem

Ground station ini menggantikan peran hardware receiver dongle (SX1262 microcontroller) dengan **SDR berbasis software DSP murni** atau **GNU Radio flowgraph**, menangkap sinyal radio frekuensi (RF) mentah langsung dari antena dan mendemodulasi modulasi LoRa CSS (Chirp Spread Spectrum) secara real-time.

```
                    ┌─────────────────────────┐
                    │ RASCube Satellite #1581 │
                    │   (RF TX @ 925.0 MHz)   │
                    └────────────┬────────────┘
                                 │ Over-The-Air (LoRa CSS)
                                 ▼
                    ┌─────────────────────────┐
                    │  Antena RX (915 MHz)    │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │ ADALM-PLUTO / Pluto+    │
                    │   AD9363 RF Frontend    │
                    │  LO=925 MHz, Fs=1 MSPS  │
                    └────────────┬────────────┘
                                 │ USB CDC / libiio (Raw I/Q Samples)
                                 ▼
      ┌──────────────────────────────────────────────────────┐
      │             Python Ground Station Engine             │
      │  1. I/Q Normalization (/ 2048.0)                     │
      │  2. Vector FFT Dechirping (BW=500k, SF=7)            │
      │  3. Preamble & Sync Word (0x12) Detection            │
      │  4. Diagonal Deinterleaving & Hamming(5,4) FEC       │
      │  5. SX126x Dewhitening Pipeline                      │
      │  6. Telemetry Parser (Main Telemetry Port 0x10)      │
      └──────────────────────────┬───────────────────────────┘
                                 │
                                 ▼
                  Live Telemetry Metrics / Raw Hex
              (Uptime, Lat, Lon, Voltages, Attitude)
```

---

## 2. Parameter Fisik & Modulasi RF (Physical Layer)

| Parameter | Nilai / Spesifikasi | Keterangan |
|---|---|---|
| **Frekuensi Tengah ($f_c$)** | `925.000 MHz` | Dihitung dari formula: $916.0 + (\text{Serial} \pmod{18}) \times 0.6\text{ MHz}$ (Channel 15) |
| **Rentang Saluran RF** | `916.000 MHz` – `926.200 MHz` | 18 Saluran (Channel 0 s/d 17, spasi 600 kHz) |
| **Modulasi** | LoRa (Chirp Spread Spectrum - CSS) | Chirp linier naik/turun |
| **Bandwidth ($\text{BW}$)** | **`500.0 kHz`** (`500_000 Hz`) | Index 9 pada tabel `BANDWIDTH_KHZ` RASCube |
| **Spreading Factor ($\text{SF}$)** | **`SF7`** ($2^7 = 128$ chips/symbol) | Standard data rate satelit |
| **Durasi Simbol ($T_s$)** | **`256 µs`** ($0.256\text{ ms}$) | $T_s = \frac{2^{\text{SF}}}{\text{BW}} = \frac{128}{500000} = 0.000256\text{ s}$ |
| **Laju Simbol ($R_s$)** | `3,906.25 symbols/sec` | $R_s = \frac{\text{BW}}{2^{\text{SF}}}$ |
| **Coding Rate ($\text{CR}$)** | **`4/5`** ($\text{CR}=1$) | Hamming $(5, 4)$ Forward Error Correction |
| **Sync Word** | **`0x12`** (`18` desimal) | SX1262 / SX1276 Private Network standard |
| **Preamble** | `8 Simbol Unmodulated Upchirp` | Preamble deteksi sinkronisasi awal |
| **Start Frame Delimiter (SFD)** | `2.25 Simbol Downchirp` | Tanda akhir preamble dan awal header |
| **Header Mode** | Explicit Header | Header LoRa berisi panjang payload & CRC flag |
| **Payload CRC** | Enabled (2 bytes CRC16-CCITT) | Verifikasi integritas payload |

---

## 3. Konfigurasi Hardware ADALM-PLUTO

| Konfigurasi SDR | Nilai Rekomendasi | Alasan / Catatan |
|---|---|---|
| **Sample Rate ($F_s$)** | `1,000,000 SPS` (1 MSPS) | Menghasilkan tepat **256 sampel per simbol** ($O_{\text{factor}} = 2$). |
| **RF Bandwidth Filter** | `1,000,000 Hz` (1 MHz) | Menjamin seluruh spektrum $500\text{ kHz}$ lolos filter analog tanpa distorsi tepi. |
| **Gain Control Mode** | `manual` | Menghindari fluktuasi gain AGC saat tidak ada transmisi sinyal. |
| **Hardware Gain** | **`35.0 dB` – `42.0 dB`** | **Kritis:** Gain $>50\text{ dB}$ menyebabkan saturasi ADC 12-bit ($\pm 2047$ clipping). |
| **Buffer Size** | `16384` atau `65536` sampel | $16384$ sampel $= 16.384\text{ ms}$ buffer, memberikan latensi sangat rendah. |
| **Normalisasi I/Q** | $\text{IQ}_{\text{norm}} = \frac{\text{ADC}_{\text{raw}}}{2048.0}$ | Mengubah rentang integer 12-bit ADC ke float kompleks $[-1.0, +1.0]$. |

---

## 4. Struktur Paket Telemetri Satelit (Port 0x10)

Downlink telemetri utama dikirim dalam format frame USB/RF:
- **Byte 0**: `0x10` (Inbound Port: `MAIN_TELEMETRY`)
- **Byte 1**: `0x79` (Panjang payload: 121 bytes)
- **Byte 2..122**: 121 bytes payload terkompresi LE (Little-Endian)

### Layout Byte Payload 121-Byte:

| Offset | Ukuran | Tipe | Unit | Deskripsi Data |
|---|---|---|---|---|
| `0` | 4 | `u32LE` | count | Nomor urut paket (Packet sequence counter) |
| `4` | 2 | `u16LE` | mV | Tegangan rel 5V utama (Main 5V rail) |
| `6` | 2 | `u16LE` | mV | Tegangan rel 3.3V utama (Main 3.3V rail) |
| `8` | 2 | `u16LE` | raw | Sensor LDR solar cell channel 1 |
| `10` | 2 | `u16LE` | raw | Sensor LDR solar cell channel 2 |
| `12` | 2 | `u16LE` | raw | Sensor LDR solar cell channel 3 |
| `14` | 2 | `u16LE` | mV | Tegangan charger baterai |
| `16` | 2 | `i16LE` | mA | Arus pengisian baterai (Battery charging current) |
| `18` | 2 | `i16LE` | mV | Tegangan bus USB |
| `20` | 2 | `i16LE` | mA | Arus bus USB |
| `22` | 2 | `i16LE` | mV | Tegangan output baterai |
| `24` | 2 | `i16LE` | mA | Arus beban baterai |
| `26` | 2 | `i16LE` | mV | Tegangan solar input panel 1 |
| `28` | 2 | `i16LE` | mA | Arus solar input panel 1 |
| `30` | 2 | `i16LE` | mV | Tegangan solar input panel 2 |
| `32` | 2 | `i16LE` | mA | Arus solar input panel 2 |
| `34` | 2 | `i16LE` | mV | Tegangan solar input panel 3 |
| `36` | 2 | `i16LE` | mA | Arus solar input panel 3 |
| `38` | 1 | `u8` | flag | Status baterai penuh (Charging complete flag) |
| `39` | 1 | `u8` | flag | Status power charger baik (Power good flag) |
| `40` | 4 | `u32LE` | ms | **OBC Uptime** (Waktu aktif satelit sejak boot) |
| `44` | 6 | `3×i16LE` | raw | IMU Magnetometer X, Y, Z |
| `50` | 2 | `i16LE` | 0.1 °C | Barometer Temperatur |
| `52` | 4 | `float32` | 0.01 hPa| Barometer Tekanan Atmosfer |
| `56` | 4 | `float32` | meter | Barometer Ketinggian Relatif |
| `60` | 6 | `3×i16LE` | raw | IMU Akselerometer X, Y, Z ($1\text{ LSB} = 0.000061\text{ g}$) |
| `66` | 6 | `3×i16LE` | raw | IMU Giroskop X, Y, Z ($1\text{ LSB} = 0.0175\text{ dps}$) |
| `72` | 4 | `float32` | derajat | **GPS Latitude** (Lintang) |
| `76` | 4 | `float32` | derajat | **GPS Longitude** (Bujur) |
| `80` | 4 | `float32` | meter | GPS Altitude (Ketinggian dari permukaan laut) |
| `84` | 4 | `float32` | km/jam | GPS Speed (Kecepatan horizontal) |
| `88` | 4 | `float32` | derajat | GPS Course (Arah orientasi kompas) |
| `92` | 4 | `float32` | unitless| GPS HDOP (Horizontal Dilution of Precision) |
| `96` | 1 | `u8` | count | Jumlah satelit GPS terkunci (Satellites in view) |
| `97` | 1 | `u8` | bool | Status GPS Lock (3D Fix = 1, No Fix = 0) |
| `98` | 12 | `3×float32`| derajat | Orientasi Attitude Euler (Roll, Pitch, Yaw) |
| `110`| 2 | `u16LE` | code | Error code sistem OBC |
| `112`| 1 | `u8` | version | Versi firmware STM32 OBC |
| `113`| 4 | `float32` | dBm | Radio RSSI sinyal |
| `117`| 4 | `float32` | dB | Radio SNR sinyal |

---

## 5. Komponen Software & Cara Penggunaan

### 1. `examples/sdr/pluto_direct_receiver.py` (Native Real-Time SDR Receiver)
Demodulator real-time native Python murni. Sangat responsif, latensi rendah, tanpa dependensi GNU Radio subprocess.

```bash
# Output format metrics: Uptime, Latitude, Longitude
python3 examples/sdr/pluto_direct_receiver.py --sat 1581 --gain 40

# Output format raw HEX paket (1079...):
python3 examples/sdr/pluto_direct_receiver.py --sat 1581 --gain 40 --hex
```

### 2. `examples/sdr/pluto_live_e2e_test.py` (Automated E2E Ground Station Test)
Menjalankan simulasi ground station lengkap:
1. Mengirim beacon wake-up via USB Dongle di background thread.
2. Membuka PlutoSDR dan mendemodulasi downlink RF satelit secara langsung.

```bash
python3 examples/sdr/pluto_live_e2e_test.py
```

### 3. `src/rascube_v2/sdr/pluto.py` (`PlutoSDRReceiver` Class)
Modul SDK Python untuk integrasi aplikasi ground station custom:

```python
from rascube_v2.sdr.pluto import PlutoSDRReceiver, SDRLoRaConfig

config = SDRLoRaConfig(
    serial_number=1581,
    rx_gain_db=40.0,
    bandwidth_hz=500_000,
    spreading_factor=7,
)

def on_telemetry(sample):
    print(f"Uptime: {sample.device_uptime_ms} ms | Lat: {sample.gps.latitude} | Lon: {sample.gps.longitude}")

receiver = PlutoSDRReceiver(config=config, on_sample=on_telemetry)
receiver.start_direct_sdr()
```

---

## 6. Spesifikasi Transmisi Uplink (Command & Control)

PlutoSDR mendukung transmisi uplink radio langsung ke satelit RASCube melalui port antena TX:

### Protokol Framing Uplink (Radio Frame Format)
Setiap perintah uplink dikemas dalam format 1-byte Port Header + 1-byte Panjang Payload + N-byte Payload:
$$\text{Frame} = [\text{HostPort},\ \text{Length},\ \text{Payload}\dots]$$

### Perintah Uplink yang Didukung:

| Perintah / Aksi | HostPort (Hex) | Payload Format | Contoh Frame (Hex) | Deskripsi |
|---|---|---|---|---|
| **OBC Info / Wake-up Ping** | `0x12` | `0x00` (1 byte) | `12 01 00` | Membangunkan satelit dan memicu transmisi telemetri downlink |
| **Set Arduino RGB LED** | `0x80` | `[R, G, B]` (3 bytes) | `80 03 FF 00 00` | Mengatur warna LED RGB satelit (Merah: `FF 00 00`, Hijau: `00 FF 00`) |
| **Buzzer Startup Song** | `0x84` | `0x00` (1 byte) | `84 01 00` | Memainkan lagu startup buzzer satelit over-the-air |
| **Buzzer Tone Custom** | `0x81` | `u16LE` frekuensi (Hz) | `81 02 E8 03` | Membunyikan buzzer pada frekuensi 1000 Hz |
| **OBC Flash Settings** | `0x16` | `u8` flag | `16 01 01` | Mengatur konfigurasi flash memory OBC |
| **Camera Capture Trigger** | `0x13` | `0x00` (1 byte) | `13 01 00` | Memicu pengambilan gambar kamera onboard |

---

## 7. Penggunaan CLI Uplink Transmitter (`pluto_transmitter.py`)

Gunakan tool [`examples/sdr/pluto_transmitter.py`](file:///Users/mm/GitHub/RASCube-Python/examples/sdr/pluto_transmitter.py) untuk mengirim perintah uplink via PlutoSDR TX:

```bash
# 1. Kirim Wake-up Ping ke Sat #1581 (925.0 MHz)
python3 examples/sdr/pluto_transmitter.py --sat 1581 --ping

# 2. Nyalakan LED RGB Hijau di satelit
python3 examples/sdr/pluto_transmitter.py --sat 1581 --rgb 0 255 0

# 3. Mainkan lagu startup buzzer di satelit
python3 examples/sdr/pluto_transmitter.py --sat 1581 --song

# 4. Mode Beacon Berkelanjutan (Kirim wake-up ping otomatis setiap 5 detik)
python3 examples/sdr/pluto_transmitter.py --sat 1581 --beacon 5

# 5. Kirim Raw Hex Frame kustom
python3 examples/sdr/pluto_transmitter.py --sat 1581 --raw-hex 120100
```

### Python SDK Uplink Example:
```python
from rascube_v2.constants import HostPort
from rascube_v2.sdr import PlutoSDRTransmitter, SDRLoRaConfig

config = SDRLoRaConfig(serial_number=1581, bandwidth_hz=500_000, spreading_factor=7)
tx = PlutoSDRTransmitter(config=config, tx_gain_db=0.0)

# Kirim perintah RGB LED Merah (Port 0x80, len 3, [255, 0, 0])
tx.transmit_bytes(bytes([HostPort.ARDUINO_RGB, 0x03, 255, 0, 0]))
```

