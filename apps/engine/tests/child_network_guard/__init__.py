"""Carpeta que `tests/conftest.py` pone en el `PYTHONPATH` de los procesos hijos (ver su
`sitecustomize.py`). Este `__init__.py` solo existe para que las herramientas (mypy) no
confundan su `sitecustomize` con el de `scripts/pyinstaller_offline`; Python no lo usa."""
