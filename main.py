#!/usr/bin/python3 -u
import time
import serial
import logging as logger

import modbus_tk.defines as cst
from modbus_tk import modbus_tcp, modbus_rtu

SERIAL_PORT = "/dev/ttyXRUSB0"
MODBUS_TCP_GW = "192.168.1.140"
MODBUS_TCP_GW_PORT = 8899

logger.basicConfig(
    format='%(asctime)s modbus-gw: %(message)s',
    level=logger.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')

def main():
    tcp_slave_server = None
    rtu_slave_server = None
    maxem_100 = None
    maxem_2 = None
    victron_100 = None
    victron_2 = None

    try:
        tcp_slave_server = modbus_tcp.TcpServer(address="192.168.1.87", port=502)
        rtu_slave_server = modbus_rtu.RtuServer(serial.Serial(port=SERIAL_PORT, baudrate=19200, bytesize=8, parity=serial.PARITY_EVEN, stopbits=serial.STOPBITS_ONE, xonxoff=0, timeout=10))

        maxem_100 = rtu_slave_server.add_slave(100)
        maxem_2 = rtu_slave_server.add_slave(2)
        victron_100 = tcp_slave_server.add_slave(100)
        victron_2 = tcp_slave_server.add_slave(2)

        # Create registers for virtual slave devices
        # Victron Energy compatible memory blocks
        for register_name in VICTRON_HOLDING_REGISTERS:
            addr = VICTRON_HOLDING_REGISTERS[register_name][0]
            addr_len = VICTRON_HOLDING_REGISTERS[register_name][1]
            victron_100.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)
            victron_2.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)

        # Maxem Home compatible memory blocks
        for register_name in MAXEM_HOLDING_REGISTERS:
            addr = MAXEM_HOLDING_REGISTERS[register_name][0]
            addr_len = MAXEM_HOLDING_REGISTERS[register_name][1]
            maxem_100.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)
            maxem_2.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)

        tcp_slave_server.start()
        logger.info(f"Modbus TCP slave server started...")
        rtu_slave_server.start()
        logger.info(f"Modbus RTU slave server started...")

    except KeyboardInterrupt as _E:
        tcp_slave_server.stop()
        rtu_slave_server.stop()

    tcp_master = modbus_tcp.TcpMaster(host=MODBUS_TCP_GW, port=MODBUS_TCP_GW_PORT, timeout_in_sec=5.0)

    while True:
        try:
            # Poll the real ABB B23 hardware slaves via network connected Modbus-TCP server (waveshare / EW-11 / etc.)
            # and copy that data to the 'virtual' slaves.
            # Victron
            for register_name in VICTRON_HOLDING_REGISTERS:
                addr = VICTRON_HOLDING_REGISTERS[register_name][0]
                addr_len = VICTRON_HOLDING_REGISTERS[register_name][1]

                acload_values = tcp_master.execute(100, cst.READ_HOLDING_REGISTERS, addr, addr_len)
                if acload_values:
                    if tcp_slave_server and victron_100:
                        victron_100.set_values(register_name, addr, acload_values)
                tesla_values = tcp_master.execute(2, cst.READ_HOLDING_REGISTERS, addr, addr_len)
                if tesla_values:
                    if tcp_slave_server and victron_2:
                        victron_2.set_values(register_name, addr, tesla_values)
            # Maxem
            for register_name in MAXEM_HOLDING_REGISTERS:
                addr = MAXEM_HOLDING_REGISTERS[register_name][0]
                addr_len = MAXEM_HOLDING_REGISTERS[register_name][1]

                acload_values = tcp_master.execute(100, cst.READ_HOLDING_REGISTERS, addr, addr_len)
                if acload_values:
                    if rtu_slave_server and maxem_100:
                        maxem_100.set_values(register_name, addr, acload_values)
                tesla_values = tcp_master.execute(2, cst.READ_HOLDING_REGISTERS, addr, addr_len)
                if tesla_values:
                    if rtu_slave_server and maxem_2:
                        maxem_2.set_values(register_name, addr, tesla_values)

            time.sleep(0.5)

        except Exception as _E:
            logger.error(f"tcp_master(error): {_E}")
            tcp_master.close()


MAXEM_HOLDING_REGISTERS = dict({
    "total_accumulators":  (0x5000, 44),
    "by_tariff": (0x5170, 58),
    "per_phase": (0x5460, 108),
    "instantaneous_values": (0x5b00, 66),
    "inputs_outpus": (0x6300, 32),
    "data_identification": (0x8900, 96),
    "misc": (0x8A07, 30),
    "settings": (0x8c04, 8),
})

VICTRON_HOLDING_REGISTERS = dict({
    "hw_version":  (0x8960, 6),
    "fw_version":  (0x8908, 8),
    "serial":  (0x8900, 2),
    "usage": (0x5b00, 48),
    "line_import_export": (0x5460, 24),
    "total_import_export":  (0x5000, 8),
})


if __name__ == "__main__":
    main()
