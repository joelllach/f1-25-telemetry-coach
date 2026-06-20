"""Emit synthetic F1 25-format UDP packets to test the pipeline without a PS5.

Builds a simple oval-ish lap with a few brake zones, two laps (2nd faster),
and streams Motion / LapData / CarTelemetry / CarStatus packets at ~30 Hz to
127.0.0.1:20777. Run `python -m f1coach debrief --dry-run` in another shell
first, then run this.

This also serves as an executable cross-check that our struct offsets are
self-consistent (we pack with the same layouts we unpack with).
"""
from __future__ import annotations

import math
import socket
import struct
import time

from f1coach import packets as pk

HOST, PORT = "127.0.0.1", 20777
PLAYER = 0
LAP_LEN = 3000.0  # metres


def header_bytes(packet_id: int, session_time: float, frame: int) -> bytes:
    return pk._HEADER.pack(
        pk.PACKET_FORMAT_2025,  # packetFormat
        25,                     # gameYear
        1, 0, 1,                # major/minor/packetVersion
        packet_id,
        0xABCDEF,               # sessionUID
        session_time,
        frame,
        frame,                  # overallFrame
        PLAYER,                 # playerCarIndex
        255,                    # secondaryPlayerCarIndex
    )


def pad_cars(player_record: bytes, record_size: int) -> bytes:
    """Place player record at slot PLAYER, zero-fill the other car slots."""
    assert len(player_record) <= record_size, (len(player_record), record_size)
    player_record = player_record.ljust(record_size, b"\x00")
    blank = b"\x00" * record_size
    out = bytearray()
    for i in range(pk.NUM_CARS):
        out += player_record if i == PLAYER else blank
    return bytes(out)


def motion_packet(session_time, frame, x, y, z, g_lat, g_long):
    body = pk._CAR_MOTION.pack(
        x, y, z,            # worldPosition
        0.0, 0.0, 0.0,      # worldVelocity
        0, 0, 0,            # forwardDir (int16)
        0, 0, 0,            # rightDir (int16)
        g_lat, g_long, 0.0, # gForces
        0.0, 0.0, 0.0,      # yaw/pitch/roll
    )
    return header_bytes(pk.PID_MOTION, session_time, frame) + pad_cars(body, pk.CAR_MOTION_SIZE)


def lap_packet(session_time, frame, lap_dist, total_dist, lap_num, cur_ms,
               last_ms, invalid, sector):
    body = pk._LAP.pack(
        last_ms, cur_ms,
        0, 0, 0, 0,         # sector1/2 ms+min parts
        0, 0, 0, 0,         # deltas
        lap_dist, total_dist, 0.0,
        1,                  # carPosition
        lap_num,
        0, 0,               # pitStatus, numPitStops
        sector,
        invalid,
        0, 0,               # penalties, totalWarnings
    )
    return header_bytes(pk.PID_LAP_DATA, session_time, frame) + pad_cars(body, pk.LAP_DATA_RECORD_SIZE)


def telem_packet(session_time, frame, speed, throttle, steer, brake, gear, rpm, drs):
    body = pk._TELEM.pack(
        int(speed), throttle, steer, brake,
        0,                  # clutch
        gear, int(rpm), drs,
        0, 0,               # revLightsPercent, revLightsBitValue
        300, 300, 300, 300, # brakesTemperature
        90, 90, 90, 90,     # tyresSurfaceTemp
        80, 80, 80, 80,     # tyresInnerTemp
        100,                # engineTemperature
        23.0, 23.0, 21.0, 21.0,  # tyresPressure
        0, 0, 0, 0,         # surfaceType
    )
    return header_bytes(pk.PID_CAR_TELEMETRY, session_time, frame) + pad_cars(body, pk.CAR_TELEMETRY_RECORD_SIZE)


def status_packet(session_time, frame):
    body = pk._STATUS.pack(
        0, 0, 1, 50, 0,         # TC, ABS, fuelMix, brakeBias, pitLimiter
        80.0, 110.0, 18.0,      # fuel in tank / capacity / remaining laps
        13000, 4000,            # maxRPM, idleRPM
        8, 0, 0,                # maxGears, drsAllowed, drsActivationDistance
        16, 16, 3,              # actual/visual tyre compound (Soft), age
        0,                      # vehicleFiaFlags
        100.0, 50.0, 2.0e6,     # ICE/MGUK power, ersStoreEnergy
        2,                      # ersDeployMode
        1.0e5, 1.0e4, 1.5e5,    # ers harvested/deployed
    )
    return header_bytes(pk.PID_CAR_STATUS, session_time, frame) + pad_cars(body, pk.CAR_STATUS_RECORD_SIZE)


def speed_profile(d, fast=False):
    """Synthetic speed (km/h) around the lap with 3 brake zones."""
    base = 250 + 40 * math.sin(d / LAP_LEN * 2 * math.pi)
    # brake zones centered at 800, 1700, 2500 m
    for center, depth in ((800, 150), (1700, 120), (2500, 170)):
        base -= depth * math.exp(-((d - center) ** 2) / (2 * 120 ** 2))
    if fast:
        base += 6  # faster lap: a touch more speed everywhere
    return max(60.0, base)


def emit():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    frame = 0
    t = 0.0
    dt = 1 / 30
    total = 0.0
    print(f"emitting synthetic telemetry -> {HOST}:{PORT} (2 laps)")
    for lap_num in (1, 2, 3):
        fast = (lap_num >= 2)
        d = 0.0
        lap_t0 = t
        while d < LAP_LEN:
            spd = speed_profile(d, fast)
            v_ms = spd / 3.6
            d += v_ms * dt
            total += v_ms * dt
            t += dt
            frame += 1
            # derive inputs from speed gradient
            ahead = speed_profile(min(LAP_LEN, d + 20), fast)
            braking = ahead < spd - 2
            brake = min(1.0, (spd - ahead) / 40) if braking else 0.0
            throttle = 0.0 if braking else min(1.0, 0.4 + (ahead - spd) / 20 + 0.3)
            gear = max(1, min(8, int(spd / 35) + 1))
            steer = 0.3 * math.sin(d / LAP_LEN * 6 * math.pi)
            cur_ms = int((t - lap_t0) * 1000)
            last_ms = 0 if lap_num == 1 else _prev_lap_ms[0]
            sector = 1 if d < 1000 else (2 if d < 2000 else 3)

            sock.sendto(motion_packet(t, frame, d, 0.0, 0.0, steer, brake - throttle), (HOST, PORT))
            sock.sendto(telem_packet(t, frame, spd, throttle, steer, brake, gear, 8000 + spd * 10, 0), (HOST, PORT))
            sock.sendto(lap_packet(t, frame, d, total, lap_num, cur_ms, last_ms, 0, sector), (HOST, PORT))
            if frame % 30 == 0:
                sock.sendto(status_packet(t, frame), (HOST, PORT))
            time.sleep(dt)
        _prev_lap_ms[0] = int((t - lap_t0) * 1000)
    # send one more lap-data packet to flush the final lap boundary
    frame += 1
    t += dt
    sock.sendto(lap_packet(t, frame, 0.0, total, 4, 0, _prev_lap_ms[0], 0, 1), (HOST, PORT))
    print("done.")


_prev_lap_ms = [0]

if __name__ == "__main__":
    emit()
