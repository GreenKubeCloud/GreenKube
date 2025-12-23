#!/usr/bin/env python3
import asyncio
import logging
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from greenkube.core.config import config
from greenkube.core.db import db_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("init_db")


async def init_db():
    start_msg = f"Initializing {config.DB_TYPE} database..."
    if config.DB_TYPE == "sqlite":
        start_msg += f" (Path: {config.DB_PATH})"
    elif config.DB_TYPE == "postgres":
        start_msg += f" (Connection: {config.DB_CONNECTION_STRING})"

    logger.info(start_msg)

    try:
        await db_manager.connect()
        logger.info("Database initialized successfully.")

        # Verify instance_carbon_profiles table existence (implicitly checked by setup success, but we can query)
        async with db_manager.connection_scope() as conn:
            if config.DB_TYPE == "sqlite":
                await conn.execute("SELECT count(*) FROM instance_carbon_profiles")
            elif config.DB_TYPE == "postgres":
                await conn.execute("SELECT count(*) FROM instance_carbon_profiles")
            logger.info("Verified 'instance_carbon_profiles' table exists.")

    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        sys.exit(1)
    finally:
        await db_manager.close()


if __name__ == "__main__":
    asyncio.run(init_db())
