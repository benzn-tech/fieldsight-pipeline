"""Thin repository functions. They execute SQL but NEVER commit —
the caller owns the transaction (see db.connection.get_connection)."""
