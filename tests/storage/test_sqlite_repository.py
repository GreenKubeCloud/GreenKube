import pytest
import sqlite3
from src.greenkube.storage.sqlite_repository import SQLiteCarbonIntensityRepository
from src.greenkube.core.db import DatabaseManager

def test_save_and_get_latest(test_db_connection):
    """
    Teste que l'on peut sauvegarder des données et récupérer la plus récente.
    """
    # Arrange
    db_manager = DatabaseManager()
    db_manager.connection = test_db_connection
    # CORRECTION: Renommage de la méthode d'initialisation du schéma
    db_manager.setup_sqlite()

    repo = SQLiteCarbonIntensityRepository(db_manager.get_connection())
    
    zone = "FR"
    history_data = [
        {"datetime": "2023-10-27T10:00:00Z", "carbonIntensity": 50},
        {"datetime": "2023-10-27T11:00:00Z", "carbonIntensity": 55},
        {"datetime": "2023-10-27T12:00:00Z", "carbonIntensity": 60},
    ]

    # Act
    saved_count = repo.save_history(history_data, zone)
    latest_intensity = repo.get_for_zone_at_time(zone, "2023-10-27T11:30:00Z")
    
    # Assert
    assert saved_count == 3
    assert latest_intensity == 55 # Doit retourner la valeur de 11h00, pas 12h00
