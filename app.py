from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import jwt
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_from_directory, session

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(Path.home() / ".env")
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__, static_folder=None)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()
OPENAI_CLIENT_ID = os.getenv("OPENAI_CLIENT_ID", "").strip()
OPENAI_CLIENT_SECRET = os.getenv("OPENAI_CLIENT_SECRET", "").strip()
OPENAI_TOKEN_AUTH_METHOD = os.getenv("OPENAI_TOKEN_AUTH_METHOD", "none").strip()
OPENAI_REDIRECT_URI = os.getenv(
    "OPENAI_REDIRECT_URI",
    "https://excellab.pythonanywhere.com/auth/openai/callback",
).strip()

OIDC_ISSUER = "https://auth.openai.com"
OIDC_AUTHORIZATION_ENDPOINT = "https://auth.openai.com/api/accounts/authorize"
OIDC_TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
OIDC_JWKS_URI = "https://auth.openai.com/.well-known/jwks.json"

DB_DIR = BASE_DIR / "instance"
DB_DIR.mkdir(exist_ok=True)
DB_PATH = DB_DIR / "excellab.db"

PRACTICE = json.loads((BASE_DIR / "data" / "practice.json").read_text(encoding="utf-8"))
EXERCISES = {item["id"]: item for item in PRACTICE.get("exercises", [])}

