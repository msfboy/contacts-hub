import socket
import threading
import time
import webbrowser

import app as contactshub_app


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _open_browser_soon(url: str) -> None:
    time.sleep(1.2)
    webbrowser.open(url)


if __name__ == "__main__":
    url = "http://127.0.0.1:5050"
    if _port_open("127.0.0.1", 5050):
        print(f"ContactsHub est déjà actif sur {url} — ouverture du navigateur...")
        webbrowser.open(url)
    else:
        print(f"Démarrage de ContactsHub sur {url}...")
        threading.Thread(target=_open_browser_soon, args=(url,), daemon=True).start()
        contactshub_app.app.run(host="127.0.0.1", port=5050, debug=False)
