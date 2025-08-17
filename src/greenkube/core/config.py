# src/greenkube/core/config.py

import os
from dotenv import load_dotenv

# Load environment variables from a .env file located in the project root
dotenv_path = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env')
load_dotenv(dotenv_path=dotenv_path)

class Config:
    """
    Handles the application's configuration by loading values from environment variables.
    """
    DB_TYPE = os.getenv("DB_TYPE", "sqlite")
    DB_PATH = os.getenv("DB_PATH", "greenkube_data.db")
    DB_CONNECTION_STRING = os.getenv("DB_CONNECTION_STRING")
    ELECTRICITY_MAPS_TOKEN = os.getenv("ELECTRICITY_MAPS_TOKEN")

    @classmethod
    def validate(cls):
        """
        Validates that the necessary configuration variables are set.
        """
        if cls.DB_TYPE not in ["sqlite", "postgres"]:
            raise ValueError("DB_TYPE must be either 'sqlite' or 'postgres'")
        if cls.DB_TYPE == "postgres" and not cls.DB_CONNECTION_STRING:
            raise ValueError("DB_CONNECTION_STRING must be set for postgres database")
        if not cls.ELECTRICITY_MAPS_TOKEN:
            print("Warning: ELECTRICITY_MAPS_TOKEN is not set.")

# Instantiate the config to be imported by other modules
config = Config()
config.validate()
