import asyncio
import logging
import typing

from kubernetes_asyncio import client, config

logger = logging.getLogger(__name__)

# Global lock to prevent race conditions during config loading
_CONFIG_LOCK = asyncio.Lock()
_CONFIG_LOADED = False
_API_CLIENT = None
_API_CLIENT_LOCK = asyncio.Lock()


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
            logger.warning("Unexpected error loading in-cluster config: %s", e)

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
            logger.warning("Unexpected error loading kubeconfig: %s", e)

    logger.warning("Failed to load any Kubernetes configuration.")
    return False


async def get_core_v1_api() -> typing.Optional[client.CoreV1Api]:
    """
    Returns a configured CoreV1Api instance.
    Safe to call concurrently.
    """
    if await ensure_k8s_config():
        # Return a CoreV1Api bound to a shared ApiClient to avoid creating
        # ephemeral ApiClient objects which leak aiohttp sessions when not closed.
        api_client = await _get_shared_api_client()
        return client.CoreV1Api(api_client=api_client)
    return None


async def get_autoscaling_v2_api() -> typing.Optional[client.AutoscalingV2Api]:
    """
    Returns a configured AutoscalingV2Api instance for HPA operations.
    Safe to call concurrently.
    """
    if await ensure_k8s_config():
        api_client = await _get_shared_api_client()
        return client.AutoscalingV2Api(api_client=api_client)
    return None


async def _get_shared_api_client() -> client.ApiClient:
    """Create or return a singleton ApiClient for the process.

    The singleton avoids spawning many ApiClient instances (which create
    aiohttp.ClientSession objects) that can be accidentally left unclosed.
    Close it on application shutdown by calling `close_k8s_client()`.
    """
    global _API_CLIENT

    if _API_CLIENT is not None:
        return _API_CLIENT

    async with _API_CLIENT_LOCK:
        if _API_CLIENT is not None:
            return _API_CLIENT
        # Construct a new ApiClient. This is synchronous but safe to call
        # from async code. Mark the client so callers can detect the shared
        # client and avoid closing it themselves.
        _API_CLIENT = client.ApiClient()
        try:
            setattr(_API_CLIENT, "_is_shared_k8s_client", True)
        except Exception:
            pass
        return _API_CLIENT


async def close_k8s_client() -> None:
    """Close the shared ApiClient if it exists.

    This should be called during application shutdown to properly close the
    underlying aiohttp session.
    """
    global _API_CLIENT
    if _API_CLIENT is None:
        return

    try:
        await _API_CLIENT.close()
    except Exception:
        logger.exception("Error while closing shared Kubernetes ApiClient")
    finally:
        _API_CLIENT = None
