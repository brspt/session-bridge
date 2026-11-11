# Session Bridge

Satu EXE untuk memindahkan dan sinkron otomatis chat antar harness AI di Windows.
Jalankan `SessionBridge.exe`. Tidak perlu instal apa pun, tapi harness tujuan harus sudah terpasang.

> **English:** one Windows EXE to move and auto-sync chat sessions between Claude Code, Codex, Delta,
> OpenCode, Hermes and other detected AI harnesses. Download `SessionBridge.exe` from Releases and run it.
> Switch the window to English with **Bahasa → English** at the bottom left.

## Halaman

Menu di kiri. Status sinkron otomatis selalu tampil di pojok kiri bawah (klik untuk ke halamannya).
Di atasnya ada pilihan **Bahasa** (`Indonesia` / `English`). Jendela langsung berganti bahasa dan pilihannya
tersimpan di `config.json`, juga dipakai output baris perintah.

**Pindahkan.** (1) Pilih `Dari` dan `Ke`. Tombol `⇄` membalik arah. (2) Centang sesi di tabel
(`Cari` menyaring judul dan folder, `Pilih semua` memilih yang tampil), lalu tekan tombol biru
`Pindahkan N sesi ke …`. Hasilnya muncul di `Aktivitas`.

- Chat asli di harness asal tetap ada. Data di tujuan tidak diubah atau dihapus.
- `File bundle (.json)` bisa dipilih di kedua sisi: sebagai `Ke` untuk cadangan atau pindah PC, sebagai `Dari` untuk memasukkannya lagi.
- Sesi yang dipindahkan ke Claude Code langsung dibuka di tab Code aplikasi Claude desktop (tautan `claude://resume`),
  tanpa menutup aplikasinya, juga kalau sesinya sudah pernah dipindahkan. Di terminal, jalankan `claude --resume`
  dari folder kerja sesi itu.
- Sinkron otomatis tidak membuka aplikasi desktop, supaya jendela tidak berpindah sendiri saat Anda bekerja.
  Sesi baru hasil sinkron otomatis ke Claude Code: pindahkan sekali lewat halaman ini untuk memunculkannya di tab Code.

**Sinkron Otomatis.** (1) Tambah rute `Dari → Ke` (centang `Dua arah` untuk kedua arah). (2) Atur jadwal dan batas hari. (3) Jalankan:

- `Pratinjau`: hitung yang akan disalin, tidak menulis apa pun. Selalu coba ini dulu.
- `Sinkron sekarang`: satu putaran.
- `Mulai otomatis` / `Hentikan otomatis`: satu tombol untuk proses latar. Tetap jalan setelah jendela ditutup.
- `Mulai sinkron otomatis saat login Windows`: mati secara default. Kalau EXE dipindah, centang ulang.

**Harness.** Daftar harness, jumlah sesi, akses (baca + tulis atau baca-saja), dan alasannya. Klik baris untuk lokasi dan catatan lengkap.

## Dukungan

| Harness | Baca | Tulis | Catatan |
|---|---|---|---|
| Claude Code | ya | ya | File JSONL sesi, panggilan tool jadi blok tool asli Claude, dibuka di tab Code aplikasi desktop lewat tautan resume |
| Codex | ya | ya | Rollout + indeks thread |
| Delta | ya | ya | Lewat CLI resmi, thread selalu privat |
| OpenCode | ya | ya | Lewat CLI aplikasi desktop OpenCode 2.x (`session import`, tabel `session_v2`); 1.x lewat `opencode import` |
| Hermes | ya | ya | Backup DB sebelum menulis (satu per hari, simpan 7) |
| DeepSeek Harness | ya | ya | Log sesi `~/.dsh` (frame zstd). Hasil tulis lolos pemuat aplikasinya sendiri; sesi yang sedang dibuka tidak disentuh |
| Copilot CLI | ya | ya | Log event `~/.copilot/session-state`. Hasil tulis lolos pemuat SDK Copilot bawaan VS Code |
| AionUi | ya | ya | Percakapan `aionrs` baru dengan workspace sendiri; lolos backend AionUi. Backup DB sebelum menulis |
| Cherry Studio | sebagian | ya, belum teruji aplikasinya | Hanya sesi agen di `agents.db` (chat utama ada di IndexedDB). Tulis meniru bentuk baris yang ada. Backup DB sebelum menulis |
| Antigravity | ya | tidak | Isi percakapan protobuf tanpa skema, hanya indeks teks terbaca |
| Zed | ya | tidak | Belum ada thread nyata untuk diuji |
| Lainnya | ya | tidak | Folder/SQLite mirip sesi dideteksi otomatis, label `(terdeteksi)`; formatnya tebakan, jadi tidak ditulis |