RATE_BUCKETS: dict[str, deque[float]] = defaultdict(deque)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL DEFAULT 'openai',
                provider_sub TEXT NOT NULL UNIQUE,
                email TEXT,
                name TEXT,
                picture TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_transactions (
                browser_key TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                code_verifier TEXT NOT NULL,
                nonce TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS progress (
                user_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS ai_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                exercise_id TEXT,
                passed INTEGER,
                kind TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
            );
            """
        )


init_db()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    with db() as conn:
        return conn.execute(
            "SELECT id, email, name, picture FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def rate_limit(name: str, limit: int, window_seconds: int) -> bool:
    identity = str(session.get("user_id") or request.remote_addr or "anon")
    key = f"{name}:{identity}"
    now = time.time()
    bucket = RATE_BUCKETS[key]
    while bucket and bucket[0] <= now - window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def same_origin() -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        return urlparse(origin).netloc == request.host
    except ValueError:
        return False


def extract_response_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    parts: list[str] = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def openai_structured(name: str, schema: dict, system: str, user: str, max_tokens: int = 650) -> dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY belum dikonfigurasi di server.")

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_MODEL,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_output_tokens": max_tokens,
            "reasoning": {"effort": "low"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": name,
                    "strict": True,
                    "schema": schema,
                }
            },
        },
        timeout=35,
    )
    if response.status_code >= 400:
        request_id = response.headers.get("x-request-id", "-")
        detail = response.text[:500]
        app.logger.warning("OpenAI error %s request=%s body=%s", response.status_code, request_id, detail)
        raise RuntimeError(f"OpenAI API gagal ({response.status_code}).")

    text = extract_response_text(response.json())
    if not text:
        raise RuntimeError("OpenAI tidak mengembalikan feedback teks.")
    return json.loads(text)


FEEDBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "explanation": {"type": "string"},
        "correction": {"type": "string"},
        "tip": {"type": "string"},
        "next_skill": {"type": "string"},
    },
    "required": ["summary", "explanation", "correction", "tip", "next_skill"],
    "additionalProperties": False,
}

RECOMMEND_SCHEMA = {
    "type": "object",
    "properties": {
        "focus_summary": {"type": "string"},
        "study_tip": {"type": "string"},
        "extra_drills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "difficulty": {"type": "string", "enum": ["Dasar", "Menengah", "Lanjutan"]},
                    "skill": {"type": "string"},
                    "task": {"type": "string"},
                    "hint": {"type": "string"},
                },
                "required": ["title", "difficulty", "skill", "task", "hint"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["focus_summary", "study_tip", "extra_drills"],
    "additionalProperties": False,
}


@app.get("/api/config")
def api_config():
    user = current_user()
    return jsonify(
        {
            "aiAvailable": bool(OPENAI_API_KEY),
            "aiModel": OPENAI_MODEL if OPENAI_API_KEY else None,
            "chatgptLoginAvailable": bool(OPENAI_CLIENT_ID),
            "user": dict(user) if user else None,
        }
    )


@app.get("/api/session")
def api_session():
    user = current_user()
    return jsonify({"user": dict(user) if user else None})


@app.post("/api/ai/grade")
def ai_grade():
    if not same_origin():
        return jsonify({"error": "Origin tidak diizinkan."}), 403
    if not rate_limit("grade", 45, 3600):
        return jsonify({"error": "Batas feedback AI sementara tercapai. Coba lagi nanti."}), 429

    body = request.get_json(silent=True) or {}
    exercise_id = str(body.get("exerciseId", ""))[:160]
    answer = str(body.get("answer", ""))[:4000]
    mode = str(body.get("mode", "result"))[:40]
    passed = body.get("passed") is True
    computed_value = body.get("computedValue")
    formula_ok = body.get("formulaOk") is not False

    item = EXERCISES.get(exercise_id)
    if not item or not answer:
        return jsonify({"error": "Latihan atau jawaban tidak valid."}), 400

    system = (
        "Anda adalah tutor Microsoft Excel berbahasa Indonesia untuk pemula sampai menengah. "
        "Status benar/salah sudah ditentukan mesin aplikasi secara deterministik; jangan membantah status itu. "
        "Jelaskan dengan singkat, spesifik, dan mendidik. Jika salah, tunjukkan letak konsep yang perlu diperbaiki "
        "dan berikan koreksi formula bila relevan. Jangan mengarang data di luar konteks latihan."
    )
    user_prompt = json.dumps(
        {
            "exercise": {
                "title": item.get("title"),
                "objective": item.get("objective"),
                "instructions": item.get("instructions"),
                "required_functions": item.get("requiredFunctions", []),
                "expected_result": item.get("expected"),
                "reference_solution": item.get("solution"),
                "reference_explanation": item.get("explanation"),
            },
            "learner": {
                "answer": answer,
                "mode": mode,
                "computed_value": computed_value,
                "deterministic_passed": passed,
                "required_functions_found": formula_ok,
            },
            "output_guidance": {
                "summary": "1 kalimat penilaian",
                "explanation": "mengapa jawaban benar/salah",
                "correction": "formula/perbaikan yang disarankan; kosong jika tak perlu",
                "tip": "1 tip praktis",
                "next_skill": "skill Excel yang sebaiknya dilatih berikutnya",
            },
        },
        ensure_ascii=False,
    )

    try:
        result = openai_structured("excel_feedback", FEEDBACK_SCHEMA, system, user_prompt)
    except (RuntimeError, requests.RequestException, json.JSONDecodeError) as exc:
        return jsonify({"error": str(exc)}), 503

    user = current_user()
    with db() as conn:
        conn.execute(
            "INSERT INTO ai_activity(user_id, exercise_id, passed, kind, created_at) VALUES(?,?,?,?,?)",
            (user["id"] if user else None, exercise_id, 1 if passed else 0, "grade", utcnow()),
        )
    return jsonify(result)


@app.post("/api/ai/recommend")
def ai_recommend():
    if not same_origin():
        return jsonify({"error": "Origin tidak diizinkan."}), 403
    if not rate_limit("recommend", 12, 3600):
        return jsonify({"error": "Batas rekomendasi AI sementara tercapai. Coba lagi nanti."}), 429

    body = request.get_json(silent=True) or {}
    completed = {str(x) for x in (body.get("completedExercises") or []) if isinstance(x, str)}
    attempts = body.get("attempts") or []
    recent = [x for x in attempts[-30:] if isinstance(x, dict)]

    weak_functions: dict[str, int] = defaultdict(int)
    weak_levels: dict[str, int] = defaultdict(int)
    for attempt in recent:
        if attempt.get("passed") is False:
            item = EXERCISES.get(str(attempt.get("id", "")))
            if not item:
                continue
            weak_levels[str(item.get("levelId", ""))] += 1
            for fn in item.get("requiredFunctions", []):
                weak_functions[str(fn)] += 1

    def score(item: dict) -> tuple[int, int, str]:
        fn_score = sum(weak_functions.get(str(fn), 0) for fn in item.get("requiredFunctions", []))
        level_score = weak_levels.get(str(item.get("levelId", "")), 0)
        return (fn_score * 3 + level_score, -len(item.get("requiredFunctions", [])), item.get("title", ""))

    candidates = [x for x in EXERCISES.values() if x["id"] not in completed]
    candidates.sort(key=score, reverse=True)
    recommended = candidates[:4]
    if not recommended:
        recommended = sorted(EXERCISES.values(), key=score, reverse=True)[:4]

    weak_list = [name for name, _ in sorted(weak_functions.items(), key=lambda kv: kv[1], reverse=True)[:5]]
    system = (
        "Anda adalah tutor Excel. Buat latihan tambahan yang ringkas untuk pemelajar Indonesia. "
        "Latihan harus dapat dikerjakan tanpa file tambahan: sertakan semua angka/data kecil yang diperlukan di teks soal. "
        "Jangan berikan jawaban akhir di task; hint boleh menyebut fungsi yang cocok."
    )
    prompt = json.dumps(
        {
            "weak_functions": weak_list,
            "recent_failed_exercise_ids": [str(a.get("id")) for a in recent if a.get("passed") is False][-8:],
            "recommended_existing": [
                {
                    "id": x["id"],
                    "title": x["title"],
                    "functions": x.get("requiredFunctions", []),
                    "difficulty": x.get("difficulty"),
                }
                for x in recommended
            ],
            "instruction": "Buat 3 latihan mini tambahan yang terutama melatih kelemahan pengguna. Bila belum ada kelemahan, fokuskan fondasi SUM/AVERAGE/IF/lookup secara bertahap.",
        },
        ensure_ascii=False,
    )

    try:
        ai = openai_structured("excel_recommendations", RECOMMEND_SCHEMA, system, prompt, max_tokens=900)
    except (RuntimeError, requests.RequestException, json.JSONDecodeError) as exc:
        return jsonify({"error": str(exc), "recommendedExerciseIds": [x["id"] for x in recommended]}), 503

    ai["recommendedExerciseIds"] = [x["id"] for x in recommended]
    ai["extra_drills"] = (ai.get("extra_drills") or [])[:3]

    user = current_user()
    with db() as conn:
        conn.execute(
            "INSERT INTO ai_activity(user_id, exercise_id, passed, kind, created_at) VALUES(?,?,?,?,?)",
            (user["id"] if user else None, None, None, "recommend", utcnow()),
        )
    return jsonify(ai)


@app.get("/api/progress")
def get_progress():
    user = current_user()
    if not user:
        return jsonify({"error": "Login diperlukan."}), 401
    with db() as conn:
        row = conn.execute("SELECT payload, updated_at FROM progress WHERE user_id = ?", (user["id"],)).fetchone()
    return jsonify({"progress": json.loads(row["payload"]) if row else None, "updatedAt": row["updated_at"] if row else None})


@app.put("/api/progress")
def put_progress():
    if not same_origin():
        return jsonify({"error": "Origin tidak diizinkan."}), 403
    user = current_user()
    if not user:
        return jsonify({"error": "Login diperlukan."}), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Progress tidak valid."}), 400
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 800_000:
        return jsonify({"error": "Progress terlalu besar."}), 413
    now = utcnow()
    with db() as conn:
        conn.execute(
            "INSERT INTO progress(user_id, payload, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (user["id"], encoded, now),
        )
    return jsonify({"saved": True, "updatedAt": now})


@app.get("/auth/openai")
def auth_openai():
    if not OPENAI_CLIENT_ID:
        return redirect("/?auth=not_configured#settings")

    browser_key = secrets.token_urlsafe(24)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    nonce = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + 600

    with db() as conn:
        conn.execute("DELETE FROM oauth_transactions WHERE expires_at < ?", (int(time.time()),))
        conn.execute(
            "INSERT OR REPLACE INTO oauth_transactions(browser_key,state,code_verifier,nonce,redirect_uri,expires_at) VALUES(?,?,?,?,?,?)",
            (browser_key, state, verifier, nonce, OPENAI_REDIRECT_URI, expires_at),
        )

    query = urlencode(
        {
            "client_id": OPENAI_CLIENT_ID,
            "redirect_uri": OPENAI_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    response = redirect(f"{OIDC_AUTHORIZATION_ENDPOINT}?{query}")
    response.set_cookie(
        "el_oauth_tx",
        browser_key,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
    )
    return response


@app.get("/auth/openai/callback")
def auth_openai_callback():
    browser_key = request.cookies.get("el_oauth_tx", "")
    with db() as conn:
        tx = conn.execute(
            "SELECT * FROM oauth_transactions WHERE browser_key = ?", (browser_key,)
        ).fetchone()
        if browser_key:
            conn.execute("DELETE FROM oauth_transactions WHERE browser_key = ?", (browser_key,))

    if (
        not tx
        or tx["expires_at"] < int(time.time())
        or request.args.get("state") != tx["state"]
        or request.args.get("error")
        or not request.args.get("code")
    ):
        response = redirect("/?auth=failed#settings")
        response.delete_cookie("el_oauth_tx", path="/")
        return response

    token_data = {
        "grant_type": "authorization_code",
        "code": request.args["code"],
        "redirect_uri": tx["redirect_uri"],
        "client_id": OPENAI_CLIENT_ID,
        "code_verifier": tx["code_verifier"],
    }
    token_headers = {"Accept": "application/json"}
    if OPENAI_TOKEN_AUTH_METHOD == "client_secret_basic":
        if not OPENAI_CLIENT_SECRET:
            return redirect("/?auth=secret_missing#settings")
        from urllib.parse import quote_plus
        basic_value = f"{quote_plus(OPENAI_CLIENT_ID)}:{quote_plus(OPENAI_CLIENT_SECRET)}"
        token_headers["Authorization"] = "Basic " + base64.b64encode(basic_value.encode("utf-8")).decode("ascii")

    try:
        token_response = requests.post(
            OIDC_TOKEN_ENDPOINT,
            data=token_data,
            headers=token_headers,
            timeout=20,
        )
        token_response.raise_for_status()
        id_token = token_response.json().get("id_token")
        if not isinstance(id_token, str):
            raise RuntimeError("ID token tidak tersedia.")

        header = jwt.get_unverified_header(id_token)
        alg = header.get("alg")
        if alg not in {"RS256", "PS256"}:
            raise RuntimeError("Algoritma ID token tidak didukung.")
        signing_key = jwt.PyJWKClient(OIDC_JWKS_URI).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=[alg],
            audience=OPENAI_CLIENT_ID,
            issuer=OIDC_ISSUER,
            options={"require": ["sub", "exp", "iat"]},
            leeway=5,
        )
        if claims.get("nonce") != tx["nonce"]:
            raise RuntimeError("Nonce tidak cocok.")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise RuntimeError("Subject akun tidak tersedia.")
    except Exception as exc:
        app.logger.warning("ChatGPT sign-in failed: %s", exc)
        response = redirect("/?auth=failed#settings")
        response.delete_cookie("el_oauth_tx", path="/")
        return response

    now = utcnow()
    with db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE provider_sub = ?", (sub,)).fetchone()
        if existing:
            user_id = existing["id"]
            conn.execute(
                "UPDATE users SET email=?, name=?, picture=?, updated_at=? WHERE id=?",
                (claims.get("email"), claims.get("name"), claims.get("picture"), now, user_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO users(provider_sub,email,name,picture,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (sub, claims.get("email"), claims.get("name"), claims.get("picture"), now, now),
            )
            user_id = cur.lastrowid

    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    response = redirect("/?auth=success#dashboard")
    response.delete_cookie("el_oauth_tx", path="/")
    return response


@app.post("/auth/logout")
def auth_logout():
    if not same_origin():
        return jsonify({"error": "Origin tidak diizinkan."}), 403
    session.clear()
    return jsonify({"signedOut": True})


PUBLIC_FILES = {
    "index.html",
    "app.mjs",
    "engine.mjs",
    "state.mjs",
    "styles.css",
    "favicon.svg",
    "workbook-worker.js",
}


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<filename>")
def public_root_file(filename: str):
    if filename not in PUBLIC_FILES:
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(BASE_DIR, filename)


@app.get("/data/<path:filename>")
def public_data(filename: str):
    return send_from_directory(BASE_DIR / "data", filename)


@app.get("/workbooks/<path:filename>")
def public_workbooks(filename: str):
    return send_from_directory(BASE_DIR / "workbooks", filename, as_attachment=False)


@app.get("/vendor/<path:filename>")
def public_vendor(filename: str):
    return send_from_directory(BASE_DIR / "vendor", filename)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
