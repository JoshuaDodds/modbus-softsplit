#!/usr/bin/python3 -u
import time
import serial
import logging as logger
from dotenv import dotenv_values

import modbus_tk.defines as cst
from modbus_tk import modbus_tcp, modbus_rtu

SERIAL_PORT = dotenv_values('.env').get('SERIAL_PORT') or "/dev/ttyXRUSB0"
MODBUS_TCP_GW = dotenv_values('.env').get('MODBUS_TCP_GW_IP') or "192.168.1.140"
MODBUS_TCP_GW_PORT = int(dotenv_values('.env').get('MODBUS_TCP_GW_PORT') or 8899)

logger.basicConfig(
    format='%(asctime)s modbus-gw: %(message)s',
    level=logger.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')

# Enable debug logging ONLY for the modbus_tk internal engine to expose the TCP requests
modbus_tk_logger = logger.getLogger("modbus_tk")
modbus_tk_logger.setLevel(logger.DEBUG)


class SerialSniffer:
    def __init__(self, port):
        self._port = port

    def __getattr__(self, attr):
        return getattr(self._port, attr)

    def read(self, size=1):
        data = self._port.read(size)
        
        # Filter isolated bus-release noise (common on 2-wire RS485)
        if len(data) == 1 and data in (b'\x00', b'\xff'):
            return b''
            
        # These are tied to the root logger (INFO), so they remain silent
        if data:
            logger.debug(f"RTU RAW RX: {data.hex()}")
        return data

    def write(self, data):
        logger.debug(f"RTU RAW TX: {data.hex()}")
        return self._port.write(data)


def main():
    tcp_slave_server = None
    rtu_slave_server = None
    maxem_100 = None
    maxem_2 = None
    victron_100 = None
    victron_2 = None

    try:
        tcp_slave_server = modbus_tcp.TcpServer(port=502)
        
        raw_serial = serial.Serial(
            port=SERIAL_PORT, 
            baudrate=19200, 
            bytesize=8, 
            parity=serial.PARITY_EVEN, 
            stopbits=serial.STOPBITS_ONE, 
            xonxoff=0, 
            timeout=0.1, 
            inter_byte_timeout=0.01 
        )
        
        sniffed_serial = SerialSniffer(raw_serial)
        rtu_slave_server = modbus_rtu.RtuServer(sniffed_serial)

        maxem_100 = rtu_slave_server.add_slave(100)
        maxem_2 = rtu_slave_server.add_slave(2)
        victron_100 = tcp_slave_server.add_slave(100)
        victron_2 = tcp_slave_server.add_slave(2)

        for register_name in VICTRON_HOLDING_REGISTERS:
            addr = VICTRON_HOLDING_REGISTERS[register_name][0]
            addr_len = VICTRON_HOLDING_REGISTERS[register_name][1]
            victron_100.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)
            victron_2.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)

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
        if tcp_slave_server: tcp_slave_server.stop()
        if rtu_slave_server: rtu_slave_server.stop()

    tcp_master = modbus_tcp.TcpMaster(host=MODBUS_TCP_GW, port=MODBUS_TCP_GW_PORT, timeout_in_sec=5.0)

    last_heartbeat = time.time()

    while True:
        try:
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

            current_time = time.time()
            if current_time - last_heartbeat >= 60:
                logger.info(f"Heartbeat: Successfully polled master and updated virtual slaves.")
                last_heartbeat = current_time

            time.sleep(1)

        except Exception as _E:
            logger.error(f"tcp_master(error): {_E}")
            try:
                tcp_master.close()
            except Exception:
                pass
            time.sleep(5)


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

