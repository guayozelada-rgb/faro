"""Base de datos local cifrada del perfil (SQLCipher 4, ADR 0009).

Todo acceso pasa por `connection.py`; ningún otro módulo importa `sqlcipher3`.
"""
