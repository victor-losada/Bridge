"""Cifrado en reposo de passwords MT5 con Fernet.

La clave vive en FERNET_KEY. El plaintext solo existe en memoria
del Manager al arrancar un Worker, y en el env del proceso hijo.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CredentialCipher:
    def __init__(self, fernet_key: str) -> None:
        key = fernet_key.encode("utf-8") if isinstance(fernet_key, str) else fernet_key
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("No se pudo descifrar la credencial (FERNET_KEY incorrecta)") from exc

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("utf-8")
