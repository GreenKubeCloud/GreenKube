# tests/storage/test_sqlite_repository.py

from src.greenkube.storage.sqlite_repository import SQLiteCarbonIntensityRepository
from src.greenkube.core.db import DatabaseManager

def test_save_and_get_latest(test_db_connection):
    """
    Teste que l'on peut sauvegarder des données et récupérer la plus récente.
    """
    # Arrange
    db_manager = DatabaseManager()
    db_manager.connection = test_db_connection
    db_manager.init_schema()

    repo = SQLiteCarbonIntensityRepository()
    repo.db_connection = test_db_connection

    # Données de mock complètes pour correspondre aux attentes de la BDD
    history_data = [
        {
            "carbonIntensity": 23, 
            "datetime": "2025-08-16T16:00:00.000Z",
            "updatedAt": "2025-08-16T16:01:00.000Z",
            "createdAt": "2025-08-16T16:01:00.000Z",
            "emissionFactorType": "lifecycle",
            "isEstimated": True,
            "estimationMethod": "TIME_SLICER"
        },
        {
            "carbonIntensity": 27, 
            "datetime": "2025-08-16T17:00:00.000Z", # Le plus récent
            "updatedAt": "2025-08-16T17:01:00.000Z",
            "createdAt": "2025-08-16T17:01:00.000Z",
            "emissionFactorType": "lifecycle",
            "isEstimated": False,
            "estimationMethod": None
        },
    ]

    # Act
    repo.save_history(history_data, zone="FR")

    # Assert
    # On vérifie directement dans la BDD, car .rowcount n'est pas fiable
    cursor = test_db_connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM carbon_intensity WHERE zone = ?", ("FR",))
    count = cursor.fetchone()[0]
    assert count == 2, "Le nombre d'enregistrements insérés devrait être de 2"

    latest_intensity = repo.get_latest_for_zone(zone="FR")
    assert latest_intensity == 27, "La dernière valeur d'intensité devrait être 27"

