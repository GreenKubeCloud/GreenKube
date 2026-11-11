from unittest.mock import MagicMock, patch

import typer

from greenkube.cli.start import start


def test_start_initializes_async_loop():
    """Test that start() calls asyncio.run."""
    with patch("asyncio.run") as mock_run:
        # Mock context
        mock_ctx = MagicMock()
        mock_ctx.invoked_subcommand = None

        # Mock config to avoid DB setup or mock DB manager
        with (
            patch("greenkube.cli.start.get_config") as mock_get_config,
            patch("greenkube.cli.start._async_start", new_callable=MagicMock) as mock_async_start,
        ):
            mock_cfg = MagicMock()
            mock_cfg.DB_TYPE = "postgres"  # Skip sqlite path
            mock_cfg.LOG_LEVEL = "INFO"
            mock_cfg.PROMETHEUS_QUERY_RANGE_STEP = "1h"
            mock_cfg.NODE_ANALYSIS_INTERVAL = "1h"
            mock_get_config.return_value = mock_cfg

            # Start might raise Exit if it succeeds/fails, catch it
            try:
                start(mock_ctx, last="1h")
            except typer.Exit:
                pass

            mock_run.assert_called_once()
            mock_async_start.assert_called_once()
