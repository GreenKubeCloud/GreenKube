# src/greenkube/storage/sqlite_repository.py
import sqlite3
import psycopg2
from .base_repository import CarbonIntensityRepository
from ..core.db import get_db_connection
from ..core.config import config

class SQLiteCarbonIntensityRepository(CarbonIntensityRepository):
    """
    Implémentation concrète du repository pour interagir avec une base de données
    SQLite ou PostgreSQL.
    """
    def __init__(self):
        self.db_connection = get_db_connection()

    def get_latest_for_zone(self, zone: str) -> float | None:
        """
        Récupère la dernière intensité carbone pour une zone depuis la BDD.
        """
        cursor = self.db_connection.cursor()
        query = "SELECT carbon_intensity FROM carbon_intensity WHERE zone = ? ORDER BY datetime DESC LIMIT 1;"
        try:
            cursor.execute(query, (zone,))
            result = cursor.fetchone()
            return result[0] if result else None
        finally:
            cursor.close()

    def save_history(self, history_data: list, zone: str) -> int:
        """
        Sauvegarde les données historiques pour une zone. C'est ici que la logique
        qui était dans le collector est maintenant centralisée.
        """
        if config.DB_TYPE == 'postgres':
            sql = """
            INSERT INTO carbon_intensity (
                zone, carbon_intensity, datetime, updated_at, created_at,
                emission_factor_type, is_estimated, estimation_method
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (datetime) DO NOTHING;
            """
        else: # sqlite
            sql = """
            INSERT OR IGNORE INTO carbon_intensity (
                zone, carbon_intensity, datetime, updated_at, created_at,
                emission_factor_type, is_estimated, estimation_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """

        data_to_insert = [
            (
                zone,
                item.get("carbonIntensity"),
                item.get("datetime"),
                item.get("updatedAt"),
                item.get("createdAt"),
                item.get("emissionFactorType"),
                item.get("isEstimated"),
                item.get("estimationMethod"),
            )
            for item in history_data
        ]
        
        cursor = None
        try:
            cursor = self.db_connection.cursor()
            cursor.executemany(sql, data_to_insert)
            newly_inserted_rows = cursor.rowcount
            self.db_connection.commit()
            return newly_inserted_rows
        except (sqlite3.Error, psycopg2.Error) as e:
            print(f"Database error during data insertion: {e}")
            self.db_connection.rollback()
            return 0
        finally:
            if cursor:
                cursor.close()