Batas tulis yang perlu diketahui:

- Panggilan tool hanya jadi blok tool asli di Claude Code. Di harness lain ia masuk sebagai teks.
- AionUi dan Cherry Studio menampilkan riwayat yang disalin, tetapi agennya tidak otomatis mengingatnya:
  sesi agen di balik percakapan itu mulai kosong.
- Penulis Cherry Studio belum pernah diperiksa oleh aplikasinya (aplikasi tidak terpasang saat dibuat).
  Coba dulu dengan satu sesi dan lihat hasilnya di Cherry Studio.

**DeepSeek.** DeepSeek Harness (`~/.dsh`) punya adaptor sendiri. Sesi DeepSeek yang dijalankan lewat
Claude Code (profil `~/.claude/profiles`) atau OpenCode tersimpan di dua harness itu, jadi ikut lewat sana.

## Cara kerja sinkron

- **Hanya menambah.** Tidak pernah mengubah atau menghapus pesan yang sudah ada.
- **Anti gema.** Pesan dikenali dari peran + isi + urutan kemunculan, jadi salinan tidak memantul balik.
- **Tautan lama dihormati.** Sesi yang sudah dipasangkan (impor Claude dari Codex, cermin delta-sync) tidak
  disalin ulang. Kontak pertama hanya mencatat kondisi (`adopsi`), pesan baru sesudahnya yang disalin.
- **Batas hari.** Sesi lebih tua dari batas dilewati, kecuali sudah punya pasangan di tujuan rute itu.
- **Sesi aktif ditunda.** Sumber atau tujuan yang berubah 60 detik terakhir dicoba lagi putaran berikutnya.

### Berdampingan dengan delta-sync

Cermin Codex → Delta lama (`~/.codex/integrations/delta-sync`) tidak disentuh. Selama aktif (tidak ada file `STOP`):

- Codex → Delta dilewati, itu tugas delta-sync.
- Thread Delta hasil cermin tidak dipakai sebagai sumber dan tidak ditulisi.
- X → Delta dilewati kalau X → Codex juga aktif, karena delta-sync sudah membawanya.
- Delta → Codex dilewati kecuali sudah tertaut, supaya tidak kembali sebagai thread kedua.

## Batasan

- Balasan yang diketik di thread Delta hasil cermin tidak ikut, delta-sync satu arah.
- Menambah pesan ke thread Delta yang bukan buatan Session Bridge belum teruji. Kalau ditolak Delta, hasilnya
  baris `GAGAL` di log dan tidak ada data berubah.
- Output tool dipotong 8000 karakter saat ditulis ke harness lain supaya resume tidak berat. Bundle menyimpan utuh.
- Delta menampilkan waktu buat thread, bukan waktu ubah, jadi batas hari untuk Delta sebagai sumber memakai waktu buat.
- Kirim thread Delta panjang yang putus di tengah penambahan bisa menggandakan batch pertama saat diulang.

**Keamanan bundle.** File ekspor berisi seluruh percakapan, termasuk apa pun yang pernah tertempel
di dalamnya seperti API key. Perlakukan seperti data rahasia.

File kredensial harness tidak pernah dibaca. Log hanya berisi id, jumlah, dan jenis galat, bukan isi percakapan.

## Baris perintah

```
SessionBridge.exe              jendela utama
SessionBridge.exe --detect     daftar harness (JSON)
SessionBridge.exe --preview    pratinjau rute tersimpan
SessionBridge.exe --sync-once  satu putaran sinkron
SessionBridge.exe --start      mulai proses latar
SessionBridge.exe --stop       hentikan proses latar
SessionBridge.exe --self-test  uji diri di folder sementara
```

Karena EXE berjenis windowed, hasilnya juga ditulis ke `~/.session-bridge/last-output.txt`.

## Data program

`~/.session-bridge/` (ganti lewat env `SESSION_BRIDGE_DIR`): `config.json`, `state.sqlite`, `delta.json`,
`backups/`, `bridge.log`. Jangan hapus `state.sqlite` dan `delta.json`: tautan antar sesi ada di sana, tanpa
itu sinkron berikutnya membuat salinan baru.

## Build dan uji

Butuh [uv](https://docs.astral.sh/uv/).

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
uv run --no-project --python 3.12 --with zstandard python -B test_bridge.py --live
```

`--live` menjalankan biner codex/opencode/hermes/claude asli terhadap folder sandbox, tanpa panggilan model.
