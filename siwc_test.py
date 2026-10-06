from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
import requests

APP_NAME = "Excel Lab"
CONFIG_DIR = Path.home() / ".config" / "excel-lab"
HOST_ID_FILE = CONFIG_DIR / "host-id"
AUTH_FILE = CONFIG_DIR / "chatgpt-auth.json"
PENDING_CLIENT_FILE = CONFIG_DIR / "pending-client.json"

AUTH_ENDPOINT = "https://auth.openai.com/api/accounts/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
JWKS_URI = "https://auth.openai.com/.well-known/jwks.json"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"

CALLBACK_PATH = "/auth/callback"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def write_private_json(path: Path, payload: dict) -> None:
    ensure_config_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_host_id() -> str:
    ensure_config_dir()
    if HOST_ID_FILE.exists():
        value = HOST_ID_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = f"urn:uuid:{uuid.uuid4()}"
    HOST_ID_FILE.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(HOST_ID_FILE, 0o600)
    except OSError:
        pass
    return value


def load_json_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def load_auth() -> dict | None:
    return load_json_file(AUTH_FILE)


def load_pending_client() -> dict | None:
    return load_json_file(PENDING_CLIENT_FILE)


class CallbackHandler(BaseHTTPRequestHandler):
    result: dict | None = None
    event: threading.Event | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return

        CallbackHandler.result = {
            key: values[0] if values else ""
            for key, values in parse_qs(parsed.query).items()
        }

        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Excel Lab</title></head><body style='font-family:-apple-system,sans-serif;padding:40px'>"
            "<h2>Excel Lab sudah menerima login ChatGPT.</h2>"
            "<p>Kamu boleh menutup tab ini dan kembali ke Terminal.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

        if CallbackHandler.event:
            CallbackHandler.event.set()

    def log_message(self, format: str, *args) -> None:
        return


def validate_id_token(id_token: str, client_id: str, nonce: str) -> dict:
    header = jwt.get_unverified_header(id_token)
    alg = header.get("alg")
    if alg not in {"RS256", "PS256"}:
        raise RuntimeError(f"Algoritma ID token tidak didukung: {alg}")

    key = jwt.PyJWKClient(JWKS_URI).get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        key.key,
        algorithms=[alg],
        audience=client_id,
        issuer=ISSUER,
        options={"require": ["sub", "exp", "iat"]},
        leeway=5,
    )
    if claims.get("nonce") != nonce:
        raise RuntimeError("Nonce ID token tidak cocok.")
    return claims


