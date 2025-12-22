import asyncio
import logging
import typing

from kubernetes_asyncio import client, config

logger = logging.getLogger(__name__)

# Global lock to prevent race conditions during config loading
_CONFIG_LOCK = asyncio.Lock()
_CONFIG_LOADED = False


async def ensure_k8s_config() -> bool:
    """
    Ensures that the Kubernetes configuration is loaded exactly once.
    This function is thread-safe and non-blocking (runs sync load in executor).

    Returns:
        bool: True if config was loaded successfully (or was already loaded), False otherwise.
    """
    global _CONFIG_LOADED

    if _CONFIG_LOADED:
        return True

    async with _CONFIG_LOCK:
        # Double-check locking pattern
        if _CONFIG_LOADED:
            return True

        # Try in-cluster config first
        try:
            logger.debug("Attempting to load in-cluster Kubernetes config...")
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration.")
            _CONFIG_LOADED = True
            return True
        except config.ConfigException:
            logger.debug("In-cluster config not found.")
        except Exception as e:
            logger.warning(f"Unexpected error loading in-cluster config: {e}")

        # Try local kubeconfig
        try:
            logger.debug("Attempting to load local kubeconfig...")
            await config.load_kube_config()
            logger.info("Loaded Kubernetes configuration from kubeconfig file.")
            _CONFIG_LOADED = True
            return True
        except config.ConfigException:
            logger.warning("Could not find kubeconfig file.")
        except Exception as e:
            logger.warning(f"Unexpected error loading kubeconfig: {e}")

    logger.warning("Failed to load any Kubernetes configuration.")
    return False


async def get_core_v1_api() -> typing.Optional[client.CoreV1Api]:
    """
    Returns a configured CoreV1Api instance.
    Safe to call concurrently.
    """
    if await ensure_k8s_config():
        return client.CoreV1Api()
    return None
