import socket

for port in (5432, 8086, 8113, 8000, 6333):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        print(port, "-> TCP ok")
        s.close()
    except Exception as err:
        print(port, "-> fail:", type(err).__name__, err)
