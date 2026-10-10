# Lumivara Sentinel

**Powered by rygroup.**

Bot otomasi untuk [Lumivara Online](https://lumivaraonline.com) yang dikendalikan lewat
Telegram. Jalan di PC/server kamu sendiri, main pakai akun kamu sendiri, dan cuma nurut
sama akun Telegram kamu.

> 🔴 **Satu koneksi saja.** Game ini cuma mengizinkan **1 koneksi aktif per akun**. Kalau bot
> jalan **dan** kamu buka game di browser/HP, keduanya saling tendang (`4002 Opened in another
> tab`) dan karakter nggak farming. Saat bot jalan, **tutup game di tempat lain**.
>
> ⚠️ **Risiko kamu tanggung sendiri.** Mengotomasi game ini melanggar Terms of Service-nya dan
> operatornya aktif mencari bot (bisa ban + sita item). Bot ini nggak punya fitur anti-deteksi;
> jeda/jitter yang ada cuma supaya nggak nge-spam server.

---

## Install (satu baris)

### Windows

Buka **PowerShell** (tekan `Win`, ketik *PowerShell*, Enter), lalu paste:

```powershell
irm https://raw.githubusercontent.com/rygroup-dev/lumivara-sentinel/main/install.ps1 | iex
```

### Linux / macOS / Termux (Android)

```bash
curl -fsSL https://raw.githubusercontent.com/rygroup-dev/lumivara-sentinel/main/install.sh | bash
```

Installer-nya:

1. Cek Python 3.11+ (Windows: dipasang otomatis lewat `winget` kalau belum ada).
2. Download kode ke `~/lumivara-sentinel` (Windows: `C:\Users\<kamu>\lumivara-sentinel`).
3. Bikin virtualenv & install dependency.
4. Tanya 3 hal (penjelasan lengkap di bawah): **token bot Telegram**, **ID Telegram kamu**
   (dideteksi otomatis), dan **cookie login game**. Semuanya dicek langsung ke server sebelum
   disimpan.
5. Windows: bikin shortcut **Lumivara Sentinel** di Desktop. Linux: (opsional) pasang sebagai
   service yang otomatis jalan saat boot.

Jalankan installer yang sama lagi kapan saja untuk **update** — setelan & cookie kamu tetap.

---

## Yang ditanyakan saat setup

### 1. Token bot Telegram

Tiap orang pakai bot Telegram **sendiri** (gratis):

1. Buka [@BotFather](https://t.me/BotFather) di Telegram → kirim `/newbot`.
2. Kasih nama (bebas) dan username (harus diakhiri `bot`, mis. `lumi_andi_bot`).
3. BotFather membalas dengan token seperti `123456789:AAH...` → copy, paste ke installer.

### 2. ID Telegram kamu

Installer menampilkan link bot kamu (`https://t.me/<username_bot>`). Buka, tekan **START**.
Installer otomatis menangkap ID kamu dan bot mengirim pesan *"setup OK ✅"*. Hanya ID ini yang
bisa mengontrol bot. (Kalau gagal, ketik manual — cek ID kamu di [@userinfobot](https://t.me/userinfobot).)

### 3. Login game (cookie)

Game login pakai Google, jadi bot memakai **cookie sesi** dari browser yang sudah login.
Harus dari **browser di PC/laptop** (Chrome atau Edge):

1. Buka **https://lumivaraonline.com**, login dengan Google sampai masuk ke game.
2. Tekan **F12** (atau klik kanan → *Inspect*).
3. Buka tab **Application**. Kalau nggak kelihatan, klik **`>>`** di deretan tab.
4. Panel kiri: **Storage → Cookies → `https://lumivaraonline.com`**.
5. Klik baris bernama **`pixelrpg_google_session`**, lalu copy isi kolom **Value**
   (teks panjang yang diawali `eyJ`). Double-click di kolom Value → `Ctrl+A` → `Ctrl+C`.
6. Paste ke installer.

Catatan:
- Lewat **Console** (`document.cookie`) **nggak bisa** — cookie ini HttpOnly.
- Installer juga menerima seluruh header `cookie:` dari tab **Network** kalau itu lebih gampang.
- Setelah cookie diambil, **tutup tab game** (lihat aturan satu koneksi di atas).
- Cookie berlaku **±30 hari** (installer menampilkan tanggalnya). Kalau bot mulai
  `disconnected` terus, ambil cookie baru dan jalankan:

  ```bash
  # Windows (di folder lumivara-sentinel)
  .venv\Scripts\python -m lumivara.setup_wizard --cookie
  # Linux / macOS
  .venv/bin/python -m lumivara.setup_wizard --cookie
  ```

> 🔒 **Cookie = akses penuh ke akun game kamu, token bot = kontrol bot kamu.** Keduanya cuma
> disimpan di file `.env` di komputer kamu. Jangan pernah dikirim ke orang lain atau di-upload.

---

## Menjalankan

- **Windows:** double-click shortcut **Lumivara Sentinel** di Desktop (atau `run.bat` di folder
  install). Biarkan window-nya terbuka; tutup = bot berhenti.
- **Linux/macOS:** `cd ~/lumivara-sentinel && ./run.sh` (atau lewat service kalau dipasang).

Lalu di Telegram kirim **`/menu`** ke bot kamu.

Membuka `run.bat` dua kali aman — copy kedua langsung menolak jalan, karena dua bot di akun yang
sama akan saling tendang. Kalau internet atau Telegram putus, bot menunggu dan mencoba lagi
sendiri.

Log ada di `logs/bot.log`; tiap 30 detik ada baris `STATUS`, contoh:

```
STATUS online | Lv49 archer @field (target field) | hp 930/930 | hp-pots 264 | silver 470 | kills 6969 ...
```

---

## Apa yang dikerjakan bot

| Fitur | Keterangan |
|---|---|
| 🌾 **Farming** | Serang mob terdekat, rotasi skill, ambil loot, revive saat mati. Drop yang gagal diambil di-skip. |
| 🗺 **Pilih map otomatis** | Dari tabel zona game (Verdant 1–10 sampai Sunfallen 121–130). Bot juga mengukur EXP, silver, potion dan kematian per map, lalu pilih map yang paling untung. Map yang bikin mati 3× atau boros potion dihindari dulu. Pindah map lewat portal. |
| ❤️ **Heal hemat** | Minum potion hanya saat sedang diserang di bawah `POTION_HP_PERCENT`; Yellow/White/Orange Potion dipakai duluan (Yellow dibeli dari market ±13 silver, heal 300–350), Red cuma cadangan. Kalau nggak ada mob dekat, duduk (regen gratis). |
| 🏪 **Belanja di town** | Jalan ke merchant → jual loot monster → beli Red Potion (maks % silver), panah, Return Scroll. Potion < 5 di field → pulang pakai Return Scroll. Item terdaftar game (potion, scroll, refine stone, …) dan **card** tidak pernah dijual. |
| 🎒 **Equip** | Pakai gear terbaik di tas per slot sesuai class (Archer → Bow, dst). |
| ✨ **Skill** | Belajar skill class satu-satu (pasif damage dulu). |
| 📊 **Stat** | Bobot per class (Archer DEX 3 : AGI 2 : VIT 1). Stat yang terlalu mahal ditunda sampai point cukup. |
| ♻️ **Respec** | Di bawah Lv80 (gratis), kalau stat/skill terlanjur salah alokasi, bot ke Reset Master sekali lalu alokasi ulang. |
| 📜 **Quest & hadiah** | 10 quest tutorial, daily, mail, hadiah 100 kill harian diklaim otomatis. Quest *change job* & *jual-beli* perlu kamu lakukan sekali di game. |
| ⚖️ **Market pemain** (`ENABLE_MARKET`) | Di broker town: tiap loot, refine stone, card, potion lebih, blue potion, relic box, fly/butterfly wing dicek ke board (item hadiah/`gifted` tidak bisa dijual, otomatis dilewati). Gear class lain / lebih jelek dipasang 1 silver di bawah listing termurah model yang sama; kalau `MARKET_GEAR_RELIST_HOURS` (24 jam) belum laku, dicabut dan dipasang ulang 15% lebih murah, dan kalau masih belum laku dibongkar jadi fragment (fragment ikut dijual). Tidak pernah di-*destroy*. Fly Wing / Return Scroll / potion tidak dijual di bawah harga NPC. Jual langsung ke bid kalau ≥85% harga rata-rata, kalau tidak pasang jual 1 silver di bawah ask termurah (tidak di bawah 85% rata-rata). Kalau NPC lebih untung setelah fee (2,5% pasang + 8% pajak), ditinggal untuk NPC. Potion dibeli dari market kalau lebih murah dari NPC. Order yang tidak laku > `MARKET_REPRICE_HOURS` dibatalkan & dipasang ulang dengan harga baru. |
| 💰 **Gold Exchange** | Baca harga (bid/ask). Dengan `GOLD_AUTOBUY=true`, tiap 10 menit silver di atas `GOLD_RESERVE` ditukar ke Gold di ask termurah (maks `GOLD_MAX_PRICE`). |
| 🧭 **Rute aman** | Rute portal tidak lewat map yang jauh di atas level (lewat town). Kalau nabrak tembok saat jalan ke portal, bot coba jalan memutar. |

## Dashboard Telegram (`/menu`)

Status live (level, class, map, HP/SP, silver, kills, stok potion & panah, hasil belanja,
quest) + tombol:

- **▶️/⏹ Start/Stop Farm**, **🔄 Refresh**
- Toggle **📜 Quest · 📊 Stats · 💀 Revive · 🎒 Equip · ✨ Skills**
- **🗺 Travel** — pilih map (dikunci) atau *Auto by level*
- **🎒 Gear** — gear terpakai vs terbaik, *Equip best now*
- **🏪 Town run** — pulang & belanja sekarang
- **💰 Gold Market** — harga & hitungan premium
- **⚖️ Market now** — ke broker & jual sekarang (pakai Return Scroll kalau di field)
- **❤️ Heal now**, **🎁 Claim all**, **⚙️ Settings**, **🧾 WS Log**

Dashboard update sendiri tiap ±5 detik di menu utama.

---

## Pengaturan (`.env`)

Semua opsional selain tiga yang diisi installer. Ubah, lalu restart bot.

| Key | Default | Arti |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token dari @BotFather |
| `TELEGRAM_CHAT_ID` | — | ID Telegram pemilik (satu-satunya yang bisa kontrol) |
| `LUMIVARA_COOKIE` | — | `pixelrpg_google_session=...` |
| `POTION_HP_PERCENT` | 55 | Minum potion di bawah HP% ini (saat diserang) |
| `FARM_ZONE` | auto | `auto`, atau paksa: `field`, `meadow`, `snow`, `desert`, `swamp`, `wildwood`, `dunes`, `glacier`, `caldera`, `moor`, `crystal`, `tempest`, `citadel` |
| `ZONE_MARGIN` | 15 | Map dipakai kalau level ≥ level minimum map + margin |
| `ENABLE_SELL` | true | Belanja otomatis di town |
| `POTION_TARGET` | 40 | Target stok Red Potion |
| `BIG_POTION_TARGET` | 60 | Target stok Yellow Potion (dibeli dari market ±13 silver, heal 300–350 — dipakai duluan) |
| `POTION_BUDGET_PCT` | 30 | Maks % silver untuk potion per kunjungan |
| `ARROW_MIN` | 1000 | Beli 2000 panah kalau di bawah ini |
| `POTION_KEEP` | 150 | Red Potion hasil drop di atas ini dijual |
| `FARM_GOAL` | silver | Map dinilai dari `silver`/jam bersih, atau `exp`/jam |
| `ENABLE_MARKET` | false | Jual-beli otomatis di market pemain |
| `MARKET_SELL_CARDS` | true | Ikut jual card di market |
| `MARKET_SELL_GEAR` | true | Jual gear yang tidak akan dipakai (class lain / lebih jelek) |
| `MARKET_GEAR_SLOTS` | 20 | Maks gear dipasang sekaligus (40 kalau premium) |
| `MARKET_REPRICE_HOURS` | 6 | Pasang ulang order item (loot, card, potion) yang belum laku setelah sekian jam |
| `MARKET_GEAR_RELIST_HOURS` | 24 | Gear belum laku sekian jam → pasang ulang 15% lebih murah, lalu jadi fragment |
| `MARKET_EVERY_MIN` | 45 | Paling sering ke broker tiap sekian menit |
| `GOLD_AUTOBUY` | false | Tukar silver → Gold otomatis |
| `GOLD_RESERVE` | 2000 | Silver yang selalu disisakan |
| `GOLD_MAX_PRICE` | 8000 | Harga maksimum per Gold (silver) |
| `STAT_BUILD` | dex,agi,vit,… | Urutan stat untuk class tanpa bobot bawaan |
| `ACTION_DELAY_MIN` / `_JITTER` | 0.45 / 0.55 | Jeda antar aksi (detik) |

---

## Install manual

```bash
git clone https://github.com/rygroup-dev/lumivara-sentinel.git
cd lumivara-sentinel
python -m venv .venv
# Windows: .venv\Scripts\python   |   Linux/macOS: .venv/bin/python
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m lumivara.setup_wizard
.venv/bin/python main.py
```

---

## Troubleshooting

| Gejala | Solusi |
|---|---|
| Bot nggak balas `/menu` | Pastikan window bot masih terbuka/servicenya jalan, dan kamu chat dari akun Telegram yang sama dengan saat setup. |
| `disconnected` terus | Cookie kedaluwarsa → `python -m lumivara.setup_wizard --cookie`. |
| `KICKED (4002)` di log | Game sedang dibuka di browser/HP. Tutup. |
| `map change (Moved to …)` di log | Normal — pindah map memang menutup koneksi, bot reconnect sendiri. |
| `already running from this folder` | Bot sudah jalan di window lain. |
| `can't reach Telegram — retrying` | Internet/Telegram putus; bot menunggu sendiri. |
| Karakter sering mati / silver turun | Naikkan `ZONE_MARGIN` atau paksa map lebih gampang (`FARM_ZONE=meadow`). |
| Akun belum punya karakter | Buat karakter dulu di game. |

---

## Struktur

```
install.ps1 / install.sh   installer satu baris
run.bat / run.sh           launcher
main.py                    entrypoint
lumivara/
  setup_wizard.py          setup interaktif (token, chat id, cookie)
  config.py                baca .env
  protocol.py              format pesan WebSocket, tabel map/portal/NPC/item
  state.py                 rekonstruksi state game dari snapshot
  client.py                WebSocket + HTTP, pacing, reconnect
  automation.py            farming, belanja, equip, skill, stat, quest
  intel.py                 statistik per map/mob untuk memilih tempat farming
  telegram_bot.py          dashboard Telegram
  app.py                   wiring, retry, kunci satu-instance
```
