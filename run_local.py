"""Lancement local pour developpement/demo (auth locale, ni TLS ni coffre)."""
import os

os.environ.setdefault("AUTH_MODE", "local")
os.environ.setdefault("EZ365_LOCAL_USERS", "testeur:motdepasse")
# Cle Fernet jetable, generee pour les tests : ce n'est PAS la cle de production.
os.environ.setdefault("STORAGE_KEY", "1HkwvGs9HefxF0GEAuFbvOte6qf9zLqPYnQkQpJCsJk=")
os.environ.setdefault("DATA_DIR", ".localdata")
os.environ.setdefault("MS_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
os.environ.setdefault("MS_CLIENT_SECRET", "secret-de-test")
os.environ.setdefault("MS_REDIRECT_URI", "http://localhost:8000/ms/callback")
os.environ.setdefault("VAULT_ENABLED", "false")
os.environ.setdefault("PORT", "8000")
os.environ["SSL_CERTFILE"] = ""
os.environ["SSL_KEYFILE"] = ""

from app.main import main

if __name__ == "__main__":
    main()
