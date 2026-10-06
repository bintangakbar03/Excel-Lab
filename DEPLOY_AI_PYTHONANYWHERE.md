# Excel Lab + ChatGPT — deploy di PythonAnywhere

Versi ini menambahkan backend Flask tanpa mengubah struktur materi/workbook yang sudah ada.

## Fitur yang ditambahkan

- Koreksi deterministik Excel Lab tetap menentukan benar/salah.
- Setelah koreksi lokal, endpoint `/api/ai/grade` meminta OpenAI menjelaskan jawaban, memberi koreksi dan tip.
- Halaman **Latihan workbook** memiliki tombol rekomendasi adaptif. Endpoint `/api/ai/recommend` membaca riwayat upaya lokal yang dikirim browser, memilih latihan yang sudah ada, lalu membuat 3 tantangan tambahan.
- API key hanya berada di server, tidak pernah dikirim ke JavaScript browser.
- Backend sudah menyiapkan **Continue with ChatGPT** memakai Authorization Code + PKCE + OpenID Connect. Tombol baru aktif bila `OPENAI_CLIENT_ID` tersedia.
- SQLite menyimpan mapping akun ChatGPT dan menyediakan endpoint progress akun untuk pengembangan sinkronisasi berikutnya.

## 1. Timpa folder website

Folder yang dipakai pada konfigurasi ini:

```bash
/home/ExcelLab/Excel-Lab-siap-hosting
```

Upload seluruh isi ZIP ini ke folder tersebut. Pastikan minimal ada:

```text
app.py
app.mjs
index.html
styles.css
requirements.txt
data/
workbooks/
vendor/
```

## 2. Buat virtualenv dan install backend

Di Bash console PythonAnywhere:

```bash
cd ~/Excel-Lab-siap-hosting
mkvirtualenv --python=/usr/bin/python3.13 excellab-ai
pip install -r requirements.txt
```

Jika web app PythonAnywhere kamu menggunakan versi Python yang berbeda, buat virtualenv dengan versi yang sama dengan web app tersebut.

## 3. Buat secret server

Generate secret Flask:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Lalu buat/edit `~/.env`:

```bash
nano ~/.env
```

Isi awal:

```dotenv
FLASK_SECRET_KEY=PASTE_RANDOM_SECRET_DI_SINI
OPENAI_API_KEY=PASTE_OPENAI_API_KEY_DI_SINI
OPENAI_MODEL=gpt-5.6-luna
```

Simpan. Jangan menaruh `OPENAI_API_KEY` di `app.mjs`, `index.html`, GitHub public, atau kode frontend lain.

> `gpt-5.6-luna` dipilih sebagai default karena cocok untuk workload tutoring yang cost-sensitive. Kalau kualitas feedback ingin dinaikkan, ubah `OPENAI_MODEL=gpt-5.6-terra`.

## 4. Ubah web app dari static-only menjadi Flask

Di tab **Web** PythonAnywhere:

1. Pada bagian **Virtualenv**, isi:

```text
/home/ExcelLab/.virtualenvs/excellab-ai
```

2. Buka file WSGI web app kamu dan gunakan konfigurasi berikut:

```python
import sys

project_home = '/home/ExcelLab/Excel-Lab-siap-hosting'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

from app import app as application
```

`app.run()` yang ada di `app.py` aman karena berada di dalam `if __name__ == "__main__"`; WSGI tidak menjalankannya.

3. Jika sebelumnya kamu memasang static mapping `/ -> /home/ExcelLab/Excel-Lab-siap-hosting`, hapus mapping `/` tersebut supaya request `/api/...` masuk ke Flask. Flask dalam `app.py` sekarang juga melayani file frontend dan workbook.

4. Tekan **Reload** pada web app.

## 5. Tes AI

Buka:

```text
https://excellab.pythonanywhere.com/
```

Masuk ke salah satu **Latihan workbook** → isi jawaban → **Evaluasi jawaban**.

Urutan yang seharusnya terlihat:

1. Excel Lab langsung memberi hasil benar/salah.
2. Di bawahnya muncul **ChatGPT sedang menganalisis jawaban…**.
3. Lalu muncul kartu **Feedback ChatGPT** dengan penjelasan, koreksi, tip, dan skill berikutnya.

