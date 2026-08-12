"""
CI-level bypass guard for the outbound send gate.

Statically audits every Python file under app/ and fails if any module
outside the sanctioned transport/wrapper layer could reach the Meta/IG
message-send APIs directly. This is what makes send_gate.check_send() a
100% choke point: new code that tries to bypass it turns the build red.

Three independent checks (AST-based, so comments/docstrings can't
false-positive):
  1. No call/reference to a `_raw_*` transport function outside
     whatsapp_service.py / instagram_service.py / outbound.py.
  2. No call to a `send_*`/`reply_to_comment` attribute of
     whatsapp_service/instagram_service outside outbound.py (the old public
     names no longer exist; this catches reintroduction too).
  3. No module outside the transport layer builds a Graph API /messages URL
     (string-literal scan for the graph host in files that aren't
     whitelisted read-only users of the Graph API).
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# The only modules allowed to reference the raw transport functions.
TRANSPORT_MODULES = {
    "app/services/whatsapp_service.py",
    "app/services/instagram_service.py",
    "app/services/outbound.py",
}

# Files that legitimately contain the Graph host for NON-messaging calls
# (media download, token debug, channel setup reads). None of them may send.
GRAPH_URL_READONLY_WHITELIST = {
    "app/services/vision_service.py",     # media download (GET)
    "app/main.py",                        # debug_token (GET)
    "app/routers/channels.py",            # channel setup reads
    "app/routers/integrations.py",        # integration setup reads
}

CHANNEL_SERVICE_NAMES = {"whatsapp_service", "instagram_service"}
SEND_ATTR_PREFIXES = ("send_", "reply_to_comment")


def _app_py_files() -> list[Path]:
    """All .py files under app/, stable order for readable failure output."""
    return sorted(APP_DIR.rglob("*.py"))


def _rel(path: Path) -> str:
    """Path relative to the backend root, POSIX-style, for whitelist matching."""
    return path.relative_to(APP_DIR.parent).as_posix()


def test_no_raw_send_outside_transport_layer():
    """`_raw_*` transport functions may only appear in the transport layer."""
    offenders: list[str] = []
    for path in _app_py_files():
        rel = _rel(path)
        if rel in TRANSPORT_MODULES:
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name.startswith("_raw_"):
                        offenders.append(f"{rel}:{node.lineno} imports {alias.name}")
                continue
            if name and name.startswith("_raw_") and (
                name.startswith("_raw_send") or name.startswith("_raw_reply")
            ):
                offenders.append(f"{rel}:{node.lineno} references {name}")
    assert not offenders, (
        "Raw Meta/IG transport functions referenced outside the gated "
        "outbound layer — route these through app/services/outbound.py:\n"
        + "\n".join(offenders)
    )


def test_no_channel_service_send_calls_outside_outbound():
    """No module but outbound.py may call send attrs on the channel services."""
    offenders: list[str] = []
    for path in _app_py_files():
        rel = _rel(path)
        if rel in TRANSPORT_MODULES:
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value
            base_name = base.id if isinstance(base, ast.Name) else (
                base.attr if isinstance(base, ast.Attribute) else None
            )
            if base_name in CHANNEL_SERVICE_NAMES and node.attr.startswith(SEND_ATTR_PREFIXES):
                offenders.append(f"{rel}:{node.lineno} calls {base_name}.{node.attr}")
    assert not offenders, (
        "Direct channel-service send calls found outside outbound.py — "
        "these bypass send_gate.check_send():\n" + "\n".join(offenders)
    )


def test_no_graph_send_url_outside_transport_layer():
    """
    The Graph API host may not appear outside the transport layer and the
    read-only whitelist — and whitelisted read-only files must not POST to a
    /messages endpoint.
    """
    offenders: list[str] = []
    for path in _app_py_files():
        rel = _rel(path)
        if rel in TRANSPORT_MODULES:
            continue
        text = path.read_text()
        if "graph.facebook.com" not in text and "graph.instagram.com" not in text:
            continue
        if rel not in GRAPH_URL_READONLY_WHITELIST:
            offenders.append(f"{rel}: contains Graph API host outside transport layer")
            continue
        if "/messages" in text:
            offenders.append(f"{rel}: read-only whitelisted file references a /messages endpoint")
    assert not offenders, (
        "Possible direct Graph API messaging path outside the gated "
        "transport layer:\n" + "\n".join(offenders)
    )
