# -*- coding: utf-8 -*-
"""ESP32-S3 sensor node integration — Phase 2.

The ESP32-S3 collects environmental + biometric data (LD6002 mmWave radar,
SHT3X temperature/humidity, AGS10 TVOC air quality, BH1750 ambient light)
and streams one JSON object per second over USB CDC to the SC171.

This module provides:
  - SensorState     — thread-safe container with null→0 normalization
  - SensorReader    — background subprocess wrapper, resilient to USB dropouts
  - get_sensor_state() — module-level accessor compatible with RuntimeServices

Integration point: RuntimeServices._create_sensor_reader()
"""

from src.sensors.sensor_state import SensorState, get_sensor_state
from src.sensors.sensor_reader import SensorReader

__all__ = ["SensorState", "SensorReader", "get_sensor_state"]
