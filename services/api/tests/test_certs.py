import socket
import ssl
import threading

from app.certs import generate


def test_generated_chain_passes_strict_tls_verification(tmp_path):
    generate(tmp_path, ["broker.example.com", "127.0.0.1"])

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(tmp_path / "server.crt", tmp_path / "server.key")
    client_ctx = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
    client_ctx.verify_flags |= ssl.VERIFY_X509_STRICT

    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        with server_ctx.wrap_socket(conn, server_side=True) as tls:
            tls.recv(1)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    with socket.create_connection(("127.0.0.1", port)) as raw:
        with client_ctx.wrap_socket(raw, server_hostname="broker.example.com") as tls:
            assert tls.version() in ("TLSv1.2", "TLSv1.3")
            tls.send(b"x")
    thread.join(2)
    listener.close()
