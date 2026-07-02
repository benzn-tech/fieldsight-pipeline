"""Versioned .sql migration runner. No ORM; no psycopg import at module top."""
import os


def parse_version(filename: str) -> int:
    return int(filename.split("_", 1)[0])


def pending_versions(all_files: list[str], applied: set[str]) -> list[str]:
    todo = [f for f in all_files if f.endswith(".sql") and f not in applied]
    return sorted(todo, key=parse_version)
