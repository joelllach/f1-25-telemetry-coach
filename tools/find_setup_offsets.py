"""One-shot: capture a CarSetups packet and locate field offsets by matching
KNOWN ground-truth values the user read from the in-game garage.

Run this INSTEAD of the recorder for a few seconds (it binds the UDP port).
It scans the player's setup record for the known tyre pressures and brake bias,
prints the byte offsets, and dumps the float/byte layout so we can correct
packets.py precisely.

Usage:  python tools/find_setup_offsets.py
Edit KNOWN below to match your garage readout.
"""
import socket
import struct
import sys

from f1coach import packets as pk

# ---- ground truth from the garage (edit to match) ----
KNOWN = {
    "tyre_pressure_front": 29.6,   # psi, FL & FR
    "tyre_pressure_rear": 26.5,    # psi, RL & RR
    "brake_bias": 54,              # %
    "front_wing": 37,
    "rear_wing": 30,
}
PORT = 20777


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    s.settimeout(15)
    print(f"[listening {PORT}] waiting for a CarSetups (id 5) packet... drive/sit in garage")
    raw = None
    pidx = 0
    try:
        while raw is None:
            data, _ = s.recvfrom(4096)
            if len(data) < pk.HEADER_SIZE:
                continue
            hdr = pk.parse_header(data)
            if hdr.packet_id == pk.PID_CAR_SETUPS:
                raw = data
                pidx = hdr.player_car_index
    except socket.timeout:
        print("No CarSetups packet in 15s. Make sure you're in a session.")
        sys.exit(1)
    finally:
        s.close()

    print(f"got CarSetups packet, len={len(raw)}, playerIdx={pidx}")
    body_len = len(raw) - pk.HEADER_SIZE
    rec_size = body_len // pk.NUM_CARS if body_len % pk.NUM_CARS == 0 else None
    print(f"body={body_len} bytes; if 22 cars => record size = {rec_size} "
          f"(we assumed {pk.CAR_SETUP_RECORD_SIZE})")

    base = pk.HEADER_SIZE + pidx * (rec_size or pk.CAR_SETUP_RECORD_SIZE)
    end = base + (rec_size or pk.CAR_SETUP_RECORD_SIZE)
    rec = raw[base:end]
    print(f"\nplayer setup record bytes [{base}:{end}]")

    # scan every float offset in the record for known pressures
    print("\n=== float matches (offset relative to record start) ===")
    fp, rp = KNOWN["tyre_pressure_front"], KNOWN["tyre_pressure_rear"]
    for off in range(0, len(rec) - 3):
        try:
            val = struct.unpack_from("<f", rec, off)[0]
        except struct.error:
            continue
        for label, target in (("FRONT_PRESS", fp), ("REAR_PRESS", rp)):
            if abs(val - target) < 0.15:
                print(f"  off {off:3d}: {val:8.3f}  ~= {label} ({target})")
    # scan byte offsets for known small ints
    print("\n=== byte matches for brake_bias / wings ===")
    for off in range(len(rec)):
        b = rec[off]
        for label, target in (("brake_bias", KNOWN["brake_bias"]),
                              ("front_wing", KNOWN["front_wing"]),
                              ("rear_wing", KNOWN["rear_wing"])):
            if b == target:
                print(f"  off {off:3d}: {b:3d}  == {label}")

    # hex dump for manual inspection
    print("\n=== hex dump of player record ===")
    for i in range(0, len(rec), 16):
        chunk = rec[i:i + 16]
        hexs = " ".join(f"{x:02x}" for x in chunk)
        print(f"  {i:3d}: {hexs}")


if __name__ == "__main__":
    main()
