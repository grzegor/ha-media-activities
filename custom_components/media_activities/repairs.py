"""Repairs link persistent configuration problems to native reconfiguration."""

from homeassistant.components.repairs import RepairsFlow
from homeassistant.components.repairs.const import FlowType
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
import voluptuous as vol

from .const import DOMAIN


@callback
def async_update_issues(hass, entry, missing_entities, configuration_error=None):
    """Ordinary waits and command-only equipment never create Repairs."""
    issues = {
        "entity_missing": ", ".join(sorted(missing_entities)),
        "invalid_configuration": configuration_error,
    }
    for key, detail in issues.items():
        issue_id = f"{entry.entry_id}_{key}"
        if not detail:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue
        ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=True,
            severity=ir.IssueSeverity.ERROR, translation_key=key,
            translation_placeholders={"system": entry.title, "detail": str(detail)},
            data={"entry_id": entry.entry_id})


class ReconfigureRepair(RepairsFlow):
    def __init__(self, entry_id):
        self.entry_id = entry_id

    async def async_step_init(self, user_input=None):
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(self, user_input=None):
        if self.hass.config_entries.async_get_entry(self.entry_id) is None:
            return self.async_abort(reason="entry_removed")
        if user_input is not None:
            flow = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": self.entry_id})
            return self.async_abort(reason="reconfigure", next_flow=(FlowType.CONFIG_FLOW, flow["flow_id"]))
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(hass, issue_id, data):
    return ReconfigureRepair((data or {}).get("entry_id", ""))
