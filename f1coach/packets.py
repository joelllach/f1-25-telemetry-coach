"""F1 25 (packetFormat=2025) UDP packet parsing.

Only the fields the coach needs are decoded. Strategy for robustness against
year-to-year offset drift:

  * The common 29-byte header is parsed first and is extremely stable.
  * Each packet body is parsed with explicit struct formats. We DO NOT assume
    we parsed every trailing byte correctly -- we slice the exact region we
    need and read it. If EA added fields mid-struct in a future patch, only the
    affected packet degrades; the header and other packets keep working.
  * Expected packet sizes are recorded; size mismatches are surfaced as a
    warning by the caller rather than crashing.

Offsets follow the EA F1 25 UDP spec. VERIFY against the official spec
(EA Answers HQ "F1 25 UDP specification") before trusting any field whose
position you depend on -- small additions happen each season.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

PACKET_FORMAT_2025 = 2025

# Packet IDs (stable across recent titles)
PID_MOTION = 0
PID_SESSION = 1
PID_LAP_DATA = 2
PID_EVENT = 3
PID_PARTICIPANTS = 4
PID_CAR_SETUPS = 5
PID_CAR_TELEMETRY = 6
PID_CAR_STATUS = 7
PID_FINAL_CLASSIFICATION = 8
PID_LOBBY_INFO = 9
PID_CAR_DAMAGE = 10
PID_SESSION_HISTORY = 11
PID_TYRE_SETS = 12
PID_MOTION_EX = 13
PID_TIME_TRIAL = 14

NUM_CARS = 22  # F1 25 sends 22 car slots

# Common header: F1 25 layout
#   uint16 packetFormat
#   uint8  gameYear
#   uint8  gameMajorVersion
#   uint8  gameMinorVersion
#   uint8  packetVersion
#   uint8  packetId
#   uint64 sessionUID
#   float  sessionTime
#   uint32 frameIdentifier
#   uint32 overallFrameIdentifier
#   uint8  playerCarIndex
#   uint8  secondaryPlayerCarIndex
_HEADER = struct.Struct("<H BBBB B Q f I I B B")
HEADER_SIZE = _HEADER.size  # 29


@dataclass
class Header:
    packet_format: int
    game_year: int
    packet_id: int
    session_uid: int
    session_time: float
    frame_identifier: int
    player_car_index: int


def parse_header(data: bytes) -> Header:
    (pfmt, gyear, _maj, _min, _pver, pid, suid, stime,
     frame, _overall, player_idx, _sec) = _HEADER.unpack_from(data, 0)
    return Header(
        packet_format=pfmt,
        game_year=gyear,
        packet_id=pid,
        session_uid=suid,
        session_time=stime,
        frame_identifier=frame,
        player_car_index=player_idx,
    )


# ---------------------------------------------------------------------------
# Motion (ID 0) -- per-car. We only pull worldPosition X/Y/Z + g-forces for the
# player car. CarMotionData is 60 bytes each in F1 25.
#   float worldPositionX,Y,Z
#   float worldVelocityX,Y,Z
#   int16 worldForwardDirX,Y,Z   (normalised *32767)
#   int16 worldRightDirX,Y,Z
#   float gForceLateral, gForceLongitudinal, gForceVertical
#   float yaw, pitch, roll
_CAR_MOTION = struct.Struct("<6f 6h 6f")
CAR_MOTION_SIZE = _CAR_MOTION.size  # 60


@dataclass
class MotionData:
    world_x: float
    world_y: float
    world_z: float
    g_lat: float
    g_long: float
    g_vert: float


def parse_motion_player(data: bytes, player_idx: int) -> MotionData | None:
    off = HEADER_SIZE + player_idx * CAR_MOTION_SIZE
    if off + CAR_MOTION_SIZE > len(data):
        return None
    v = _CAR_MOTION.unpack_from(data, off)
    wx, wy, wz = v[0], v[1], v[2]
    g_lat, g_long, g_vert = v[12], v[13], v[14]
    return MotionData(wx, wy, wz, g_lat, g_long, g_vert)


# ---------------------------------------------------------------------------
# Motion Ex (ID 13) -- PLAYER CAR ONLY, a single flat record right after the
# header (not per-car). F1 25 PacketMotionExData layout, in order:
#   float suspensionPosition[4]
#   float suspensionVelocity[4]
#   float suspensionAcceleration[4]
#   float wheelSpeed[4]
#   float wheelSlipRatio[4]        <- longitudinal slip (lock-up < 0, spin > 0)
#   float wheelSlipAngle[4]        <- lateral slip angle (rad); under/oversteer
#   float wheelLatForce[4]
#   float wheelLongForce[4]
#   float heightOfCOGAboveGround
#   float localVelocityX,Y,Z
#   float angularVelocityX,Y,Z
#   float angularAccelerationX,Y,Z
#   float frontWheelsAngle
#   float wheelVertForce[4]
#   ... (later titles append more fields; we only read up to slip angle, so
#        trailing additions don't affect us)
# Wheel index order in F1: [0]=RL, [1]=RR, [2]=FL, [3]=FR
_MOTIONEX_HEAD = struct.Struct("<4f 4f 4f 4f 4f 4f")  # through wheelSlipAngle
MOTIONEX_HEAD_SIZE = _MOTIONEX_HEAD.size

WHEEL_NAMES = ("RL", "RR", "FL", "FR")


@dataclass
class MotionExData:
    slip_ratio: tuple   # per wheel [RL,RR,FL,FR]; <0 locking, >0 spinning
    slip_angle: tuple   # per wheel [RL,RR,FL,FR] in radians
    wheel_speed: tuple  # per wheel m/s


def parse_motion_ex(data: bytes) -> MotionExData | None:
    off = HEADER_SIZE
    if off + MOTIONEX_HEAD_SIZE > len(data):
        return None
    v = _MOTIONEX_HEAD.unpack_from(data, off)
    # groups of 4: susPos, susVel, susAcc, wheelSpeed, slipRatio, slipAngle
    wheel_speed = v[12:16]
    slip_ratio = v[16:20]
    slip_angle = v[20:24]
    return MotionExData(slip_ratio=slip_ratio, slip_angle=slip_angle,
                        wheel_speed=wheel_speed)


# ---------------------------------------------------------------------------
# Lap Data (ID 2) -- per-car LapData. F1 25 LapData layout (player slice only):
#   uint32 lastLapTimeInMS
#   uint32 currentLapTimeInMS
#   uint16 sector1TimeMSPart
#   uint8  sector1TimeMinutesPart
#   uint16 sector2TimeMSPart
#   uint8  sector2TimeMinutesPart
#   uint16 deltaToCarInFrontMSPart
#   uint8  deltaToCarInFrontMinutesPart
#   uint16 deltaToRaceLeaderMSPart
#   uint8  deltaToRaceLeaderMinutesPart
#   float  lapDistance
#   float  totalDistance
#   float  safetyCarDelta
#   uint8  carPosition
#   uint8  currentLapNum
#   uint8  pitStatus
#   uint8  numPitStops
#   uint8  sector
#   uint8  currentLapInvalid
#   uint8  penalties
#   uint8  totalWarnings
#   ... (more trailing fields we don't need)
_LAP = struct.Struct("<I I H B H B H B H B f f f B B B B B B B B")
LAP_PLAYER_SLICE = _LAP.size


@dataclass
class LapData:
    last_lap_ms: int
    current_lap_ms: int
    lap_distance: float
    total_distance: float
    car_position: int
    current_lap_num: int
    sector: int
    current_lap_invalid: int


# Full per-car LapData record size in F1 25. Used to index the player slot.
LAP_DATA_RECORD_SIZE = 57


def parse_lap_player(data: bytes, player_idx: int) -> LapData | None:
    off = HEADER_SIZE + player_idx * LAP_DATA_RECORD_SIZE
    if off + LAP_PLAYER_SLICE > len(data):
        return None
    v = _LAP.unpack_from(data, off)
    return LapData(
        last_lap_ms=v[0],
        current_lap_ms=v[1],
        lap_distance=v[10],
        total_distance=v[11],
        car_position=v[13],
        current_lap_num=v[14],
        sector=v[17],
        current_lap_invalid=v[18],
    )


# ---------------------------------------------------------------------------
# Car Telemetry (ID 6) -- per-car CarTelemetryData. Player slice:
#   uint16 speed (km/h)
#   float  throttle (0..1)
#   float  steer (-1..1)
#   float  brake (0..1)
#   uint8  clutch (0..100)
#   int8   gear (-1..8)
#   uint16 engineRPM
#   uint8  drs (0/1)
#   uint8  revLightsPercent
#   uint16 revLightsBitValue
#   uint16 brakesTemperature[4]
#   uint8  tyresSurfaceTemperature[4]
#   uint8  tyresInnerTemperature[4]
#   uint16 engineTemperature
#   float  tyresPressure[4]
#   uint8  surfaceType[4]
_TELEM = struct.Struct("<H f f f B b H B B H 4H 4B 4B H 4f 4B")
TELEM_PLAYER_SLICE = _TELEM.size
CAR_TELEMETRY_RECORD_SIZE = 60  # per-car record size in F1 25


@dataclass
class TelemetryData:
    speed: int
    throttle: float
    steer: float
    brake: float
    gear: int
    engine_rpm: int
    drs: int
    brakes_temp: tuple          # [RL,RR,FL,FR] deg C
    tyres_surface_temp: tuple   # [RL,RR,FL,FR] deg C
    tyres_inner_temp: tuple     # [RL,RR,FL,FR] deg C
    surface_type: tuple         # [RL,RR,FL,FR] surface code (see SURFACE_TYPE)


def parse_telemetry_player(data: bytes, player_idx: int) -> TelemetryData | None:
    off = HEADER_SIZE + player_idx * CAR_TELEMETRY_RECORD_SIZE
    if off + TELEM_PLAYER_SLICE > len(data):
        return None
    v = _TELEM.unpack_from(data, off)
    speed = v[0]
    throttle = v[1]
    steer = v[2]
    brake = v[3]
    gear = v[5]
    rpm = v[6]
    drs = v[7]
    brakes_temp = (v[10], v[11], v[12], v[13])
    surf_temp = (v[14], v[15], v[16], v[17])
    inner_temp = (v[18], v[19], v[20], v[21])
    surface = (v[27], v[28], v[29], v[30])
    return TelemetryData(speed, throttle, steer, brake, gear, rpm, drs,
                         brakes_temp, surf_temp, inner_temp, surface)


# Surface type codes (F1 25); anything not tarmac/kerb = running wide.
SURFACE_TYPE = {
    0: "tarmac", 1: "rumble", 2: "concrete", 3: "rock", 4: "gravel",
    5: "mud", 6: "sand", 7: "grass", 8: "water", 9: "cobble",
    10: "metal", 11: "ridged",
}
OFFTRACK_SURFACES = {4, 5, 6, 7}  # gravel/mud/sand/grass = off track


# ---------------------------------------------------------------------------
# Car Status (ID 7) -- player slice, we want fuel, tyre compound/age, ERS.
#   uint8  tractionControl
#   uint8  antiLockBrakes
#   uint8  fuelMix
#   uint8  frontBrakeBias
#   uint8  pitLimiterStatus
#   float  fuelInTank
#   float  fuelCapacity
#   float  fuelRemainingLaps
#   uint16 maxRPM
#   uint16 idleRPM
#   uint8  maxGears
#   uint8  drsAllowed
#   uint16 drsActivationDistance
#   uint8  actualTyreCompound
#   uint8  visualTyreCompound
#   uint8  tyresAgeLaps
#   int8   vehicleFiaFlags
#   float  enginePowerICE
#   float  enginePowerMGUK
#   float  ersStoreEnergy
#   uint8  ersDeployMode
#   float  ersHarvestedThisLapMGUK
#   float  ersHarvestedThisLapMGUH
#   float  ersDeployedThisLap
_STATUS = struct.Struct("<B B B B B f f f H H B B H B B B b f f f B f f f")
STATUS_PLAYER_SLICE = _STATUS.size
CAR_STATUS_RECORD_SIZE = 55  # per-car record size in F1 25


@dataclass
class StatusData:
    fuel_in_tank: float
    fuel_remaining_laps: float
    actual_tyre_compound: int
    visual_tyre_compound: int
    tyres_age_laps: int
    ers_store_energy: float
    ers_deploy_mode: int


def parse_status_player(data: bytes, player_idx: int) -> StatusData | None:
    off = HEADER_SIZE + player_idx * CAR_STATUS_RECORD_SIZE
    if off + STATUS_PLAYER_SLICE > len(data):
        return None
    v = _STATUS.unpack_from(data, off)
    return StatusData(
        fuel_in_tank=v[5],
        fuel_remaining_laps=v[7],
        actual_tyre_compound=v[13],
        visual_tyre_compound=v[14],
        tyres_age_laps=v[15],
        ers_store_energy=v[19],
        ers_deploy_mode=v[20],
    )


# Visual tyre compound codes (F1 25)
VISUAL_TYRE = {
    16: "Soft", 17: "Medium", 18: "Hard", 7: "Inter", 8: "Wet",
    15: "Wet(classic)", 19: "Super Soft", 20: "Soft(c)", 21: "Medium(c)", 22: "Hard(c)",
}

ERS_MODE = {0: "None", 1: "Medium", 2: "Hotlap", 3: "Overtake"}


# ---------------------------------------------------------------------------
# Car Setups (ID 5) -- per-car CarSetupData. F1 25 layout (player slice):
#   uint8  frontWing, rearWing
#   uint8  onThrottle, offThrottle          (differential %)
#   uint8  frontCamber? -> actually floats below; F1 25:
#   float  frontCamber, rearCamber, frontToe, rearToe
#   uint8  frontSuspension, rearSuspension
#   uint8  frontAntiRollBar, rearAntiRollBar
#   uint8  frontSuspensionHeight, rearSuspensionHeight
#   uint8  brakePressure, brakeBias
#   float  engineBraking?  -- (F1 25 added) ; then:
#   float  rearLeftTyrePressure, rearRightTyrePressure,
#          frontLeftTyrePressure, frontRightTyrePressure
#   uint8  ballast
#   float  fuelLoad
# VERIFIED against live data (player record is 50 bytes). Confirmed layout by
# matching known garage values (tyre pressures 29.6F/26.5R, camber/toe):
#   off  0  u8   front_wing
#   off  1  u8   rear_wing
#   off  2  u8   on_throttle_diff
#   off  3  u8   off_throttle_diff
#   off  4  f32  front_camber
#   off  8  f32  rear_camber
#   off 12  f32  front_toe
#   off 16  f32  rear_toe
#   off 20  u8   front_suspension
#   off 21  u8   rear_suspension
#   off 22  u8   front_anti_roll_bar
#   off 23  u8   rear_anti_roll_bar
#   off 24  u8   front_ride_height
#   off 25  u8   rear_ride_height
#   off 26  u8   brake_pressure
#   off 27  u8   brake_bias
#   off 28  u8   (engine braking / reserved)
#   off 29  f32  rear_left_tyre_pressure
#   off 33  f32  rear_right_tyre_pressure
#   off 37  f32  front_left_tyre_pressure
#   off 41  f32  front_right_tyre_pressure
#   off 45  u8   ballast
#   off 46  f32  fuel_load
_SETUP = struct.Struct("<B B B B 4f B B B B B B B B B 4f B f")
SETUP_PLAYER_SLICE = _SETUP.size
CAR_SETUP_RECORD_SIZE = 50  # verified from live body: 1100/22 = 50


@dataclass
class SetupData:
    front_wing: int
    rear_wing: int
    on_throttle_diff: int
    off_throttle_diff: int
    front_camber: float
    rear_camber: float
    front_toe: float
    rear_toe: float
    front_arb: int
    rear_arb: int
    front_ride_height: int
    rear_ride_height: int
    brake_pressure: int
    brake_bias: int
    tyre_pressures: tuple  # [RL,RR,FL,FR]


def parse_setup_player(data: bytes, player_idx: int) -> SetupData | None:
    off = HEADER_SIZE + player_idx * CAR_SETUP_RECORD_SIZE
    if off + SETUP_PLAYER_SLICE > len(data):
        return None
    v = _SETUP.unpack_from(data, off)
    # indices: 0 fw, 1 rw, 2 onT, 3 offT, 4-7 fcam/rcam/ftoe/rtoe (floats),
    # 8 fsus, 9 rsus, 10 farb, 11 rarb, 12 frh, 13 rrh, 14 bpress, 15 bbias,
    # 16 engbrk, 17-20 tyre pressures [RL,RR,FL,FR], 21 ballast, 22 fuel
    return SetupData(
        front_wing=v[0], rear_wing=v[1],
        on_throttle_diff=v[2], off_throttle_diff=v[3],
        front_camber=round(v[4], 2), rear_camber=round(v[5], 2),
        front_toe=round(v[6], 2), rear_toe=round(v[7], 2),
        front_arb=v[10], rear_arb=v[11],
        front_ride_height=v[12], rear_ride_height=v[13],
        brake_pressure=v[14], brake_bias=v[15],
        tyre_pressures=(round(v[17], 1), round(v[18], 1), round(v[19], 1), round(v[20], 1)),
    )


# Plausible ranges for setup fields. The back half of the F1 25 CarSetupData
# struct drifts year-to-year; rather than trust every field, we null anything
# outside its sane range so the coach only sees validated numbers. Verify the
# full layout against the official spec to recover the dropped fields.
_SETUP_RANGES = {
    "front_wing": (0, 50), "rear_wing": (0, 50),
    "on_throttle_diff": (20, 100), "off_throttle_diff": (20, 100),
    "front_camber": (-5.0, 0.0), "rear_camber": (-4.0, 0.0),
    "front_toe": (0.0, 0.2), "rear_toe": (0.0, 0.6),
    "front_arb": (1, 21), "rear_arb": (1, 21),
    "front_ride_height": (10, 50), "rear_ride_height": (10, 80),
    "brake_pressure": (50, 100), "brake_bias": (50, 70),
}


def sanitize_setup(s: "SetupData") -> dict:
    """Return setup as a dict with out-of-range (mis-parsed) fields set to None."""
    import dataclasses as _dc
    out = {}
    for k, v in _dc.asdict(s).items():
        if k == "tyre_pressures":
            ok = all(15.0 <= p <= 40.0 for p in v)  # psi-ish range
            out[k] = list(v) if ok else None
            continue
        lo_hi = _SETUP_RANGES.get(k)
        if lo_hi and not (lo_hi[0] <= v <= lo_hi[1]):
            out[k] = None
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Event (ID 3) -- 4-char event code right after the header, then a union of
# event-specific detail we mostly don't need. We just surface the code.
#   char eventStringCode[4]
EVENT_CODES = {
    "SSTA": "session_start", "SEND": "session_end", "FTLP": "fastest_lap",
    "RTMT": "retirement", "DRSE": "drs_enabled", "DRSD": "drs_disabled",
    "TMPT": "teammate_in_pits", "CHQF": "chequered_flag", "RCWN": "race_winner",
    "PENA": "penalty", "SPTP": "speed_trap", "STLG": "start_lights",
    "LGOT": "lights_out", "DTSV": "drive_through_served",
    "SGSV": "stop_go_served", "FLBK": "flashback", "BUTN": "button",
    "RDFL": "red_flag", "OVTK": "overtake", "SCAR": "safety_car",
    "COLL": "collision",
}


def parse_event_code(data: bytes) -> str | None:
    if len(data) < HEADER_SIZE + 4:
        return None
    code = data[HEADER_SIZE:HEADER_SIZE + 4].decode("ascii", errors="replace")
    return code


# ---------------------------------------------------------------------------
# Session (ID 1) -- single record after header. We pull weather + temps +
# track. Field order (F1 25 prefix we use):
#   uint8  weather (0=clear..5=storm)
#   int8   trackTemperature (C)
#   int8   airTemperature (C)
#   uint8  totalLaps
#   uint16 trackLength (m)
#   uint8  sessionType
#   int8   trackId
_SESSION = struct.Struct("<B b b B H B b")
SESSION_PREFIX_SIZE = _SESSION.size

WEATHER = {0: "clear", 1: "light_cloud", 2: "overcast", 3: "light_rain",
           4: "heavy_rain", 5: "storm"}
TRACK_IDS = {  # F1 25 ids (validated: 17 = Red Bull Ring, 4323m)
    17: "Austria (Red Bull Ring)", 2: "Shanghai", 12: "Suzuka",
    7: "Silverstone", 20: "Spa", 13: "Monza", 14: "Monaco",
}


@dataclass
class SessionInfo:
    weather: int
    track_temp: int
    air_temp: int
    total_laps: int
    track_length: int
    session_type: int
    track_id: int


def parse_session(data: bytes) -> SessionInfo | None:
    if HEADER_SIZE + SESSION_PREFIX_SIZE > len(data):
        return None
    w, tt, at, tl, tlen, st, tid = _SESSION.unpack_from(data, HEADER_SIZE)
    return SessionInfo(weather=w, track_temp=tt, air_temp=at, total_laps=tl,
                       track_length=tlen, session_type=st, track_id=tid)


# ---------------------------------------------------------------------------
# Car Damage (ID 10) -- per-car. Player slice prefix:
#   float tyresWear[4] (%)
#   uint8 tyresDamage[4] (%)
#   uint8 brakesDamage[4] (%)
#   uint8 frontLeftWingDamage, frontRightWingDamage, rearWingDamage
#   uint8 floorDamage, diffuserDamage, sidepodDamage
_DAMAGE = struct.Struct("<4f 4B 4B B B B B B B")
DAMAGE_PLAYER_SLICE = _DAMAGE.size
CAR_DAMAGE_RECORD_SIZE = 46  # per-car record size in F1 25 (validate!)


@dataclass
class DamageData:
    tyres_wear: tuple  # [RL,RR,FL,FR] %
    front_wing_damage: int
    rear_wing_damage: int


def parse_damage_player(data: bytes, player_idx: int) -> DamageData | None:
    off = HEADER_SIZE + player_idx * CAR_DAMAGE_RECORD_SIZE
    if off + DAMAGE_PLAYER_SLICE > len(data):
        return None
    v = _DAMAGE.unpack_from(data, off)
    tyres_wear = (round(v[0], 1), round(v[1], 1), round(v[2], 1), round(v[3], 1))
    fl_wing, fr_wing, rear_wing = v[12], v[13], v[14]
    return DamageData(tyres_wear=tyres_wear,
                      front_wing_damage=max(fl_wing, fr_wing),
                      rear_wing_damage=rear_wing)
