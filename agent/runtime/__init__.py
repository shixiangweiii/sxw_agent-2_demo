"""Canonical single-host Agent Runtime.

The package deliberately keeps domain/application code independent from ADK and
SQLite.  SQLite, filesystem artifacts and the three legacy reasoning engines are
adapters around the durable Runtime contract.
"""

