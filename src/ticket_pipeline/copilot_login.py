#!/usr/bin/env python3
"""
copilot_login - one-time interactive device-flow authorization for the
GitHub Copilot provider in ai_client.py.

There's no `gh` CLI on this machine and no plaintext token to read out
of the VS Code Copilot extension's own credential store (an internal,
likely-encrypted sqlite db not meant to be scraped by other tools) - so
this gets its own GitHub OAuth token the same way editor Copilot
integrations do: GitHub's standard OAuth device flow, using the same
public client_id long-relied-on by open-source Copilot clients
(copilot.vim, copilot.lua, various Copilot proxies). This is normal,
documented OAuth device-flow usage, not a credential-extraction trick -
you authorize it yourself in your browser.

The resulting long-lived OAuth token is cached at
ai_client.COPILOT_OAUTH_TOKEN_FILE; ai_client exchanges it for a
short-lived Copilot session token (and adds Copilot's required headers)
on every actual chat-completions call - see _copilot_auth_headers.

Run once: `python copilot_login.py`. Re-run any time the cached token
stops working (revoked, expired, scope changed).
"""

import json
import sys
import time
import urllib.error
import urllib.request

from .lib import ai_client

# The public OAuth app client_id GitHub Copilot's own editor integrations
# use for device-flow auth - not a secret, the same id widely embedded in
# open-source Copilot clients.
CLIENT_ID = "Iv1.b507a08c87ecfe98"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"


def _post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main() -> None:
    device = _post_json(DEVICE_CODE_URL, {"client_id": CLIENT_ID, "scope": "read:user"})

    print(f"Go to: {device['verification_uri']}")
    print(f"Enter code: {device['user_code']}")
    print("Waiting for authorization ...")

    interval = device.get("interval", 5)
    deadline = time.time() + device.get("expires_in", 900)

    while time.time() < deadline:
        time.sleep(interval)
        try:
            result = _post_json(
                ACCESS_TOKEN_URL,
                {
                    "client_id": CLIENT_ID,
                    "device_code": device["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
        except urllib.error.HTTPError as e:
            print(f"error: token request failed: HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
            sys.exit(1)

        error = result.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = result.get("interval", interval + 5)
            continue
        if error:
            print(f"error: {error}: {result.get('error_description', '')}", file=sys.stderr)
            sys.exit(1)

        token = result["access_token"]
        ai_client.COPILOT_OAUTH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        ai_client.COPILOT_OAUTH_TOKEN_FILE.write_text(token, encoding="utf-8")
        print(f"Authorized. Token cached at {ai_client.COPILOT_OAUTH_TOKEN_FILE}.")
        return

    print("error: authorization timed out - run this again.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
