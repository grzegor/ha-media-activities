"""Shared public constants for Media Activities."""

from homeassistant.const import Platform

DOMAIN = "media_activities"
NAME = "Media Activities"
VERSION = "0.1.1"
PLATFORMS = [Platform.SELECT, Platform.SWITCH, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]
IDLE = "idle"
PHASES = ["observing", "preparing", "ready", "applied_unverified", "recovering", "finishing", "blocked", "recovery_paused", "handover"]
CLEANUP_STATES = ["not_needed", "preserved", "pending", "complete", "blocked"]
