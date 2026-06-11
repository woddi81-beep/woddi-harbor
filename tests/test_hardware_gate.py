from tools.hardware_gate import hardware_inventory


def test_hardware_inventory_has_required_fields() -> None:
    inventory = hardware_inventory()

    assert int(inventory["logical_cpus"]) > 0
    assert int(inventory["sockets"]) > 0
    assert int(inventory["memory_bytes"]) > 0
