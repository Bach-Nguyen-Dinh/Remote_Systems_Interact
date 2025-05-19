import os

# Updated physical USB port mappings
PHYSICAL_PORTS = {
    "USB A Gen2 Bottom": [
        "1-5", "1-5:1.0", "1-5:1.1", "1-5:1.2"
    ],
    "USB A Gen2 Top": [
        "1-6", "1-6:1.0", "1-6:1.1"
    ],
    "USB A Gen3 Left": [
        "2-7.2", "2-7.2:1.0", "1-4.1:1.2", "1-4.2:1.0", "1-4.2:1.1"
    ],
    "USB A Gen3 Right": [
        "1-4.1", "1-4.1:1.0", "1-4.1:1.1", "2-7.1"
    ]
}

def get_connected_ports():
    usb_sys_path = "/sys/bus/usb/devices/"
    return set(entry for entry in os.listdir(usb_sys_path) if "-" in entry)

def check_physical_ports():
    connected_ports = get_connected_ports()

    print("📦 Physical USB Port Connection Status:\n")
    for port_name, aliases in PHYSICAL_PORTS.items():
        is_connected = any(alias in connected_ports for alias in aliases)
        status = "🟢 Connected" if is_connected else "⚪ Empty"
        print(f"  {port_name:<20} → {status}")

if __name__ == "__main__":
    check_physical_ports()
