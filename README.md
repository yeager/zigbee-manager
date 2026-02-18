# Zigbee Manager

Zigbee device management via Zigbee2MQTT REST API, built with GTK4/Adwaita.

## Features
- View all Zigbee devices with details
- Rename devices
- OTA firmware update checks
- Remove devices
- Permit join control
- Mesh network tree visualization
- Device info (model, vendor, firmware, IEEE address)

## Dependencies
```bash
pip install requests paho-mqtt
```

## Run
```bash
PYTHONPATH=src python3 -c "from zigbee_manager.main import main; main()"
```

## License
GPL-3.0-or-later
