# Modbus-softsplit
Modbus software based splitter/slave replicator allowing multiple modbus masters read access on a single bus
#### (Note: This is very early POC code and certainly not polished or ready for any form of "production" deployment)


## The Problem
The Modbus protocol, by design, permits multiple slave devices on a single bus but only a single master device. (This is legacy 
terminology which will be referred to as "server and client devices" in the rest of this documentation). 

What this means in practice is that if a situation arises where you have 2 server devices wishing to communicate with the same client devices, 
you will experience conflicts in the bus and an inability for either server devices to reliably communicate with the 
client devices.  This is usually not a common scenario and the most likely encountered scenario would be during the integration 
of systems not specifically designed to be interoperable with each other.  In my case, I encountered this issue while 
attempting to integrate a Victron Energy CerboGX device and a Maxem.io Home Electric Vehicle Charge controller with
ABB B23/B24 kWh Meters.  Both the Victron CerboGX and the Maxem device act as server devices on the bus and and interfere
with each other and cause bus conflicts if connected together on the same bus.  

## The Solution
How this project managed to address the problem was with an approach of creating 2 "virtual" client devices and a 
"virtual" server device which continously polls the physical client devices and continously updates the "virtual" 
clients with that polled data, mirroring the state and holding registers of the "virtual" clients with the state and 
holding registers of the real physical client devices. The "virtual" Master/Server lives on the "actual" data bus while
the two "virtual" Slave/Client devices, in essence, create 2 new data buses (one for each physical Master/Server device)
and allow each of the physical Master/Server devices to have exclusive communication to the "virtual" slaves on these 2 
new "virtual" buses.

![screenshot](/layout.png?raw=true)

## Notes on this implementation:
- The implementation "as is" uses an Elfin EW-11 serial server device as the physical Master/Server device. The software
in this repository then polls it with modbus-tcp in order to keep the virtual slaves/clients up to date. 
- The two virtual slaves in this implementation differ in that one exposes itself via modbus-tcp to the Victron 
Energy hardware while the other exposes itself via modbus-rtu by way of a USB to RS-485 dongle.  The Maxem device
mentioned earlier does not support modbus-tcp so it was necessary to wire it directly and provide access to the 
virtual client devices via modbus-rtu using the USB to RS-485 converter.  
- This code should be easily read and converted in the situation where you want to have multiple RTU based Server devices
or all Server devices support modbus-tcp.  The easy to use modbus-tk project makes it quite easy to quickly adjust to 
your specific situation. 

## Requirements
This project, with gratitude, depends on and appreciates the following third party projects
- Modbus-TK (https://github.com/ljean/modbus-tk)
- pyserial (for RTU/serial communication) (https://github.com/pyserial/pyserial)
