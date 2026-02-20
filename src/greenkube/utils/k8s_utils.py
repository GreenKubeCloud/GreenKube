from decimal import Decimal
from typing import Optional


def parse_quantity(quantity: str) -> Decimal:
    """
    Parse kubernetes quantity to Decimal.
    Adapted from kubernetes-python utils.
    """
    if quantity is None:
        return Decimal(0)
    if isinstance(quantity, (int, float, Decimal)):
        return Decimal(quantity)

    quantity = str(quantity)
    number = quantity
    suffix = ""
    # Check for suffixes
    # Binary SI suffixes
    if quantity.endswith("Ki"):
        number = quantity[:-2]
        suffix = "Ki"
    elif quantity.endswith("Mi"):
        number = quantity[:-2]
        suffix = "Mi"
    elif quantity.endswith("Gi"):
        number = quantity[:-2]
        suffix = "Gi"
    elif quantity.endswith("Ti"):
        number = quantity[:-2]
        suffix = "Ti"
    elif quantity.endswith("Pi"):
        number = quantity[:-2]
        suffix = "Pi"
    elif quantity.endswith("Ei"):
        number = quantity[:-2]
        suffix = "Ei"
    # Decimal SI suffixes
    elif quantity.endswith("n"):
        number = quantity[:-1]
        suffix = "n"
    elif quantity.endswith("u"):
        number = quantity[:-1]
        suffix = "u"
    elif quantity.endswith("m"):
        number = quantity[:-1]
        suffix = "m"
    elif quantity.endswith("k"):
        number = quantity[:-1]
        suffix = "k"
    elif quantity.endswith("M"):
        number = quantity[:-1]
        suffix = "M"
    elif quantity.endswith("G"):
        number = quantity[:-1]
        suffix = "G"
    elif quantity.endswith("T"):
        number = quantity[:-1]
        suffix = "T"
    elif quantity.endswith("P"):
        number = quantity[:-1]
        suffix = "P"
    elif quantity.endswith("E"):
        number = quantity[:-1]
        suffix = "E"

    try:
        value = Decimal(number)
    except Exception:
        return Decimal(0)

    if suffix == "Ki":
        return value * 1024
    elif suffix == "Mi":
        return value * 1024**2
    elif suffix == "Gi":
        return value * 1024**3
    elif suffix == "Ti":
        return value * 1024**4
    elif suffix == "Pi":
        return value * 1024**5
    elif suffix == "Ei":
        return value * 1024**6
    elif suffix == "n":
        return value * Decimal("0.000000001")
    elif suffix == "u":
        return value * Decimal("0.000001")
    elif suffix == "m":
        return value * Decimal("0.001")
    elif suffix == "k":
        return value * 1000
    elif suffix == "M":
        return value * 1000**2
    elif suffix == "G":
        return value * 1000**3
    elif suffix == "T":
        return value * 1000**4
    elif suffix == "P":
        return value * 1000**5
    elif suffix == "E":
        return value * 1000**6

    return value


def parse_cpu_request(cpu: Optional[str]) -> int:
    """Converts K8s CPU string to millicores (int)."""
    if not cpu:
        return 0
    try:
        cores = parse_quantity(cpu)
        # Convert to millicores (multiply by 1000)
        millicores = int(cores * 1000)
        return millicores
    except Exception:
        return 0


def parse_memory_request(memory: Optional[str]) -> int:
    """Converts K8s memory string to bytes (int)."""
    if not memory:
        return 0
    try:
        bytes_val = int(parse_quantity(memory))
        return bytes_val
    except Exception:
        return 0


def parse_storage_request(storage: Optional[str]) -> int:
    """Converts K8s storage string (e.g., ephemeral-storage) to bytes (int)."""
    if not storage:
        return 0
    try:
        bytes_val = int(parse_quantity(storage))
        return bytes_val
    except Exception:
        return 0
