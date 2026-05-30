MAXEM_HOLDING_REGISTERS = {
    "total_accumulators": (0x5000, 44),
    "by_tariff": (0x5170, 58),
    "per_phase": (0x5460, 108),
    "instantaneous_values": (0x5b00, 66),
    "inputs_outpus": (0x6300, 32),
    "data_identification": (0x8900, 96),
    "misc": (0x8A07, 30),
    "settings": (0x8C04, 8),
}

VICTRON_HOLDING_REGISTERS = {
    "hw_version": (0x8960, 6),
    "fw_version": (0x8908, 8),
    "serial": (0x8900, 2),
    "usage": (0x5B00, 48),
    "line_import_export": (0x5460, 24),
    "total_import_export": (0x5000, 8),
}
