"""UDP listener for F1 25 telemetry.

Binds 0.0.0.0:<port> and decodes packets into a rolling player-car state.
Set the PS5 to send to this machine's LAN IP (or enable Broadcast Mode).
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from . import packets as pk


@dataclass
class Frame:
    """A merged snapshot of the player car at one instant."""
    session_time: float
    session_uid: int
    lap_distance: float = 0.0
    total_distance: float = 0.0
    current_lap_num: int = 0
    current_lap_ms: int = 0
    last_lap_ms: int = 0
    current_lap_invalid: int = 0
    sector: int = 0
    car_position: int = 0
    # telemetry
    speed: int = 0
    throttle: float = 0.0
    brake: float = 0.0
    steer: float = 0.0
    gear: int = 0
    rpm: int = 0
    drs: int = 0
    # motion
    world_x: float = 0.0
    world_y: float = 0.0
    world_z: float = 0.0
    g_lat: float = 0.0
    g_long: float = 0.0
    # motion-ex slip (per wheel [RL,RR,FL,FR]); from MotionEx packet (id 13)
    slip_ratio: tuple = (0.0, 0.0, 0.0, 0.0)   # <0 locking, >0 spinning
    slip_angle: tuple = (0.0, 0.0, 0.0, 0.0)   # lateral slip angle (rad)
    # telemetry tyre/surface (per wheel [RL,RR,FL,FR]); from Car Telemetry (id 6)
    tyre_surface_temp: tuple = (0, 0, 0, 0)
    tyre_inner_temp: tuple = (0, 0, 0, 0)
    surface_type: tuple = (0, 0, 0, 0)
    offtrack: int = 0   # 1 if any wheel on an off-track surface this frame
    # status (slowly changing)
    tyre_compound: int = 0
    tyre_age_laps: int = 0
    ers_store: float = 0.0


class TelemetryListener:
    def __init__(self, host: str = "0.0.0.0", port: int = 20777,
                 on_frame=None, verbose: bool = False):
        self.host = host
        self.port = port
        self.on_frame = on_frame
        self.verbose = verbose
        self.sock: socket.socket | None = None
        # rolling player state, updated as packets arrive
        self._f = Frame(session_time=0.0, session_uid=0)
        self._warned_format = False
        self.packet_counts: dict[int, int] = {}
        self.last_packet_format = 0
        # slowly-changing context, exposed for recorders (latest snapshot)
        self.latest_setup: dict | None = None
        self.raw_setup_packet: bytes | None = None  # first CarSetups packet, for offset debugging
        self.latest_session: dict | None = None
        self.latest_damage: dict | None = None
        # event hook: called with (event_code, frame) when an Event packet arrives
        self.on_event = None

    def open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.settimeout(1.0)
        self.sock = s

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _handle(self, data: bytes):
        if len(data) < pk.HEADER_SIZE:
            return
        hdr = pk.parse_header(data)
        self.last_packet_format = hdr.packet_format
        if hdr.packet_format != pk.PACKET_FORMAT_2025 and not self._warned_format:
            print(f"[warn] packetFormat={hdr.packet_format}, expected 2025. "
                  f"Set in-game UDP Format to 2025 (offsets may differ otherwise).")
            self._warned_format = True

        self.packet_counts[hdr.packet_id] = self.packet_counts.get(hdr.packet_id, 0) + 1
        self._f.session_uid = hdr.session_uid
        self._f.session_time = hdr.session_time
        pidx = hdr.player_car_index

        emit = False
        if hdr.packet_id == pk.PID_LAP_DATA:
            ld = pk.parse_lap_player(data, pidx)
            if ld:
                self._f.lap_distance = ld.lap_distance
                self._f.total_distance = ld.total_distance
                self._f.current_lap_num = ld.current_lap_num
                self._f.current_lap_ms = ld.current_lap_ms
                self._f.last_lap_ms = ld.last_lap_ms
                self._f.current_lap_invalid = ld.current_lap_invalid
                self._f.sector = ld.sector
                self._f.car_position = ld.car_position
                emit = True  # lap data is the natural per-frame trigger (60 Hz)
        elif hdr.packet_id == pk.PID_CAR_TELEMETRY:
            t = pk.parse_telemetry_player(data, pidx)
            if t:
                self._f.speed = t.speed
                self._f.throttle = t.throttle
                self._f.brake = t.brake
                self._f.steer = t.steer
                self._f.gear = t.gear
                self._f.rpm = t.engine_rpm
                self._f.drs = t.drs
                self._f.tyre_surface_temp = t.tyres_surface_temp
                self._f.tyre_inner_temp = t.tyres_inner_temp
                self._f.surface_type = t.surface_type
                self._f.offtrack = 1 if any(s in pk.OFFTRACK_SURFACES
                                            for s in t.surface_type) else 0
        elif hdr.packet_id == pk.PID_MOTION:
            m = pk.parse_motion_player(data, pidx)
            if m:
                self._f.world_x = m.world_x
                self._f.world_y = m.world_y
                self._f.world_z = m.world_z
                self._f.g_lat = m.g_lat
                self._f.g_long = m.g_long
        elif hdr.packet_id == pk.PID_MOTION_EX:
            mx = pk.parse_motion_ex(data)
            if mx:
                self._f.slip_ratio = mx.slip_ratio
                self._f.slip_angle = mx.slip_angle
        elif hdr.packet_id == pk.PID_CAR_STATUS:
            st = pk.parse_status_player(data, pidx)
            if st:
                self._f.tyre_compound = st.visual_tyre_compound
                self._f.tyre_age_laps = st.tyres_age_laps
                self._f.ers_store = st.ers_store_energy
        elif hdr.packet_id == pk.PID_CAR_SETUPS:
            if self.raw_setup_packet is None:
                self.raw_setup_packet = data  # keep first one for offset debugging
            sd = pk.parse_setup_player(data, pidx)
            if sd:
                self.latest_setup = pk.sanitize_setup(sd)
        elif hdr.packet_id == pk.PID_SESSION:
            si = pk.parse_session(data)
            if si:
                import dataclasses as _dc
                self.latest_session = _dc.asdict(si)
        elif hdr.packet_id == pk.PID_CAR_DAMAGE:
            dd = pk.parse_damage_player(data, pidx)
            if dd:
                import dataclasses as _dc
                self.latest_damage = _dc.asdict(dd)
        elif hdr.packet_id == pk.PID_EVENT:
            code = pk.parse_event_code(data)
            if code and self.on_event:
                self.on_event(code, self._f)

        if emit and self.on_frame:
            # copy so downstream can retain it
            import copy
            self.on_frame(copy.copy(self._f))

    def run(self):
        if self.sock is None:
            self.open()
        print(f"[listening] udp {self.host}:{self.port}  (Ctrl-C to stop)")
        try:
            while True:
                try:
                    data, _addr = self.sock.recvfrom(4096)
                except socket.timeout:
                    continue
                self._handle(data)
        except KeyboardInterrupt:
            print("\n[stopped]")
        finally:
            self.close()
