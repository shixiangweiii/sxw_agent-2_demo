"""Canonical single-host Agent Runtime.

The package deliberately keeps domain/application code independent from ADK and
SQLite. SQLite, filesystem artifacts, two ADK engines and the direct Native
adapter surround the durable Runtime contract.
"""
