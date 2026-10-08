# 🚀 RASCube Ground Station (Client Web USB / Web Serial)

Ground Station RASCube menggunakan arsitektur **Client Web USB / Web Serial**.  
Dongle USB Receiver dihubungkan langsung ke laptop/komputer klien melalui browser (Chrome, Edge, Opera), sehingga **server backend / Docker tidak memerlukan passthrough perangkat USB atau izin privileged**.

---

## 1. Menjalankan Server API & Web Dashboard

### Opsi A: Local Python
```bash
python examples/api/server.py --port 8080
```
- Web Dashboard: **http://localhost:8080**
- Swagger UI Docs: **http://localhost:8080/docs**
- OpenAPI Schema: **http://localhost:8080/openapi.json**

### Opsi B: Docker Container
```bash
# Jalankan via Docker Compose
docker compose up -d

# Atau jalankan langsung via Docker CLI
docker build -t rascube-api:latest .
docker run -d --name rascube-groundstation -p 8080:8080 --restart unless-stopped rascube-api:latest
```
- Web Dashboard: **http://localhost:8080** (atau IP server Anda)
- Swagger UI Docs: **http://localhost:8080/docs**

---

## 2. Cara Menghubungkan Browser ke USB Dongle

1. Buka dashboard di browser (Google Chrome, Microsoft Edge, atau Opera).
2. Tancapkan **RASCube USB Receiver Dongle** ke port USB laptop Anda.
3. Masukkan **Target Satellite Serial Number** (default: `1581`).
4. Klik tombol **"🔌 Connect Browser USB"**.
5. Pilih perangkat USB (*STMicroelectronics Virtual COM Port / RASCube Receiver*), lalu klik **Connect**.
6. Browser akan membaca seluruh frame satelit secara real-time, menampilkan telemetri & foto kamera seketika, serta menyinkronkan data ke backend API.

---

## 3. Perintah Uplink Satelit (Langsung via Web Serial)

Perintah radio uplink dapat dikirim langsung dari browser ke satelit melalui Web Serial:

- **💡 Blink RGB LED**: Mengirim perintah aktivasi LED satelit (Port `0x80`).
- **🎵 Play Startup Song**: Mengirim perintah memutar nada startup buzzer satelit (Port `0x84`).
- **📡 Ping Satellite (OBC Info)**: Mengirim request info status onboard computer (Port `0x12`).
- **📸 Capture Satellite Photo**: Mengirim trigger kamera satelit (Port `0x13`), menerima blok gambar JPEG secara progresif (Port `0x15` / `0x20`), dan merakitnya secara real-time.

---

## 4. API Endpoints Reference

### Status Ground Station & Koneksi Klien
```bash
curl http://localhost:8080/api/status
```

### Telemetri Snapshot Terakhir
```bash
curl http://localhost:8080/api/telemetry/latest
```

### Riwayat Telemetri (History Buffer)
```bash
curl "http://localhost:8080/api/telemetry/history?limit=20"
```

### Real-time Server-Sent Events (SSE) Stream
```bash
curl -N http://localhost:8080/api/telemetry/stream
```

### Status & Foto Kamera Satelit Terakhir
```bash
# Status assembly kamera & progress blok
curl http://localhost:8080/api/camera/status

# JSON gambar terakhir (Base64)
curl http://localhost:8080/api/camera/latest

# Unduh file gambar JPEG langsung
curl http://localhost:8080/api/camera/latest.jpg -o satellite_photo.jpg
```

### Ingest Telemetri (Manual atau dari Skrip Klien)
```bash
curl -X POST http://localhost:8080/api/telemetry/ingest \
  -H "Content-Type: application/json" \
  -d '{"hex": "10796C3100008E13EC0C00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"}'
```

---

## 5. Akses Jarak Jauh (Remote IP / LAN) & Secure Context

Standar keamanan Chrome dan Edge mengharuskan **Secure Context** (HTTPS atau localhost) untuk menggunakan Web Serial API.

Jika Anda membuka dashboard melalui IP jaringan lokal (misal: `http://192.168.123.176:8080`):

### Pilihan 1: Aktifkan Flag Chrome (Cepat & Praktis)
1. Buka tab baru di browser: `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
2. Ubah opsi menjadi **Enabled**.
3. Masukkan origin URL server Anda: `http://192.168.123.176:8080`
4. Klik tombol **Relaunch** di kanan bawah.
5. Web Serial langsung aktif dan siap digunakan!

### Pilihan 2: Aktifkan Built-in HTTPS
Jalankan server dengan flag `--ssl` atau set `ENABLE_SSL=1` di `docker-compose.yml`:
```bash
python examples/api/server.py --port 8443 --ssl
```
Sertifikat TLS self-signed akan dibuat secara otomatis.