"""Point d'entree du conteneur.

Passer par ce fichier plutot que par `python -m app.main` evite qu'uvicorn
reimporte le module sous un second nom (`__main__` puis `app.main`) et cree
ainsi deux instances de l'application.
"""
from app.main import main

if __name__ == "__main__":
    main()
