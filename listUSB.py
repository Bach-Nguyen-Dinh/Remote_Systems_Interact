import os
import time

PHYSICAL_PORTS = {
    "USB A Gen2 Bottom": ["1-5", "1-5:1.0", "1-5:1.1", "1-5:1.2"],
    "USB A Gen2 Top": ["1-6", "1-6:1.0", "1-6:1.1"],
    "USB A Gen3 Left": ["2-7.2", "2-7.2:1.0", "1-4.1:1.2", "1-4.2:1.0", "1-4.2:1.1"],
    "USB A Gen3 Right": ["1-4.1", "1-4.1:1.0", "1-4.1:1.1", "2-7.1"]
}

USB_SYS_PATH = "/sys/bus/usb/devices/"
BLOCK_PATH = "/sys/block"
sector_size = 512

# Stores previous IO stats
previous_stats = {}

def get_connected_ports():
    return set(entry for entry in os.listdir(USB_SYS_PATH) if "-" in entry)

def get_block_devices_for_port(port_alias):
    block_devices = []
    port_path = os.path.join(USB_SYS_PATH, port_alias)
    if not os.path.exists(port_path):
        return block_devices
    for root, dirs, files in os.walk(port_path):
        for d in dirs:
            blk_path = os.path.join(root, d, "block")
            if os.path.exists(blk_path):
                block_devices.extend(os.listdir(blk_path))
    return block_devices

def read_io_stats(dev):
    try:
        with open(f"/sys/block/{dev}/stat") as f:
            values = f.read().split()
            read_sectors = int(values[2])
            write_sectors = int(values[6])
            return read_sectors, write_sectors
    except Exception:
        return None, None

def get_speed(dev):
    old = previous_stats.get(dev)
    current = read_io_stats(dev)
    if current is None or old is None:
        return None, None
    r1, w1 = old
    r2, w2 = current
    read_speed = ((r2 - r1) * sector_size) / 1024
    write_speed = ((w2 - w1) * sector_size) / 1024
    return read_speed, write_speed

def update_previous_stats():
    for dev in os.listdir(BLOCK_PATH):
        if dev.startswith("sd"):
            stats = read_io_stats(dev)
            if stats is not None:
                previous_stats[dev] = stats

def check_physical_ports():
    connected_ports = get_connected_ports()
    print("Physical USB Port Status with I/O Speeds:\n")

    for port_name, aliases in PHYSICAL_PORTS.items():
        found = False
        speed_info = "Read: NaN KB/s, Write: NaN KB/s"
        for alias in aliases:
            if alias in connected_ports:
                block_devices = get_block_devices_for_port(alias)
                if block_devices:
                    found = True
                    dev = block_devices[0]
                    read_speed, write_speed = get_speed(dev)
                    if read_speed is not None and write_speed is not None:
                        speed_info = f"Read: {read_speed:.2f} KB/s, Write: {write_speed:.2f} KB/s"
                    break
                else:
                    found = True
        status = "🟢 Connected" if found else "⚪ Empty"
        print(f"  {port_name:<20} → {status} | {speed_info}")
    print()

if __name__ == "__main__":
    try:
        update_previous_stats()
        time.sleep(1)
        while True:
            os.system("clear")
            check_physical_ports()
            update_previous_stats()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🔌 Exiting on user request.")
