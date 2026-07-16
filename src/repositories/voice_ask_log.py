"""voice_ask_log writes (SP-Ask audit). One row per voice ask. The caller owns
the transaction (see db/connection.py) -- this never commits."""


def insert_voice_ask(conn, caller_sub, transcript, answer, company_id=None):
    """Insert one audit row and return the new id (str). company_id may be None
    when the caller isn't provisioned -- the row is still recorded."""
    row = conn.execute(
        "INSERT INTO voice_ask_log (company_id, caller_sub, transcript, answer) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (company_id, caller_sub, transcript, answer),
    ).fetchone()
    return str(row[0])
