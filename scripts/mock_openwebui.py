"""
Mock OpenWebUI Admin API server for testing LiteLLM OpenWebUI integration.

Usage:
    python3 scripts/mock_openwebui.py [--port 15000]

The mock maps openwebui_uuid → keycloak_sub.
Edit OPENWEBUI_USERS below to match your test scenarios.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# openwebui_uuid → keycloak_sub (must match user_id in users.db)
OPENWEBUI_USERS = {
    #"openwebui-test-user-aaa": "c31f90f3-b99f-4c2e-91e4-4e7776e2b995",  # dept A, fast-qwen (unblocked)
    "openwebui-test-user-aaa": "9c16b010-466e-46bc-a979-03db4c3161dd",  # dept A, fast-qwen (unblocked)
    #"openwebui-test-user-bbb": "ff3eb1bd-ba66-42b4-82ff-166895108c03",  # dept B, no models (unblocked)
    "openwebui-test-user-bbb": "7236a7ed-59e1-4851-b2c5-19e2a22f95a7",  # dept B, no models (unblocked)
    "openwebui-unknown-user":  None,  # user exists in OpenWebUI but not in LiteLLM DB
}


class MockOpenWebUIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[mock-openwebui] {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        # match /api/v1/users/<id>
        parts = parsed.path.strip("/").split("/")
        if parts[:3] == ["api", "v1", "users"] and len(parts) == 4:
            openwebui_id = parts[3]
            self._handle_user(openwebui_id)
        else:
            self._send(404, {"detail": "Not Found"})

    def _handle_user(self, openwebui_id: str):
        if openwebui_id not in OPENWEBUI_USERS:
            self._send(404, {"detail": f"User {openwebui_id!r} not found"})
            return

        keycloak_sub = OPENWEBUI_USERS[openwebui_id]
        if keycloak_sub is None:
            # user has no oidc sub (e.g. not SSO user)
            self._send(200, {"id": openwebui_id, "oauth": {}})
            return

        self._send(200, {
            "id": openwebui_id,
            "name": f"Test User ({openwebui_id[:8]})",
            "email": "test@example.com",
            "oauth": {
                "oidc": {
                    "sub": keycloak_sub,
                    "preferred_username": "testuser",
                }
            },
        })

    def _send(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=15000)
    args = parser.parse_args()

    server = HTTPServer(("0.0.0.0", args.port), MockOpenWebUIHandler)
    print(f"[mock-openwebui] listening on :{args.port}")
    print(f"[mock-openwebui] registered users: {list(OPENWEBUI_USERS.keys())}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock-openwebui] stopped")


if __name__ == "__main__":
    main()
