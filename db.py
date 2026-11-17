import sqlite3
from datetime import date

import config


def _connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                fact TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                content TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Vérification de l'ancienne structure
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }

        # Ajout de la colonne manquante sans supprimer les anciennes données
        if "content" not in columns:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN content TEXT NOT NULL DEFAULT ''"
            )

            # Récupération des anciens messages si la colonne existe
            for old_column in ("message", "text", "message_text"):
                if old_column in columns:
                    conn.execute(
                        f'UPDATE messages SET content = "{old_column}"'
                    )
                    break

        if "role" not in columns:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"
            )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS allowed_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER NOT NULL,
                usage_type TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, usage_type, usage_date)
            )
        """)

def get_facts(user_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT fact FROM facts WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, config.MAX_FACTS_PER_USER),
        ).fetchall()
    return [row["fact"] for row in rows]


def add_facts(user_id, facts):
    cleaned = []
    for fact in facts:
        fact = str(fact).strip()[:config.MAX_FACT_LENGTH]
        if fact and fact not in cleaned:
            cleaned.append(fact)

    with _connect() as conn:
        existing = {
            row["fact"]
            for row in conn.execute(
                "SELECT fact FROM facts WHERE user_id = ?", (user_id,)
            ).fetchall()
        }
        for fact in cleaned:
            if fact not in existing:
                conn.execute(
                    "INSERT INTO facts (user_id, fact) VALUES (?, ?)",
                    (user_id, fact),
                )


def delete_fact(user_id, information):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, fact FROM facts WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        matches = [
            row for row in rows
            if information.lower() in row["fact"].lower()
        ]

        if len(matches) == 0:
            return "not_found", None

        if len(matches) > 1:
            return "ambiguous", None

        conn.execute(
            "DELETE FROM facts WHERE id = ? AND user_id = ?",
            (matches[0]["id"], user_id),
        )
        return "deleted", matches[0]["fact"]


def get_history(user_id, limit=None):
    limit = limit or config.MAX_HISTORY_MESSAGES
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE user_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [(row["role"], row["content"]) for row in reversed(rows)]


def add_exchange(user_id, user_message, assistant_message):
    with _connect() as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }

        def save_message(role, content):
            values = {
                "user_id": user_id,
                "role": role,
                "content": str(content),
                "text": str(content),
                "message": str(content),
            }

            fields = [name for name in values if name in columns]
            sql_fields = ", ".join(f'"{name}"' for name in fields)
            placeholders = ", ".join("?" for _ in fields)

            conn.execute(
                f"INSERT INTO messages ({sql_fields}) VALUES ({placeholders})",
                [values[name] for name in fields],
            )

        save_message("user", user_message)
        save_message("model", assistant_message)

        conn.execute(
            """
            DELETE FROM messages
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM messages
                WHERE user_id = ?
                ORDER BY id DESC LIMIT ?
            )
            """,
            (user_id, user_id, config.STORED_MESSAGES_PER_USER),
        )
def clear_history(user_id):
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))


def get_allowed_channels(guild_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT channel_id FROM allowed_channels WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
    return [row["channel_id"] for row in rows]


def add_allowed_channel(guild_id, channel_id):
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id)
            VALUES (?, ?)
            """,
            (guild_id, channel_id),
        )


def remove_allowed_channel(guild_id, channel_id):
    with _connect() as conn:
        cursor = conn.execute(
            """
            DELETE FROM allowed_channels
            WHERE guild_id = ? AND channel_id = ?
            """,
            (guild_id, channel_id),
        )
        return cursor.rowcount > 0


def clear_allowed_channels(guild_id):
    with _connect() as conn:
        conn.execute(
            "DELETE FROM allowed_channels WHERE guild_id = ?", (guild_id,)
        )


def get_usage(user_id, usage_type):
    today = date.today().isoformat()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT amount FROM usage
            WHERE user_id = ? AND usage_type = ? AND usage_date = ?
            """,
            (user_id, usage_type, today),
        ).fetchone()
    return row["amount"] if row else 0


def add_usage(user_id, usage_type, amount=1):
    today = date.today().isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO usage (user_id, usage_type, usage_date, amount)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, usage_type, usage_date)
            DO UPDATE SET amount = amount + excluded.amount
            """,
            (user_id, usage_type, today, amount),
        )