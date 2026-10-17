"""Small TLS and local-peer checks for the workbench HTTP server."""

import ipaddress
import ssl
from pathlib import Path


def wrap_server_tls(server, certfile, keyfile):
    """Wrap the already-bound server socket; invalid credentials abort startup."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(Path(certfile)), keyfile=str(Path(keyfile)))
    # On a listening socket, handshaking during accept() would block the
    # serve_forever loop behind a peer that connects but sends no TLS bytes.
    # Defer it to the request worker, where Handler.setup sets a timeout.
    server.socket = context.wrap_socket(
        server.socket, server_side=True, do_handshake_on_connect=False
    )
    return server


def is_local_request(hostname, client_ip):
    """Require both a literal local Host name and an actual loopback peer."""
    if hostname not in {'localhost', '127.0.0.1', '::1', '[::1]'}:
        return False
    try:
        return ipaddress.ip_address(client_ip) in {
            ipaddress.ip_address('127.0.0.1'), ipaddress.ip_address('::1')
        }
    except ValueError:
        return False