def login() -> dict:
    existing = load_auth()
    pending = load_pending_client()
    saved_client_id = (existing or {}).get("client_id") or (pending or {}).get("client_id")
    initial_registration = not bool(saved_client_id)
    requested_client_id = "dynamic_agent_client" if initial_registration else saved_client_id

    host_id = get_host_id()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())

    CallbackHandler.result = None
    callback_event = threading.Event()
    CallbackHandler.event = callback_event
    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    params = {
        "client_id": requested_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "ext_agent_host_id": host_id,
    }
    if initial_registration:
        params["agent_name_hint"] = APP_NAME
    else:
        id_token_hint = (existing or {}).get("id_token")
        login_hint = (existing or {}).get("email")
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        if login_hint:
            params["login_hint"] = login_hint

    auth_url = AUTH_ENDPOINT + "?" + urlencode(params)

    print("Membuka browser untuk Continue with ChatGPT...")
    print("Kalau browser tidak terbuka otomatis, copy URL berikut secara manual:")
    print(auth_url)
    webbrowser.open(auth_url)

    if not callback_event.wait(timeout=300):
        server.shutdown()
        raise RuntimeError("Login timeout setelah 5 menit.")

    server.shutdown()
    result = CallbackHandler.result or {}

    if result.get("state") != state:
        raise RuntimeError("State OAuth tidak cocok.")
    if result.get("error"):
        raise RuntimeError(f"Login dibatalkan/gagal: {result.get('error')}")
    code = result.get("code")
    if not code:
        raise RuntimeError("Authorization code tidak diterima.")

    returned_client_id = result.get("client_id")
    if initial_registration:
        if not returned_client_id or returned_client_id == "dynamic_agent_client":
            raise RuntimeError("OpenAI tidak mengembalikan issued client_id.")
        client_id = returned_client_id
        write_private_json(
            PENDING_CLIENT_FILE,
            {
                "client_id": client_id,
                "ext_agent_host_id": host_id,
                "saved_at": utcnow(),
            },
        )
    else:
        client_id = saved_client_id
        if returned_client_id and returned_client_id != client_id:
            raise RuntimeError("Client ID callback berbeda dari client ID tersimpan.")

    token_response = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": RESOURCE,
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if not token_response.ok:
        try:
            error_payload = token_response.json()
        except ValueError:
            error_payload = {}
        error_code = error_payload.get("error")
        if error_code == "invalid_grant" and PENDING_CLIENT_FILE.exists():
            raise RuntimeError(
                "Token exchange mengembalikan invalid_grant. Issued client ID sudah disimpan. "
                "Jalankan lagi: python siwc_test.py login"
            )
        raise RuntimeError(
            f"Token exchange gagal ({token_response.status_code}): "
            f"{token_response.text[:500]}"
        )

    token = token_response.json()
    id_token = token.get("id_token")
    if not isinstance(id_token, str):
        raise RuntimeError("ID token tidak tersedia.")

    claims = validate_id_token(id_token, client_id, nonce)
    scopes = str(token.get("scope") or result.get("scope") or "").split()
    if "chatgpt.tokens.use.direct" not in scopes:
        raise RuntimeError(
            "Login berhasil, tetapi izin memakai paket ChatGPT tidak diberikan."
        )

    saved = {
        "email": claims.get("email"),
        "name": claims.get("name"),
        "issuer": ISSUER,
        "subject": claims["sub"],
        "client_id": client_id,
        "ext_agent_host_id": host_id,
        "id_token": id_token,
        "access_token": token.get("access_token"),
        "refresh_token": token.get("refresh_token"),
        "token_type": token.get("token_type", "Bearer"),
        "expires_in": int(token.get("expires_in", 3600)),
        "expires_at": int(time.time()) + int(token.get("expires_in", 3600)),
        "earliest_refresh_at": token.get("earliest_refresh_at"),
        "scopes": scopes,
        "saved_at": utcnow(),
    }
    if not saved["access_token"] or not saved["refresh_token"]:
        raise RuntimeError("Access token atau refresh token tidak tersedia.")

    write_private_json(AUTH_FILE, saved)
    try:
        PENDING_CLIENT_FILE.unlink()
    except FileNotFoundError:
        pass

    print()
    print("LOGIN CHATGPT: OK")
    print("Akun:", saved.get("email") or saved.get("name") or saved["subject"])
    print("Client ID:", saved["client_id"])
    print("ChatGPT plan usage: AKTIF")
    print("Credential tersimpan aman di:", AUTH_FILE)
    return saved


def refresh_if_needed(auth: dict) -> dict:
    if int(auth.get("expires_at", 0)) > int(time.time()) + 90:
        return auth

    refresh_token = auth.get("refresh_token")
    client_id = auth.get("client_id")
    if not refresh_token or not client_id:
        raise RuntimeError("Credential refresh tidak lengkap. Jalankan login lagi.")

    response = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "resource": RESOURCE,
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"Refresh token gagal ({response.status_code}): {response.text[:500]}"
        )

    token = response.json()
    updated = dict(auth)
    updated["access_token"] = token.get("access_token")
    updated["refresh_token"] = token.get("refresh_token")
    updated["id_token"] = token.get("id_token") or auth.get("id_token")
    updated["token_type"] = token.get("token_type", "Bearer")
    updated["expires_in"] = int(token.get("expires_in", 3600))
    updated["expires_at"] = int(time.time()) + updated["expires_in"]
    updated["earliest_refresh_at"] = token.get("earliest_refresh_at")
    if token.get("scope"):
        updated["scopes"] = str(token["scope"]).split()
    updated["saved_at"] = utcnow()

    if not updated.get("access_token") or not updated.get("refresh_token"):
        raise RuntimeError("Refresh response tidak lengkap.")

    write_private_json(AUTH_FILE, updated)
    return updated


