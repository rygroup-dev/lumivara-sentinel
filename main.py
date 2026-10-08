"""Entrypoint: run `python main.py` (or use run.bat on Windows)."""
# Trust the OS certificate store (needed behind HTTPS-inspecting proxies).
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from lumivara.app import main

if __name__ == "__main__":
    main()
