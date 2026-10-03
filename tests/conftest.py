"""Minimal real-HA fixtures; no network or physical equipment."""
import asyncio
from pathlib import Path
import sys

import pytest_asyncio
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntries
from homeassistant.helpers import area_registry, device_registry, entity_registry, issue_registry

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest_asyncio.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = ConfigEntries(instance, {})
    await instance.config_entries.async_initialize()
    await area_registry.async_load(instance, load_empty=True)
    device_registry.async_setup(instance)
    await device_registry.async_load(instance, load_empty=True)
    await entity_registry.async_load(instance, load_empty=True)
    await issue_registry.async_load(instance, load_empty=True)
    yield instance
    await instance.async_stop(force=True)
    await asyncio.sleep(0)