Di halaman **Latihan workbook**, tekan **Buat rekomendasi** untuk mendapatkan latihan berikutnya dan tantangan tambahan.

## 6. Kalau AI gagal

Buka tab **Web → Error log**. Hal yang perlu dicek:

```bash
cd ~/Excel-Lab-siap-hosting
workon excellab-ai
python -c "import flask, requests, jwt, dotenv; print('dependencies OK')"
set -a; source ~/.env; set +a
python -c "import os; print(bool(os.getenv('OPENAI_API_KEY')))"
```

Hasil terakhir harus `True`.

Pada akun PythonAnywhere Free, `api.openai.com` saat ini ada di allowlist mereka, sehingga panggilan OpenAI API dapat melewati proxy PythonAnywhere.

## 7. Mengaktifkan Continue with ChatGPT

Login ChatGPT untuk website adalah fitur terpisah dari API key. OpenAI harus memberikan OAuth client website terlebih dahulu. Setelah kamu menerima konfigurasi dari OpenAI, tambahkan ke `~/.env`:

```dotenv
OPENAI_CLIENT_ID=oaiapp_xxxxxxxxx
OPENAI_CLIENT_SECRET=
OPENAI_TOKEN_AUTH_METHOD=none
OPENAI_REDIRECT_URI=https://excellab.pythonanywhere.com/auth/openai/callback
```

Gunakan `OPENAI_TOKEN_AUTH_METHOD` dan `OPENAI_CLIENT_SECRET` **persis seperti yang diprovisikan OpenAI**. Jika client kamu confidential dan menggunakan `client_secret_basic`:

```dotenv
OPENAI_CLIENT_SECRET=xxxxxxxxx
OPENAI_TOKEN_AUTH_METHOD=client_secret_basic
```

Callback yang harus diregistrasikan ke OpenAI:

```text
https://excellab.pythonanywhere.com/auth/openai/callback
```

Setelah reload, tombol **Continue with ChatGPT** akan muncul di header dan Pengaturan.

### Catatan PythonAnywhere Free untuk login

Flow login membutuhkan server menghubungi `auth.openai.com` untuk token exchange dan JWKS verification. Saat file ini dibuat, `api.openai.com` ada di allowlist PythonAnywhere Free tetapi `auth.openai.com` belum terlihat di daftar itu. Jadi setelah memperoleh OAuth client, kemungkinan kamu perlu:

- meminta PythonAnywhere menambahkan `auth.openai.com` ke allowlist, atau
- menggunakan paket PythonAnywhere dengan unrestricted outbound Internet.

## 8. ChatGPT Plus ≠ OpenAI API billing

Versi yang bisa langsung dipakai ini menjalankan AI menggunakan `OPENAI_API_KEY` milik aplikasi, sehingga biaya/kuota berasal dari OpenAI API project tersebut.

**Continue with ChatGPT untuk identitas tidak otomatis membuat request AI dibayar oleh paket ChatGPT Plus/Pro user.** OpenAI memiliki izin/flow terpisah untuk ChatGPT plan usage pada aplikasi yang eligible/berpartisipasi. Jadi jangan menghapus `OPENAI_API_KEY` hanya karena login ChatGPT sudah aktif, kecuali aplikasi kamu juga sudah mendapat akses resmi untuk plan usage dan flow tersebut sudah diimplementasikan.

## File penting

- `app.py` — Flask API, OpenAI Responses API, OAuth ChatGPT, SQLite.
- `app.mjs` — UI feedback AI, rekomendasi AI, tombol login/logout.
- `styles.css` — tampilan kartu AI.
- `.env.example` — contoh environment variable tanpa credential asli.
- `requirements.txt` — dependency backend.
- `pythonanywhere_wsgi.py.example` — contoh WSGI.

## Keamanan yang sudah diterapkan

- API key tidak pernah dikirim ke browser.
- OAuth memakai state + nonce + PKCE S256.
- Transaction OAuth disimpan server-side di SQLite dan expired 10 menit.
- ID token diverifikasi signature, issuer, audience, expiry dan nonce sebelum membuat session.
- Session cookie HttpOnly + Secure + SameSite=Lax.
- Endpoint AI memiliki rate limit sederhana per user/IP.
- Payload dibatasi ukuran dan panjang input.
- Jawaban benar/salah tidak diserahkan sepenuhnya ke model AI.
