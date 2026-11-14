from pathlib import Path
from app.db import engine


def main():
    sql = (Path(__file__).resolve().parent.parent / "sql" / "init.sql").read_text(encoding="utf-8")
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute(sql)
        raw.commit()
        print("SportIQ database schema initialized.")
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


if __name__ == "__main__":
    main()