def list_models(access_token: str) -> list[dict]:
    response = requests.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"Model list gagal ({response.status_code}): {response.text[:500]}"
        )
    payload = response.json()
    models = payload.get("models")
    if not isinstance(models, list):
        models = payload.get("data") or []
    return models


def choose_model(models: list[dict]) -> str:
    visible = []
    for item in models:
        slug = item.get("slug") or item.get("id")
        if not slug:
            continue
        if item.get("visibility") in (None, "list"):
            visible.append((slug, item.get("display_name") or slug))

    if not visible:
        raise RuntimeError("Tidak ada model yang tersedia untuk akun ini.")

    print("\nModel ChatGPT yang tersedia:")
    for slug, display in visible[:20]:
        print(f"- {display} ({slug})")

    slugs = [slug for slug, _ in visible]
    preferred = os.getenv("CHATGPT_MODEL", "gpt-6.1-sol")
    return preferred if preferred in slugs else slugs[0]


def run_inference() -> None:
    auth = load_auth()
    if not auth:
        raise RuntimeError("Belum ada credential. Jalankan: python siwc_test.py login")

    if "chatgpt.tokens.use.direct" not in auth.get("scopes", []):
        raise RuntimeError("Credential tidak memiliki izin ChatGPT plan usage.")

    auth = refresh_if_needed(auth)
    token = auth["access_token"]
    models = list_models(token)
    model = choose_model(models)

    print("\nModel dipakai:", model)
    print("Tes jawaban AI:")

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        json={
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": "Balas persis dengan kalimat: Excel Lab siap.",
                }
            ],
            "store": False,
            "stream": True,
        },
        stream=True,
        timeout=120,
    )
    if not response.ok:
        raise RuntimeError(
            f"Inference gagal ({response.status_code}): {response.text[:800]}"
        )

    completed = False
    failed = None
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line or not raw_line.startswith("data:"):
            continue
        data = raw_line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            print(event.get("delta", ""), end="", flush=True)
        elif event_type == "response.completed":
            completed = True
        elif event_type in {"response.failed", "response.incomplete"}:
            failed = event

    print()
    if failed:
        raise RuntimeError(f"Response tidak selesai: {json.dumps(failed)[:1000]}")
    if not completed:
        raise RuntimeError("Stream berakhir tanpa response.completed.")

    print("CHATGPT PLUS INFERENCE: OK")


def show_status() -> None:
    auth = load_auth()
    if not auth:
        print("Belum login ChatGPT.")
        return
    print("Credential:", AUTH_FILE)
    print("Akun:", auth.get("email") or auth.get("name") or auth.get("subject"))
    print("Client ID:", auth.get("client_id"))
    print(
        "ChatGPT plan usage:",
        "AKTIF" if "chatgpt.tokens.use.direct" in auth.get("scopes", []) else "TIDAK AKTIF",
    )
    print("Access token berlaku sampai epoch:", auth.get("expires_at"))


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "login":
        login()
    elif command == "test":
        run_inference()
    elif command == "status":
        show_status()
    else:
        print("Pakai salah satu:")
        print("  python siwc_test.py login")
        print("  python siwc_test.py test")
        print("  python siwc_test.py status")
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDibatalkan.")
        raise SystemExit(130)
    except Exception as exc:
        print("\nERROR:", exc)
        raise SystemExit(1)
