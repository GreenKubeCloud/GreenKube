# src/greenkube/storage/sqlite_repository.py
import sqlite3
from .base_repository import CarbonIntensityRepository

class SQLiteCarbonIntensityRepository(CarbonIntensityRepository):
    """
    Implementation of the repository for SQLite.
    Handles all database interactions for carbon intensity data.
    """
    # CORRECTION: Le constructeur accepte maintenant une connexion en argument.
    def __init__(self, connection):
        """
        Initializes the repository with a database connection.

        Args:
            connection: An active sqlite3 connection object.
        """
        self.conn = connection

    def get_for_zone_at_time(self, zone: str, timestamp: str) -> float | None:
        """
        Retrieves the latest carbon intensity for a given zone at or before a specific timestamp.
        """
        cursor = self.conn.cursor()
        query = """
            SELECT carbon_intensity 
            FROM carbon_intensity_history 
            WHERE zone = ? AND datetime <= ?
            ORDER BY datetime DESC 
            LIMIT 1
        """
        cursor.execute(query, (zone, timestamp))
        result = cursor.fetchone()
        return result[0] if result else None

    def save_history(self, history_data: list, zone: str) -> int:
        """
        Saves historical carbon intensity data to the SQLite database.
        It ignores records that would be duplicates based on zone and datetime.
        """
        if not self.conn:
            print("ERROR: SQLite connection is not available.")
            return 0
            
        cursor = self.conn.cursor()
        saved_count = 0
        
        for record in history_data:
            try:
                # La clause IGNORE empêche l'insertion si la contrainte UNIQUE (zone, datetime) est violée
                cursor.execute("""
                    INSERT INTO carbon_intensity_history 
                        (zone, carbon_intensity, datetime, updated_at, created_at, 
                         emission_factor_type, is_estimated, estimation_method) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(zone, datetime) DO NOTHING;
                """, (
                    zone,
                    record.get('carbonIntensity'),
                    record.get('datetime'),
                    record.get('updatedAt'),
                    record.get('createdAt'),
                    record.get('emissionFactorType'),
                    record.get('isEstimated'),
                    record.get('estimationMethod')
                ))
                # rowcount sera 1 si une ligne a été insérée, 0 sinon
                saved_count += cursor.rowcount
            except sqlite3.Error as e:
                print(f"ERROR: Could not save record for zone {zone} at {record.get('datetime')}: {e}")

        self.conn.commit()
        return saved_count

