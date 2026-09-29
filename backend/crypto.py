from cryptography.fernet import Fernet

from backend.config import ENCRYPTION_KEY

_fernet = Fernet(ENCRYPTION_KEY.encode())


def chiffrer(valeur: str) -> str:
    return _fernet.encrypt(valeur.encode()).decode()


def dechiffrer(valeur_chiffree: str) -> str:
    return _fernet.decrypt(valeur_chiffree.encode()).decode()
