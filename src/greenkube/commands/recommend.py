import logging
import traceback

import typer
from typing_extensions import Annotated

from ..reporters.console_reporter import ConsoleReporter

logger = logging.getLogger(__name__)
app = typer.Typer(name="recommend", help="Generate optimization recommendations.")


@app.command()
def recommend(
    namespace: Annotated[str, typer.Option(help="Display recommendations for a specific namespace.")] = None,
):
    try:
        from ..cli import get_processor
        from ..core.recommender import Recommender

        processor = get_processor()
        combined_data = processor.run()
        if not combined_data:
            raise typer.Exit(code=0)
        if namespace:
            combined_data = [c for c in combined_data if c.namespace == namespace]
            if not combined_data:
                raise typer.Exit(code=0)
        recommender = Recommender()
        recs = recommender.generate_zombie_recommendations(
            combined_data
        ) + recommender.generate_rightsizing_recommendations(combined_data)
        console = ConsoleReporter()
        if not recs:
            return
        # call reporter with recommendations kwarg
        console.report(data=combined_data, recommendations=recs)
    except typer.Exit:
        raise
    except Exception as e:
        logger.error(f"Error in recommend command: {e}")
        logger.error(traceback.format_exc())
        raise typer.Exit(code=1)
