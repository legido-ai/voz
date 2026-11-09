"""Create (if needed) and push legido-ai/voz, with the App token kept in memory."""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

APP_ID = os.environ["GITHUB_APP_ID"]
INSTALLATION = os.environ["GITHUB_INSTALLATION_ID"]
PRIVATE_KEY = os.environ["GITHUB_PRIVATE_KEY"].replace("\\n", "\n")
REPO = "legido-ai/voz"
DIR = "/opt/data/workspace/voz"
BRANCH = "main"


def b64(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt() -> str:
    now = int(time.time())
    header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": APP_ID}).encode())
    key = serialization.load_pem_private_key(PRIVATE_KEY.encode(), password=None)
    signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{b64(signature)}"


def api(path: str, method: str = "GET", body: dict | None = None, token: str = "") -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"https://api.github.com{path}", data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "voz-deploy",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def main() -> int:
    status, body = api(f"/repos/{REPO}", token=app_jwt())
    if status == 200:
        token = api(f"/app/installations/{INSTALLATION}/access_tokens", "POST",
                    token=app_jwt())[1]["token"]
        print(f"repo exists: private={body['private']}")
    else:
        token = api(f"/app/installations/{INSTALLATION}/access_tokens", "POST",
                    token=app_jwt())[1]["token"]
        status, body = api("/orgs/legido-ai/repos", "POST",
                           {"name": "voz", "private": True,
                            "description": "Conversación por voz con OpenCode Go"}, token)
        print(f"repo created: {status} private={body.get('private')}")
        if status not in (200, 201):
            return 1

    token = api(f"/app/installations/{INSTALLATION}/access_tokens", "POST",
                token=app_jwt())[1]["token"]

    workdir = tempfile.mkdtemp(prefix="voz-push-")
    askpass = os.path.join(workdir, "askpass")
    secret = os.path.join(workdir, "token")
    with open(askpass, "w") as handle:
        handle.write('#!/bin/bash\ncase "$1" in\n  *Username*) echo x-access-token ;;\n'
                     '  *Password*) cat "$VOZ_GIT_TOKEN_FILE" ;;\nesac\n')
    os.chmod(askpass, 0o700)
    with open(secret, "w") as handle:
        handle.write(token)
    os.chmod(secret, 0o600)

    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "BAO_"))}
    env.update({"GIT_ASKPASS": askpass, "GIT_TERMINAL_PROMPT": "0",
                "VOZ_GIT_TOKEN_FILE": secret})

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=DIR, env=env, capture_output=True, text=True)

    def identity() -> tuple[str, str]:
        return ("Javier", "javi@legido.com")

    name, email = identity()
    steps = [
        ("init", ["init", "-b", BRANCH]) if not os.path.isdir(f"{DIR}/.git") else None,
        ("remote", ["remote", "set-url", "origin", f"https://github.com/{REPO}.git"]
         if git("remote"). stdout.strip() else
         ["remote", "add", "origin", f"https://github.com/{REPO}.git"]),
        ("add", ["add", "-A"]),
        ("commit", ["-c", f"user.name={name}", "-c", f"user.email={email}",
                    "commit", "-m", "feat(voz): voz 0.1.0 — conversación por voz con OpenCode Go"]),
        ("push", ["push", "-u", "origin", BRANCH]),
    ]
    for label, args in steps:
        if args is None:
            continue
        result = git(*args)
        out = (result.stdout + result.stderr).replace(token, "[REDACTED]")
        print(f"{label}: rc={result.returncode} {out.strip()[:200]}")
        if result.returncode != 0 and label not in ("commit",):
            return 1

    head = git("rev-parse", "HEAD").stdout.strip()
    status, ref = api(f"/repos/{REPO}/git/refs/heads/{BRANCH}", token=token)
    print(f"remote ref: {status} sha={ref.get('object', {}).get('sha', '?')[:12]} local={head[:12]} match={ref.get('object', {}).get('sha') == head}")
    os.remove(askpass)
    os.remove(secret)
    return 0


if __name__ == "__main__":
    sys.exit(main())
