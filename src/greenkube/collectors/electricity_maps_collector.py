# src/greenkube/collectors/electricity_maps_collector.py

import requests
import psycopg2
import sqlite3
from .base_collector import BaseCollector
from ..core.config import config
from ..core.db import get_db_connection

API_BASE_URL = "https://api.electricitymaps.com/v3"

class ElectricityMapsCollector(BaseCollector):
    """
    A collector to fetch and store carbon intensity data from the Electricity Maps API
    into the central project database.
    """

    def __init__(self, zone: str):
        """
        Initializes the collector with the desired zone.

        Args:
            zone (str): The zone identifier to collect data for (e.g., 'FR', 'DE').
        """
        if not config.ELECTRICITY_MAPS_TOKEN:
            raise ValueError("ELECTRICITY_MAPS_TOKEN is not set in the environment.")
        if not zone:
            raise ValueError("Zone cannot be empty.")

        self.api_token = config.ELECTRICITY_MAPS_TOKEN
        self.zone = zone
        self.headers = {"auth-token": self.api_token}
        self.db_connection = get_db_connection()

    def collect(self):
        """
        Fetches the latest historical data from the Electricity Maps API and
        stores it in the configured database.
        """
        history_url = f"{API_BASE_URL}/carbon-intensity/history?zone={self.zone}"
        print(f"Fetching carbon intensity history for zone: {self.zone}...")

        try:
            response = requests.get(history_url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            history_data = data.get("history", [])

            if not history_data:
                print("No new history data found from Electricity Maps.")
                return

            new_records_count = self._save_history_data(history_data)
            print(f"Successfully saved {new_records_count} new carbon intensity point(s).")

        except requests.exceptions.RequestException as e:
            print(f"Error fetching data from Electricity Maps API: {e}")
        except Exception as e:
            print(f"An unexpected error occurred during collection: {e}")

    def _save_history_data(self, history_data: list) -> int:
        """
        Saves a list of historical data points to the database.

        Uses 'ON CONFLICT DO NOTHING' (PostgreSQL) or 'INSERT OR IGNORE' (SQLite)
        to efficiently skip records that already exist.

        Args:
            history_data (list): A list of data point dictionaries from the API.

        Returns:
            int: The number of new rows that were actually inserted.
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
                self.zone,
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

